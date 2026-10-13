"""Run locally as a trusted operator after migrations. Never pass passwords as CLI arguments."""

import argparse
import asyncio
import getpass
import secrets
from uuid import uuid4

from app.core.config import Settings
from app.core.dependencies import Dependencies
from app.db.session import session_factory
from app.security.metadata import RequestMetadata, fingerprint
from app.security.service import SecurityError, SecurityService


async def bootstrap(
    username: str, password: str, reset: bool = False, restore_login: bool = False
) -> None:
    settings = Settings()
    dependencies = Dependencies(settings)
    try:
        security = SecurityService(
            session_factory(dependencies.engine), dependencies.redis, settings
        )
        meta = RequestMetadata(uuid4(), fingerprint("local-operator"), fingerprint("bootstrap-cli"))
        result = (
            await security.recover(username, password, meta, restore_login)
            if reset
            else await security.bootstrap(username, password, meta)
        )
        print("Administrator password reset:" if reset else "Administrator created:", result["id"])
    finally:
        await dependencies.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("--reset-existing", action="store_true")
    parser.add_argument("--restore-login", action="store_true")
    args = parser.parse_args()
    if args.restore_login and not args.reset_existing:
        parser.error("--restore-login requires --reset-existing")
    password = getpass.getpass("New administrator password (16-128 characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if not secrets.compare_digest(password.encode("utf-8"), confirmation.encode("utf-8")):
        raise SystemExit("Confirmation did not match")
    try:
        asyncio.run(bootstrap(args.username, password, args.reset_existing, args.restore_login))
    except (SecurityError, ValueError):
        raise SystemExit("Bootstrap refused; inspect configuration and security audit") from None


if __name__ == "__main__":
    main()
