"""
VPN Configuration Inventory Service (Section 13):
- Format validation (vless://, vmess://, trojan://, ss://, https:// subscription URLs)
- Normalized SHA-256 content hashing for in-batch and existing DB duplicate detection
- Bulk import preview with line-by-line diagnostics
- Atomic vs Partial batch import modes
- Concurrency-safe inventory reservation, assignment, and abandoned reservation recovery
- Encrypted storage of raw configuration strings at rest
"""
from __future__ import annotations
import hashlib
import time
from typing import Any, Dict, List, Optional, Tuple

from backend.app.core.security import decrypt_secret, encrypt_secret
from backend.app.models.database import DatabaseManager
from backend.app.services.rbac_audit import AuditAndRBACService

ALLOWED_PREFIXES: Dict[str, Tuple[str, ...]] = {
    "vless": ("vless://",),
    "vmess": ("vmess://",),
    "trojan": ("trojan://",),
    "Outline": ("ss://",),
    "subscription_url": ("https://",),
}


def normalize_config_for_hash(raw_config: str) -> str:
    """Normalize whitespace for duplicate detection while preserving the exact deliverable string."""
    return raw_config.strip()


def compute_config_hash(raw_config: str) -> str:
    normalized = normalize_config_for_hash(raw_config)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def validate_config_for_service(raw_config: str, service_type: str) -> Optional[str]:
    """Return None if valid, or a safe Persian/English diagnostic error without leaking secrets."""
    cleaned = raw_config.strip()
    if not cleaned:
        return "Empty configuration entry"
    if len(cleaned) < 16 or len(cleaned) > 4096:
        return "Configuration length out of bounds (16..4096 chars)"
    if any(ch in cleaned for ch in ("\n", "\n", "\x00")):
        return "Configuration contains illegal control characters"
    prefixes = ALLOWED_PREFIXES.get(service_type)
    if not prefixes:
        return f"Unsupported service type '{service_type}'"
    if not any(cleaned.startswith(p) for p in prefixes):
        return f"Configuration does not match expected scheme {prefixes} for {service_type}"
    return None


class InventoryService:
    def __init__(self, db: DatabaseManager, rbac: AuditAndRBACService, master_key: str) -> None:
        self.db = db
        self.rbac = rbac
        self.master_key = master_key

    def _split_entries(self, raw_payload: str, delimiter: str) -> List[str]:
        actual_delim = "\n" if delimiter in {"newline", "\n", "\n"} else delimiter
        if actual_delim not in {"\n", ",", ";", "|"}:
            raise ValueError("Unsupported delimiter; allowed: newline, comma (,), semicolon (;), pipe (|)")
        normalized_text = raw_payload.replace("\n", "\n")
        parts = normalized_text.split(actual_delim)
        return [p.strip() for p in parts if p.strip()]

    def preview_bulk_import(
        self, actor_admin_id: int, product_id: int, raw_payload: str, delimiter: str = "\n"
    ) -> Dict[str, Any]:
        """Validate batch and detect in-batch + existing DB duplicates without mutating DB."""
        self.rbac.require_permission(actor_admin_id, "inventory.import")
        product = self.db.fetch_one("SELECT * FROM products WHERE id = ?", (product_id,))
        if not product:
            raise ValueError("Product not found")
        entries = self._split_entries(raw_payload, delimiter)
        existing_hashes = {
            r["content_hash"]
            for r in self.db.fetch_all("SELECT content_hash FROM inventory_items")
        }
        seen_in_batch: set[str] = set()
        valid_items: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        duplicate_count = 0
        invalid_count = 0

        for idx, raw_item in enumerate(entries, start=1):
            err = validate_config_for_service(raw_item, product["service_type"])
            if err:
                invalid_count += 1
                errors.append({"line": idx, "type": "invalid", "error": err})
                continue
            c_hash = compute_config_hash(raw_item)
            if c_hash in seen_in_batch:
                duplicate_count += 1
                errors.append({"line": idx, "type": "duplicate_in_batch", "error": "Duplicate within uploaded batch"})
                continue
            if c_hash in existing_hashes:
                duplicate_count += 1
                errors.append({"line": idx, "type": "duplicate_in_db", "error": "Already exists in inventory database"})
                continue
            seen_in_batch.add(c_hash)
            valid_items.append({"line": idx, "raw": raw_item, "content_hash": c_hash})

        return {
            "product_id": product_id,
            "service_type": product["service_type"],
            "delimiter": delimiter,
            "total_count": len(entries),
            "valid_count": len(valid_items),
            "invalid_count": invalid_count,
            "duplicate_count": duplicate_count,
            "errors": errors,
            "can_commit_atomic": len(entries) > 0 and invalid_count == 0 and duplicate_count == 0,
            "_valid_items": valid_items,
        }

    def execute_bulk_import(
        self,
        actor_admin_id: int,
        product_id: int,
        raw_payload: str,
        *,
        delimiter: str = "\n",
        mode: str = "atomic",
    ) -> Dict[str, Any]:
        """Commit inventory import in either strict 'atomic' mode or explicitly labeled 'partial' mode."""
        self.rbac.require_permission(actor_admin_id, "inventory.import")
        if mode not in {"atomic", "partial", "single"}:
            raise ValueError("Import mode must be 'atomic', 'partial', or 'single'")
        preview = self.preview_bulk_import(actor_admin_id, product_id, raw_payload, delimiter=delimiter)
        if preview["total_count"] == 0:
            raise ValueError("Cannot import an empty inventory batch")
        if mode in {"atomic", "single"} and (preview["invalid_count"] > 0 or preview["duplicate_count"] > 0):
            raise ValueError(
                f"Atomic import rejected: {preview['invalid_count']} invalid and {preview['duplicate_count']} duplicate entries found"
            )
        if preview["valid_count"] == 0:
            raise ValueError("No valid entries to import")

        now = int(time.time())
        with self.db.transaction() as cur:
            cur.execute(
                """INSERT INTO inventory_batches
                   (product_id, imported_by_admin_id, import_mode, delimiter,
                    total_count, valid_count, invalid_count, duplicate_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    product_id,
                    actor_admin_id,
                    mode,
                    delimiter,
                    preview["total_count"],
                    preview["valid_count"],
                    preview["invalid_count"],
                    preview["duplicate_count"],
                    now,
                ),
            )
            batch_id = int(cur.lastrowid)
            for item in preview["_valid_items"]:
                enc = encrypt_secret(item["raw"], self.master_key)
                cur.execute(
                    """INSERT INTO inventory_items
                       (product_id, batch_id, config_encrypted, content_hash, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'available', ?, ?)""",
                    (product_id, batch_id, enc, item["content_hash"], now, now),
                )
            self.rbac.record_audit(
                actor_admin_id=actor_admin_id,
                action=f"inventory.import_{mode}",
                target_type="inventory_batch",
                target_id=str(batch_id),
                outcome="succeeded",
                metadata={
                    "product_id": product_id,
                    "total_count": preview["total_count"],
                    "valid_count": preview["valid_count"],
                    "invalid_count": preview["invalid_count"],
                    "duplicate_count": preview["duplicate_count"],
                },
                cur=cur,
            )
        return {
            "batch_id": batch_id,
            "mode": mode,
            "total_count": preview["total_count"],
            "imported_count": preview["valid_count"],
            "invalid_count": preview["invalid_count"],
            "duplicate_count": preview["duplicate_count"],
            "errors": preview["errors"],
        }

    def claim_and_assign_inventory_in_tx(
        self, cur: Any, product_id: int, order_id: str, now: int
    ) -> Tuple[int, str]:
        """Atomically claim one 'available' inventory item inside an active DB transaction.
        Guarantees two concurrent orders can never receive the same configuration.
        """
        cur.execute(
            """SELECT id, config_encrypted FROM inventory_items
               WHERE product_id = ? AND status = 'available'
               ORDER BY id ASC LIMIT 1""",
            (product_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("موجودی این محصول به پایان رسیده است.")
        item_id = int(row["id"])
        cur.execute(
            """UPDATE inventory_items
               SET status = 'assigned', assigned_to_order_id = ?, updated_at = ?
               WHERE id = ? AND status = 'available'""",
            (order_id, now, item_id),
        )
        if cur.rowcount != 1:
            raise ValueError("موجودی این محصول در لحظه توسط کاربر دیگری خریداری شد.")
        return item_id, row["config_encrypted"]

    def recover_expired_reservations(self, now_override: Optional[int] = None) -> int:
        now = now_override if now_override is not None else int(time.time())
        with self.db.transaction() as cur:
            cur.execute(
                """UPDATE inventory_items
                   SET status = 'available', reserved_by_order_id = NULL, reserved_until = NULL, updated_at = ?
                   WHERE status = 'reserved' AND reserved_until IS NOT NULL AND reserved_until <= ?""",
                (now, now),
            )
            return int(cur.rowcount)

    def get_stock_summary(self, product_id: int) -> Dict[str, int]:
        rows = self.db.fetch_all(
            "SELECT status, COUNT(*) AS cnt FROM inventory_items WHERE product_id = ? GROUP BY status",
            (product_id,),
        )
        summary = {"available": 0, "reserved": 0, "assigned": 0, "disabled": 0, "invalid": 0}
        for r in rows:
            summary[r["status"]] = int(r["cnt"])
        return summary

    def reveal_item_config_for_admin(self, actor_admin_id: int, item_id: int, reason: str) -> str:
        """Access-controlled decryption of an inventory item with mandatory audit record."""
        self.rbac.require_permission(actor_admin_id, "inventory.read")
        if not reason or not reason.strip():
            raise ValueError("Reason is required to view sensitive configuration")
        item = self.db.fetch_one("SELECT * FROM inventory_items WHERE id = ?", (item_id,))
        if not item:
            raise ValueError("Inventory item not found")
        self.rbac.record_audit(
            actor_admin_id=actor_admin_id,
            action="inventory.reveal_secret",
            target_type="inventory_item",
            target_id=str(item_id),
            outcome="succeeded",
            reason=reason.strip(),
        )
        return decrypt_secret(item["config_encrypted"], self.master_key)
