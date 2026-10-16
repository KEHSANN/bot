"""
Users, Administrator Authentication, Role-Based Access Control (RBAC),
Last-Super-Admin Protection, and Redacted Audit Logging Service.
"""
from __future__ import annotations
import json
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Set

from backend.app.core.security import (
    ALL_PERMISSIONS,
    AuthenticationError,
    AuthorizationError,
    SecurityError,
    create_signed_token,
    hash_password,
    verify_password,
    verify_signed_token,
)
from backend.app.models.database import DatabaseManager

SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|secret|token|config|credential|private|card_cvv|key)", re.IGNORECASE
)


def redact_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Redact any secret or configuration fields before persisting to audit logs."""
    cleaned: Dict[str, Any] = {}
    for k, v in metadata.items():
        if SENSITIVE_KEY_PATTERN.search(str(k)):
            cleaned[k] = "[REDACTED]"
        elif isinstance(v, dict):
            cleaned[k] = redact_metadata(v)
        elif isinstance(v, str) and any(
            v.startswith(prefix) for prefix in ("vless://", "vmess://", "trojan://", "ss://", "enc_v1:")
        ):
            cleaned[k] = "[REDACTED_CONFIG]"
        else:
            cleaned[k] = v
    return cleaned


class AuditAndRBACService:
    def __init__(self, db: DatabaseManager, secret_key: str) -> None:
        self.db = db
        self.secret_key = secret_key

    def record_audit(
        self,
        *,
        actor_admin_id: Optional[int],
        action: str,
        target_type: str,
        target_id: str,
        outcome: str,
        reason: Optional[str] = None,
        correlation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        cur: Any = None,
    ) -> int:
        now = int(time.time())
        corr_id = correlation_id or f"req_{secrets.token_hex(8)}"
        safe_meta = json.dumps(redact_metadata(metadata or {}), ensure_ascii=False)
        sql = """INSERT INTO audit_logs
                 (actor_admin_id, action, target_type, target_id, outcome, reason, correlation_id, metadata_json, created_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        params = (actor_admin_id, action, target_type, str(target_id), outcome, reason, corr_id, safe_meta, now)
        if cur is not None:
            cur.execute(sql, params)
            return int(cur.lastrowid)
        with self.db.transaction() as tx_cur:
            tx_cur.execute(sql, params)
            return int(tx_cur.lastrowid)

    def register_or_get_user(
        self, telegram_id: int, full_name: str, username: Optional[str] = None
    ) -> Dict[str, Any]:
        """Idempotently create or update a customer user and initialize their IRT wallet."""
        if telegram_id <= 0:
            raise ValueError("Invalid Telegram user ID")
        now = int(time.time())
        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            existing = cur.fetchone()
            if existing:
                user_id = existing["id"]
                cur.execute(
                    "UPDATE users SET full_name = ?, username = ?, updated_at = ? WHERE id = ?",
                    (full_name, username, now, user_id),
                )
            else:
                cur.execute(
                    """INSERT INTO users (telegram_id, username, full_name, status, created_at, updated_at)
                       VALUES (?, ?, ?, 'active', ?, ?)""",
                    (telegram_id, username, full_name, now, now),
                )
                user_id = int(cur.lastrowid)
                cur.execute(
                    "INSERT INTO wallets (user_id, balance_irt, updated_at) VALUES (?, 0, ?)",
                    (user_id, now),
                )
            cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            return dict(cur.fetchone())

    def bootstrap_super_admin(
        self, telegram_id: int, username: str, password: str
    ) -> Dict[str, Any]:
        """Safely bootstrap an initial super_admin account."""
        now = int(time.time())
        pw_hash = hash_password(password)
        with self.db.transaction() as cur:
            cur.execute(
                """INSERT INTO admin_users (telegram_id, username, password_hash, is_active, created_at)
                   VALUES (?, ?, ?, 1, ?)""",
                (telegram_id, username, pw_hash, now),
            )
            admin_id = int(cur.lastrowid)
            cur.execute(
                """INSERT INTO admin_role_assignments (admin_id, role_name, assigned_by_admin_id, created_at)
                   VALUES (?, 'super_admin', ?, ?)""",
                (admin_id, admin_id, now),
            )
            self.record_audit(
                actor_admin_id=admin_id,
                action="admin.bootstrap_super_admin",
                target_type="admin_user",
                target_id=str(admin_id),
                outcome="succeeded",
                reason="Initial super-admin bootstrap",
                cur=cur,
            )
            cur.execute("SELECT id, telegram_id, username, is_active, created_at FROM admin_users WHERE id = ?", (admin_id,))
            return dict(cur.fetchone())

    def get_admin_permissions(self, admin_id: int) -> Set[str]:
        admin = self.db.fetch_one("SELECT * FROM admin_users WHERE id = ? AND is_active = 1", (admin_id,))
        if not admin:
            return set()
        rows = self.db.fetch_all(
            """SELECT rp.permission_code
               FROM admin_role_assignments ara
               JOIN role_permissions rp ON ara.role_name = rp.role_name
               WHERE ara.admin_id = ?""",
            (admin_id,),
        )
        return {r["permission_code"] for r in rows}

    def require_permission(self, admin_id: int, permission: str) -> None:
        """Server-side RBAC enforcement; logs unauthorized attempts."""
        perms = self.get_admin_permissions(admin_id)
        if permission not in perms:
            self.record_audit(
                actor_admin_id=admin_id,
                action=f"rbac.denied:{permission}",
                target_type="permission",
                target_id=permission,
                outcome="denied",
                reason="Missing required permission",
            )
            raise AuthorizationError(f"Admin {admin_id} lacks required permission '{permission}'")

    def require_telegram_admin_permission(self, telegram_id: int, permission: str) -> Dict[str, Any]:
        """Verify Telegram identity maps to an active admin with the required permission."""
        admin = self.db.fetch_one(
            "SELECT id, telegram_id, username, is_active FROM admin_users WHERE telegram_id = ? AND is_active = 1",
            (telegram_id,),
        )
        if not admin:
            self.record_audit(
                actor_admin_id=None,
                action=f"telegram_admin.unauthorized:{permission}",
                target_type="telegram_user",
                target_id=str(telegram_id),
                outcome="denied",
                reason="Unrecognized Telegram user attempted admin action",
            )
            raise AuthorizationError("دسترسی غیرمجاز به پنل مدیریت.")
        self.require_permission(admin["id"], permission)
        return admin

    def authenticate_web_admin(self, username: str, password: str) -> Dict[str, Any]:
        admin = self.db.fetch_one(
            "SELECT * FROM admin_users WHERE username = ? AND is_active = 1", (username,)
        )
        if not admin or not verify_password(password, admin["password_hash"]):
            self.record_audit(
                actor_admin_id=admin["id"] if admin else None,
                action="admin.web_login",
                target_type="admin_user",
                target_id=username,
                outcome="failed",
                reason="Invalid credentials",
            )
            raise AuthenticationError("نام کاربری یا رمز عبور اشتباه است.")
        token = create_signed_token(
            {"admin_id": admin["id"], "username": admin["username"], "type": "web_session"},
            self.secret_key,
            ttl_seconds=28800,
        )
        decoded = verify_signed_token(token, self.secret_key)
        now = int(time.time())
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT INTO admin_sessions (jti, admin_id, revoked, expires_at, created_at) VALUES (?, ?, 0, ?, ?)",
                (decoded["jti"], admin["id"], decoded["exp"], now),
            )
            self.record_audit(
                actor_admin_id=admin["id"],
                action="admin.web_login",
                target_type="admin_user",
                target_id=str(admin["id"]),
                outcome="succeeded",
                cur=cur,
            )
        return {"token": token, "admin_id": admin["id"], "username": admin["username"]}

    def verify_web_session(self, token: str) -> Dict[str, Any]:
        decoded = verify_signed_token(token, self.secret_key)
        if decoded.get("type") != "web_session":
            raise AuthenticationError("Invalid session token type")
        session = self.db.fetch_one("SELECT * FROM admin_sessions WHERE jti = ?", (decoded["jti"],))
        if not session or session["revoked"] == 1:
            raise AuthenticationError("Session has been revoked")
        admin = self.db.fetch_one(
            "SELECT id, telegram_id, username, is_active FROM admin_users WHERE id = ? AND is_active = 1",
            (decoded["admin_id"],),
        )
        if not admin:
            raise AuthenticationError("Admin account is disabled")
        return admin

    def assign_role(self, actor_admin_id: int, target_admin_id: int, role_name: str) -> None:
        """Assign a role while preventing privilege self-escalation."""
        self.require_permission(actor_admin_id, "admins.manage")
        actor_perms = self.get_admin_permissions(actor_admin_id)
        role_perms = {
            r["permission_code"]
            for r in self.db.fetch_all(
                "SELECT permission_code FROM role_permissions WHERE role_name = ?", (role_name,)
            )
        }
        if not role_perms:
            raise ValueError(f"Unknown role: {role_name}")
        if not role_perms.issubset(actor_perms):
            raise AuthorizationError("Cannot grant a role with permissions exceeding your own")
        now = int(time.time())
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO admin_role_assignments (admin_id, role_name, assigned_by_admin_id, created_at) VALUES (?, ?, ?, ?)",
                (target_admin_id, role_name, actor_admin_id, now),
            )
            self.record_audit(
                actor_admin_id=actor_admin_id,
                action="admin.assign_role",
                target_type="admin_user",
                target_id=str(target_admin_id),
                outcome="succeeded",
                metadata={"role_name": role_name},
                cur=cur,
            )

    def remove_role_or_disable_admin(
        self, actor_admin_id: int, target_admin_id: int, role_to_remove: Optional[str] = None, disable: bool = False
    ) -> None:
        """Protect the last active super_admin from accidental removal or demotion (Section 19.12)."""
        self.require_permission(actor_admin_id, "admins.manage")
        with self.db.transaction() as cur:
            cur.execute(
                """SELECT COUNT(DISTINCT a.id) AS cnt
                   FROM admin_users a
                   JOIN admin_role_assignments r ON a.id = r.admin_id
                   WHERE a.is_active = 1 AND r.role_name = 'super_admin' AND a.id != ?""",
                (target_admin_id,),
            )
            other_supers = cur.fetchone()["cnt"]
            cur.execute(
                "SELECT COUNT(*) AS is_super FROM admin_role_assignments WHERE admin_id = ? AND role_name = 'super_admin'",
                (target_admin_id,),
            )
            target_is_super = cur.fetchone()["is_super"] > 0
            if target_is_super and other_supers == 0 and (disable or role_to_remove == "super_admin"):
                raise SecurityError("Cannot remove or demote the last active super_admin")
            if role_to_remove:
                cur.execute(
                    "DELETE FROM admin_role_assignments WHERE admin_id = ? AND role_name = ?",
                    (target_admin_id, role_to_remove),
                )
            if disable:
                cur.execute("UPDATE admin_users SET is_active = 0 WHERE id = ?", (target_admin_id,))
            self.record_audit(
                actor_admin_id=actor_admin_id,
                action="admin.modify_privileges",
                target_type="admin_user",
                target_id=str(target_admin_id),
                outcome="succeeded",
                metadata={"role_removed": role_to_remove, "disabled": disable},
                cur=cur,
            )

    def set_user_status(
        self, actor_admin_id: int, user_id: int, new_status: str, reason: str
    ) -> Dict[str, Any]:
        self.require_permission(actor_admin_id, "users.manage")
        if new_status not in {"active", "blocked", "suspended"}:
            raise ValueError("Invalid user status")
        if not reason or not reason.strip():
            raise ValueError("Explicit reason is required for changing user status")
        now = int(time.time())
        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            user = cur.fetchone()
            if not user:
                raise ValueError("User not found")
            old_status = user["status"]
            cur.execute("UPDATE users SET status = ?, updated_at = ? WHERE id = ?", (new_status, now, user_id))
            cur.execute(
                """INSERT INTO user_status_history (user_id, old_status, new_status, changed_by_admin_id, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, old_status, new_status, actor_admin_id, reason.strip(), now),
            )
            self.record_audit(
                actor_admin_id=actor_admin_id,
                action=f"user.status_change:{new_status}",
                target_type="user",
                target_id=str(user_id),
                outcome="succeeded",
                reason=reason.strip(),
                cur=cur,
            )
            cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            return dict(cur.fetchone())
