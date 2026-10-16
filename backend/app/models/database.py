"""
Transactional Relational Database Engine & Thread/Concurrency-Safe Session Manager.
Executes DDL migrations from 001_initial_schema.sql and provides ACID transactions
with write-lock serialization so concurrent operations (wallet debits, inventory claims,
payment callbacks, milestone approvals) strictly uphold database invariants.
"""
from __future__ import annotations
import contextlib
import pathlib
import re
import sqlite3
import threading
import time
from typing import Any, Dict, Iterator, List, Optional

from backend.app.core.security import DEFAULT_ROLES, ALL_PERMISSIONS, hash_password


def _pg_ddl_to_sqlite(sql_text: str) -> str:
    return sql_text.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")


class DatabaseManager:
    """Thread-safe, concurrency-safe transactional database manager."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA busy_timeout = 5000;")

    def migrate_and_seed(self) -> None:
        """Apply 001_initial_schema.sql and bootstrap default RBAC roles, permissions, and templates."""
        schema_path = (
            pathlib.Path(__file__).resolve().parents[2] / "migrations" / "001_initial_schema.sql"
        )
        raw_sql = schema_path.read_text(encoding="utf-8")
        sqlite_sql = _pg_ddl_to_sqlite(raw_sql)
        with self._lock:
            self._conn.executescript(sqlite_sql)
            self._conn.execute("PRAGMA foreign_keys = ON;")
        with self.transaction() as cur:
            now = int(time.time())
            for perm in sorted(ALL_PERMISSIONS):
                cur.execute(
                    "INSERT OR IGNORE INTO permissions (code, description) VALUES (?, ?)",
                    (perm, f"Permission for {perm}"),
                )
            for role_name, perms in DEFAULT_ROLES.items():
                cur.execute(
                    "INSERT OR IGNORE INTO roles (name, description) VALUES (?, ?)",
                    (role_name, f"Role {role_name}"),
                )
                for perm in perms:
                    cur.execute(
                        "INSERT OR IGNORE INTO role_permissions (role_name, permission_code) VALUES (?, ?)",
                        (role_name, perm),
                    )
            # Default Referral Rules singleton row
            cur.execute(
                """INSERT OR IGNORE INTO referral_reward_rules
                (id, is_enabled, referrer_reward_n_irt, referrer_n_trigger,
                 new_user_reward_w_irt, new_user_w_requires_membership,
                 first_payment_reward_p_irt, milestone_threshold_m,
                 milestone_reward_k_irt, milestone_repeatable, updated_at)
                VALUES (1, 1, 15000, 'membership_verified', 10000, 1, 25000, 5, 100000, 0, ?)""",
                (now,),
            )
            # Default Persian Message Templates
            default_templates = [
                (
                    "welcome",
                    "پیام خوش‌آمدگویی",
                    "سلام {full_name} عزیز! به فروشگاه اختصاصی VPN خوش آمدید.",
                    "full_name,user_id",
                ),
                (
                    "mandatory_membership",
                    "الزام عضویت در کانال",
                    "برای استفاده از خدمات ربات، لطفاً ابتدا در کانال‌های زیر عضو شوید و سپس دکمه «بررسی عضویت» را بزنید:",
                    "channels_count",
                ),
                (
                    "insufficient_balance",
                    "کسری موجودی کیف پول",
                    "موجودی کیف پول شما کافی نیست.\nمبلغ قابل پرداخت: {required_irt}\nموجودی فعلی: {balance_irt}\nکمبود موجودی: {shortage_irt}",
                    "required_irt,balance_irt,shortage_irt",
                ),
                (
                    "delivery_success",
                    "تحویل موفق اشتراک",
                    "سفارش شما با موفقیت تکمیل شد!\nشماره سفارش: {order_id}\nمحصول: {product_title}\nکانفیگ اختصاصی شما:\n{config_text}",
                    "order_id,product_title,config_text",
                ),
            ]
            for key, title_fa, body_fa, placeholders in default_templates:
                cur.execute(
                    """INSERT OR IGNORE INTO message_templates
                    (key, title_fa, body_fa, allowed_placeholders, version, is_enabled, updated_at)
                    VALUES (?, ?, ?, ?, 1, 1, ?)""",
                    (key, title_fa, body_fa, placeholders, now),
                )
            # Default Main Menu Buttons (Section 8.1)
            default_buttons = [
                ("main_menu", "خرید VPN", "browse_vpn", 1),
                ("main_menu", "محصولات اقتصادی", "economic_products", 2),
                ("main_menu", "دسته‌بندی محصولات", "categories", 3),
                ("main_menu", "کیف پول", "wallet_view", 4),
                ("main_menu", "شارژ کیف پول", "wallet_deposit", 5),
                ("main_menu", "سفارش‌های من", "my_orders", 6),
                ("main_menu", "لینک دعوت و پاداش‌ها", "referrals_hub", 7),
                ("main_menu", "راهنمای استفاده", "usage_guide", 8),
                ("main_menu", "پشتیبانی", "support_menu", 9),
                ("main_menu", "قوانین و شرایط", "terms_menu", 10),
            ]
            cur.execute("SELECT COUNT(*) AS cnt FROM workflow_buttons WHERE menu_key = 'main_menu'")
            if cur.fetchone()["cnt"] == 0:
                for menu_key, text_fa, action_key, order_idx in default_buttons:
                    cur.execute(
                        """INSERT INTO workflow_buttons
                        (menu_key, button_text_fa, action_key, display_order, is_visible)
                        VALUES (?, ?, ?, ?, 1)""",
                        (menu_key, text_fa, action_key, order_idx),
                    )

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        """Execute a serialized ACID database transaction with automatic rollback on failure."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE;")
            try:
                yield cur
                cur.execute("COMMIT;")
            except Exception:
                cur.execute("ROLLBACK;")
                raise
            finally:
                cur.close()

    def fetch_one(self, query: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(query, params)
                row = cur.fetchone()
                return dict(row) if row is not None else None
            finally:
                cur.close()

    def fetch_all(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(query, params)
                return [dict(r) for r in cur.fetchall()]
            finally:
                cur.close()
