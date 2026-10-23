"""Bulk import of unique configuration/subscription pairs.

Input format (one record per line, blank lines and ``#`` comments ignored):

    <config>|<subscription_url>
    <config> <TAB> <subscription_url>
    <config>|<subscription_url>|<optional_label>     # label is ignored

CSV files with a header row ``config,sub_url`` (or Persian aliases
``config,subscription``) are also accepted; comma separated values without a
header are treated as two columns.

Two import modes:

* ``SKIP_INVALID`` — valid, previously unseen pairs are imported. Every invalid
  or duplicate record is reported and skipped. Never silent.
* ``ALL_OR_NOTHING`` — if any record is invalid or duplicated (within the batch
  or already in the database) nothing is imported.

Deduplication uses keyed HMAC fingerprints of the *normalised* config and, when
present, of the subscription URL. Re-importing the same batch is a no-op.
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.db.enums import InventoryStatus, ProductType
from vpnbot.db.models.catalog import Plan, Product
from vpnbot.db.models.inventory import InventoryItem
from vpnbot.security.crypto import CredentialCipher, fingerprint


class ImportMode(str, Enum):
    SKIP_INVALID = "SKIP_INVALID"
    ALL_OR_NOTHING = "ALL_OR_NOTHING"


class ImportError(ValueError):
    pass


@dataclass
class ParsedRecord:
    line_no: int
    config: str
    sub_url: str | None
    error: str | None = None


@dataclass
class Preview:
    mode: ImportMode
    total: int = 0
    valid: int = 0
    invalid: int = 0
    duplicates_in_batch: int = 0
    duplicates_in_database: int = 0
    to_import: int = 0
    skipped: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)
    records: list[ParsedRecord] = field(default_factory=list)
    batch_id: str = ""

    @property
    def can_confirm(self) -> bool:
        if self.mode is ImportMode.ALL_OR_NOTHING:
            return (
                self.invalid == 0
                and self.duplicates_in_batch == 0
                and self.duplicates_in_database == 0
                and self.to_import > 0
            )
        return self.to_import > 0


@dataclass
class ImportResult:
    imported: int
    skipped_invalid: int
    skipped_duplicate_batch: int
    skipped_duplicate_db: int
    batch_id: str
    available_after: int


_SEPARATORS = ["|", "\t"]
_CSV_HEADERS = {
    "config": {"config", "configuration", "کانفیگ", "conf"},
    "sub_url": {"sub_url", "suburl", "subscription", "url", "لینک", "ساب"},
}


def parse_records(
    raw: str, *, allow_config_only: bool = False, subscription_links: bool = False
) -> list[ParsedRecord]:
    records: list[ParsedRecord] = []
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # CSV with header detection
    stripped = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if stripped and _looks_like_csv_header(stripped[0]) and (
        "," in stripped[0] or stripped[0].strip().lower() in set().union(*_CSV_HEADERS.values())
    ):
        # A header row marks CSV even with a single column (a
        # sub_url-only file has no comma to give it away).
        reader = csv.reader(io.StringIO("\n".join(stripped)))
        try:
            rows = list(reader)
        except csv.Error as exc:
            # A field past the csv module's 128KB limit raises
            # csv.Error, which is NOT this module's ImportError — it
            # used to crash the admin handler instead of producing
            # the friendly "unreadable input" outcome.
            raise ImportError(
                "فایل CSV قابل خواندن نیست (احتمالاً یک فیلد بیش از حد بزرگ است)"
            ) from exc
        header = [h.strip().lower() for h in rows[0]]
        idx_config = _find_header(header, "config")
        idx_sub = _find_header(header, "sub_url")
        for offset, row in enumerate(rows[1:], start=2):
            if not row or not any(cell.strip() for cell in row):
                continue
            config = row[idx_config].strip() if idx_config is not None and idx_config < len(row) else ""
            sub = row[idx_sub].strip() if idx_sub is not None and idx_sub < len(row) else ""
            if subscription_links and not config and sub:
                # Same rule as bare-line imports for subscription
                # products: a sub-only CSV row is a complete record.
                config = sub
            records.append(_record(offset, config, sub, allow_config_only))
        return records

    for line_no, line in enumerate(lines, start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if subscription_links and text.lower().startswith(("http://", "https://")) and "|" not in text and "\t" not in text:
            # A subscription product's stock IS the link: a bare URL
            # line is a complete record (the pair carries the link in
            # both slots, which is what delivery sends for it).
            records.append(_record(line_no, text, text, allow_config_only))
            continue
        config, sub = _split_line(text)
        records.append(_record(line_no, config, sub, allow_config_only))
    return records


def _looks_like_csv_header(line: str) -> bool:
    cells = [c.strip().lower() for c in line.split(",")]
    known = set().union(*_CSV_HEADERS.values())
    return all(cell in known for cell in cells if cell) and any(cell in known for cell in cells)


def _find_header(header: list[str], kind: str) -> int | None:
    aliases = _CSV_HEADERS[kind]
    for index, cell in enumerate(header):
        if cell in aliases:
            return index
    return None


def _split_line(text: str) -> tuple[str, str]:
    # Split ONLY when the right-hand side actually looks like a
    # subscription URL. Config URIs themselves routinely contain the
    # separators — "alpn=h2,http/1.1" has a comma, server names like
    # "#Germany | VIP" have a pipe — and the old split-on-sight rule
    # fed the tail to _record as a "subscription link" that then
    # failed the http(s) check, rejecting perfectly valid single
    # configs.
    for sep in (*_SEPARATORS, ","):
        # The pair separator is the LAST separator directly followed
        # by the subscription URL. Config URIs contain these
        # separators themselves ("alpn=h2,http/1.1", "#A | B") and the
        # URL may contain them in its own query ("?type=vless,vmess")
        # — splitting at the FIRST occurrence (the old rule) mangled
        # both sides: the config absorbed its glued-on link, and a
        # comma inside the link's query truncated the link.
        marker = max(text.rfind(sep + "http://"), text.rfind(sep + "https://"))
        if marker > 0:
            config = text[:marker].strip()
            rest = text[marker + len(sep) :]
            # A trailing "|label" column (pair format) is ignored;
            # a raw "|" cannot appear inside a valid URL anyway.
            sub = rest.split("|", 1)[0].strip()
            return config, sub
    # Pair-shaped lines whose right side is NOT a URL: a "|" or TAB
    # appearing BEFORE any "#" fragment is the pair separator, so
    # this was meant to be a pair with a broken link (typo'd scheme).
    # Surface it as an explicit record error via _record's URL check
    # instead of silently storing "config|badlink" as one config.
    # (A separator inside the fragment is part of the server name.)
    head = text.split("#", 1)[0]
    for sep in _SEPARATORS:
        if sep in head:
            config, _, rest = text.partition(sep)
            return config.strip(), rest.split(sep, 1)[0].strip()
    return text.strip(), ""


# Delivery sends the config (and the link) as Telegram messages,
# whose hard limit is 4096 characters including the surrounding
# template. Stock longer than these caps could be imported and sold
# but never delivered (TelegramBadRequest: message is too long left
# the order stuck), so the caps are enforced here, at the only stock
# intake.
MAX_CONFIG_LENGTH = 3500
MAX_SUB_URL_LENGTH = 1000


def _record(line_no: int, config: str, sub: str, allow_config_only: bool) -> ParsedRecord:
    if not config:
        return ParsedRecord(line_no, config, sub or None, error="مقدار کانفیگ خالی است")
    if len(config) > MAX_CONFIG_LENGTH:
        return ParsedRecord(
            line_no, config, sub or None,
            error=f"کانفیگ بیش از حد طولانی است (حداکثر {MAX_CONFIG_LENGTH} کاراکتر)",
        )
    if sub and len(sub) > MAX_SUB_URL_LENGTH:
        return ParsedRecord(
            line_no, config, sub,
            error=f"لینک اشتراک بیش از حد طولانی است (حداکثر {MAX_SUB_URL_LENGTH} کاراکتر)",
        )
    if not sub and not allow_config_only:
        return ParsedRecord(line_no, config, None, error="لینک اشتراک (sub_url) وجود ندارد")
    if sub and not sub.lower().startswith(("http://", "https://")):
        return ParsedRecord(line_no, config, sub, error="لینک اشتراک باید با http یا https شروع شود")
    return ParsedRecord(line_no, config, sub or None)


async def build_preview(
    session: AsyncSession,
    *,
    product: Product,
    raw: str,
    mode: ImportMode,
    secret: str,
    max_records: int = 20_000,
    label_filter: str | None = None,
) -> Preview:
    # CONFIG products are config-only by definition. COMBO means
    # "config + subscription" — a pair is required, so config-only
    # lines are errors there (an earlier version let them through
    # and sold combo stock with no subscription link). SUBSCRIPTION
    # products take bare link lines (handled in parse_records).
    allow_config_only = product.product_type is ProductType.CONFIG or label_filter == "config_only"
    records = parse_records(
        raw,
        allow_config_only=allow_config_only,
        subscription_links=product.product_type is ProductType.SUBSCRIPTION,
    )
    if not records:
        raise ImportError("هیچ رکوردی در ورودی یافت نشد")
    if len(records) > max_records:
        raise ImportError(f"حداکثر {max_records} رکورد در هر import مجاز است")

    preview = Preview(mode=mode, total=len(records), batch_id=uuid.uuid4().hex[:16])
    seen_config: set[str] = set()
    seen_sub: set[str] = set()
    candidates: list[ParsedRecord] = []

    for record in records:
        if record.error:
            preview.invalid += 1
            preview.errors.append((record.line_no, record.error))
            continue
        fp_config = fingerprint(record.config, secret)
        fp_sub = fingerprint(record.sub_url, secret) if record.sub_url else None
        if fp_config in seen_config or (fp_sub and fp_sub in seen_sub):
            preview.duplicates_in_batch += 1
            preview.errors.append((record.line_no, "تکراری در همین فایل"))
            continue
        seen_config.add(fp_config)
        if fp_sub:
            seen_sub.add(fp_sub)
        preview.valid += 1
        candidates.append(record)

    # Database duplicates
    if candidates:
        config_fps = [fingerprint(r.config, secret) for r in candidates]
        existing = set(
            (
                await session.execute(
                    select(InventoryItem.config_fingerprint).where(
                        InventoryItem.config_fingerprint.in_(config_fps)
                    )
                )
            ).scalars()
        )
        sub_fps = [fingerprint(r.sub_url, secret) for r in candidates if r.sub_url]
        existing_subs = set(
            (
                await session.execute(
                    select(InventoryItem.sub_url_fingerprint).where(
                        InventoryItem.sub_url_fingerprint.in_(sub_fps)
                    )
                )
            ).scalars()
        )
        fresh: list[ParsedRecord] = []
        for record in candidates:
            fp_config = fingerprint(record.config, secret)
            fp_sub = fingerprint(record.sub_url, secret) if record.sub_url else None
            if fp_config in existing or (fp_sub and fp_sub in existing_subs):
                preview.duplicates_in_database += 1
                preview.errors.append((record.line_no, "قبلاً در انبار ثبت شده است"))
                continue
            fresh.append(record)
        preview.records = fresh
    preview.to_import = len(preview.records)
    preview.skipped = preview.total - preview.to_import
    preview.errors.sort(key=lambda item: item[0])
    return preview


async def apply_import(
    session: AsyncSession,
    *,
    cipher: CredentialCipher,
    secret: str,
    product: Product,
    preview: Preview,
    plan_id: int | None,
    actor_admin_id: int,
) -> ImportResult:
    """Persist a confirmed preview. Caller owns the transaction."""
    if preview.mode is ImportMode.ALL_OR_NOTHING and not preview.can_confirm:
        raise ImportError("به دلیل وجود رکورد نامعتبر تکراری، import همه‌یا‌هیچ انجام نشد")
    if preview.to_import == 0:
        raise ImportError("رکورد معتبری برای درج وجود ندارد")
    if plan_id is not None:
        plan = await session.get(Plan, plan_id)
        if plan is None or plan.product_id != product.id:
            raise ImportError("پلن انتخابی متعلق به این محصول نیست")

    rows = []
    for record in preview.records:
        rows.append(
            {
                "product_id": product.id,
                "plan_id": plan_id,
                "config_cipher": cipher.encrypt(record.config),
                "config_fingerprint": fingerprint(record.config, secret),
                "sub_url_cipher": cipher.encrypt(record.sub_url) if record.sub_url else None,
                "sub_url_fingerprint": fingerprint(record.sub_url, secret) if record.sub_url else None,
                "status": InventoryStatus.AVAILABLE.value,
                "source_batch_id": preview.batch_id,
            }
        )

    # Insert in chunks: asyncpg caps a statement at 32,767 parameters
    # and each row binds 8, so a single insert of the allowed 20,000
    # records crashed above ~4,095 rows. Conflict handling drops the
    # index target so a clash on EITHER unique fingerprint (config or
    # sub_url) skips the row instead of aborting the whole import.
    inserted = 0
    for offset in range(0, len(rows), 1000):
        stmt = (
            pg_insert(InventoryItem)
            .values(rows[offset : offset + 1000])
            .on_conflict_do_nothing()
            .returning(InventoryItem.id)
        )
        inserted += len((await session.execute(stmt)).all())
    await session.flush()

    available_after = int(
        (
            await session.execute(
                select(func.count(InventoryItem.id)).where(
                    InventoryItem.product_id == product.id,
                    InventoryItem.status == InventoryStatus.AVAILABLE,
                )
            )
        ).scalar_one()
    )
    return ImportResult(
        imported=inserted,
        skipped_invalid=preview.invalid,
        skipped_duplicate_batch=preview.duplicates_in_batch,
        skipped_duplicate_db=preview.duplicates_in_database + max(0, preview.to_import - inserted),
        batch_id=preview.batch_id,
        available_after=available_after,
    )
