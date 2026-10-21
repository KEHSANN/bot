"""Command line entry point.

    vpnbot migrate            # apply Alembic migrations
    vpnbot bootstrap-owner    # grant the owner role to configured Telegram ids
    vpnbot seed               # roles, default screens, default settings
    vpnbot run                # polling bot + internal health server
    vpnbot worker             # durable job worker (same machine or another)
    vpnbot sweep              # one-shot payment expiry sweep

Both long-running commands share the same boot sequence: settings, logging,
database engine, then a graceful stop on SIGINT/SIGTERM so in-flight updates
finish before the pool is disposed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from datetime import UTC, datetime

from vpnbot.api.app import create_app
from vpnbot.config import Settings, get_settings
from vpnbot.db.session import dispose_engine, init_engine, session_scope

log = logging.getLogger("vpnbot.cli")


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s — %(message)s")
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    # aiogram logs every long-poll cycle at DEBUG; keep it out of production logs.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_migrate(_: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config

    cfg = Config(os.environ.get("ALEMBIC_CONFIG", "alembic.ini"))
    cfg.set_main_option("script_location", os.environ.get("ALEMBIC_SCRIPTS", "alembic"))
    command.upgrade(cfg, "head")
    print("migrations applied")
    return 0


async def cmd_bootstrap_owner(_: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.owner_ids:
        print("OWNER_TELEGRAM_IDS is empty; nothing to bootstrap", file=sys.stderr)
        return 2
    from vpnbot.services.auth import bootstrap_owner

    async with session_scope() as session:
        for telegram_id in settings.owner_ids:
            admin = await bootstrap_owner(session, telegram_id)
            print(f"owner ready: telegram_id={telegram_id} admin_id={admin.id}")
    return 0


async def cmd_seed(_: argparse.Namespace) -> int:
    from vpnbot.services.auth import ensure_roles
    from vpnbot.services.screens import ALL_STAGES, ScreenService
    from vpnbot.services.settings import DEFAULTS, SettingsService

    async with session_scope() as session:
        roles = await ensure_roles(session)
        print(f"roles: {', '.join(sorted(roles))}")
        screens = ScreenService(session)
        for stage in ALL_STAGES:
            await screens.get_or_create(stage)
        print(f"screens: {len(ALL_STAGES)}")
        settings_service = SettingsService(session)
        inserted = await settings_service.seed_defaults()
        print(f"settings: {len(DEFAULTS)} groups ({inserted} newly inserted)")
    return 0


async def cmd_sweep(_: argparse.Namespace) -> int:
    from vpnbot.db.session import get_session_factory
    from vpnbot.jobs import sweep_expired_payments_once

    expired = await sweep_expired_payments_once(get_session_factory())
    print(f"expired payments: {expired}")
    return 0


def _install_stop_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows has no signal handlers on the event loop; KeyboardInterrupt
        # still ends the process there.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)


async def _serve_health(settings: Settings, application):
    import uvicorn

    config = uvicorn.Config(
        application,
        host=settings.health_host,
        port=settings.health_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="health")
    return task, server


async def cmd_run(_: argparse.Namespace) -> int:
    from aiogram import Bot

    from vpnbot.bot.app import bot_properties, build_dispatcher, configure_commands
    from vpnbot.db.session import get_session_factory

    settings = get_settings()
    factory = get_session_factory()
    bot = Bot(settings.bot_token, default=bot_properties())
    dp = build_dispatcher(settings, factory)

    health_task, health_server = await _serve_health(settings, create_app(settings, factory))
    try:
        await configure_commands(bot, settings)
        # aiogram owns SIGINT/SIGTERM here so a Ctrl-C finishes the in-flight
        # update before polling stops.
        await dp.start_polling(bot)
    finally:
        health_server.should_exit = True
        try:
            await health_task
        except Exception:  # noqa: BLE001 - shutdown best effort
            log.debug("health server shutdown raised", exc_info=True)
        await bot.session.close()
        await dispose_engine()
        log.info("bot stopped")
    return 0


async def cmd_worker(_: argparse.Namespace) -> int:
    from aiogram import Bot

    from vpnbot.db.session import get_session_factory
    from vpnbot.jobs import Worker

    settings = get_settings()
    factory = get_session_factory()
    bot = Bot(settings.bot_token)
    worker = Worker(bot=bot, session_factory=factory, settings=settings)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, worker.stop)

    try:
        await worker.run_forever()
    finally:
        await bot.session.close()
        await dispose_engine()
        log.info("worker stopped")
    return 0


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
ASYNC_COMMANDS = {
    "bootstrap-owner": cmd_bootstrap_owner,
    "seed": cmd_seed,
    "run": cmd_run,
    "worker": cmd_worker,
    "sweep": cmd_sweep,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vpnbot", description="Telegram VPN sales platform")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("migrate", "apply database migrations"),
        ("bootstrap-owner", "grant the owner role to OWNER_TELEGRAM_IDS"),
        ("seed", "create roles, screens and settings"),
        ("run", "start the bot and the health endpoint"),
        ("worker", "start the background job worker"),
        ("sweep", "expire stale payments once and exit"),
    ):
        sub.add_parser(name, help=help_text)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        return cmd_migrate(args)

    settings = get_settings()
    configure_logging(settings)
    init_engine(settings)
    try:
        return asyncio.run(ASYNC_COMMANDS[args.command](args))
    except KeyboardInterrupt:
        return 130


__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
