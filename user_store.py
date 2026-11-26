"""Penyimpanan multi-user SQLite + enkripsi kredensial + paket langganan."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

PACKAGES: dict[str, tuple[str, int, str, int]] = {
    "mingguan": ("Mingguan (7 hari)", 7, "PRICE_WEEKLY", 10_000),
    "bulanan": ("Bulanan (30 hari)", 30, "PRICE_MONTHLY", 30_000),
    "semester": ("Semester (180 hari)", 180, "PRICE_SEMESTER", 120_000),
}

_PACKAGE_ALIASES: dict[str, str] = {
    "minggu": "mingguan",
    "mingguan": "mingguan",
    "weekly": "mingguan",
    "7": "mingguan",
    "bulan": "bulanan",
    "bulanan": "bulanan",
    "monthly": "bulanan",
    "30": "bulanan",
    "semester": "semester",
    "sem": "semester",
    "180": "semester",
}


def get_package(raw: str) -> tuple[str, str, int, int] | None:
    """Resolve nama/alias paket menjadi `(key, label, days, price)` atau None."""
    key = _PACKAGE_ALIASES.get(raw.strip().lower())
    if not key:
        return None
    label, days, env_key, default_price = PACKAGES[key]
    raw_price = os.getenv(env_key, "").strip()
    try:
        price = int(raw_price) if raw_price else default_price
    except ValueError:
        price = default_price
    return key, label, days, price


def format_packages_text() -> str:
    """Susun daftar paket dan harga untuk pesan Telegram."""
    lines: list[str] = []
    for key in ("mingguan", "bulanan", "semester"):
        resolved = get_package(key)
        assert resolved is not None
        _, label, _, price = resolved
        lines.append(f"• `{key}` — {label}: *Rp {price:,}*".replace(",", "."))
    return "\n".join(lines)


@dataclass(slots=True)
class UserRecord:
    """Data user terdaftar di SQLite (password sudah didekripsi di memori)."""

    chat_id: str
    telegram_username: str
    spada_username: str
    spada_password: str
    attendance_targets: list[str] = field(default_factory=list)
    status: str = "pending_payment"  # pending_payment | active | expired
    auto_enabled: bool = True
    expired_at: datetime | None = None

    def is_active(self, now: datetime) -> bool:
        """True bila status active dan belum melewati masa aktif."""
        return (
            self.status == "active"
            and self.expired_at is not None
            and self.expired_at > now
        )


def _load_fernet(key_path: Path) -> Fernet:
    """Baca kunci enkripsi dari CREDENTIAL_KEY atau file key (buat bila belum ada)."""
    env_key = os.getenv("CREDENTIAL_KEY", "").strip()
    if env_key:
        return Fernet(env_key.encode("utf-8"))

    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        key = key_path.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        os.chmod(key_path, 0o600)
    return Fernet(key)


class UserStore:
    """Penyimpanan user dan pembayaran berbasis SQLite."""

    def __init__(self, db_path: Path, key_path: Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_key = key_path or (self.db_path.parent / ".bot_key")
        self._fernet = _load_fernet(resolved_key)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id TEXT PRIMARY KEY,
                telegram_username TEXT NOT NULL DEFAULT '',
                spada_username TEXT NOT NULL,
                spada_password_enc TEXT NOT NULL,
                attendance_urls TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pending_payment',
                auto_enabled INTEGER NOT NULL DEFAULT 1,
                expired_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                package TEXT NOT NULL,
                days INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                proof_file_id TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                reviewed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sent_announcements (
                id TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _row_to_user(self, row: sqlite3.Row) -> UserRecord:
        password = self._fernet.decrypt(row["spada_password_enc"].encode("utf-8")).decode("utf-8")
        targets = json.loads(row["attendance_urls"] or "[]")
        expired_raw = row["expired_at"]
        return UserRecord(
            chat_id=row["chat_id"],
            telegram_username=row["telegram_username"],
            spada_username=row["spada_username"],
            spada_password=password,
            attendance_targets=[str(t) for t in targets],
            status=row["status"],
            auto_enabled=bool(row["auto_enabled"]),
            expired_at=datetime.fromisoformat(expired_raw) if expired_raw else None,
        )

    def upsert_credentials(
        self,
        chat_id: str,
        telegram_username: str,
        spada_username: str,
        spada_password: str,
        now: datetime,
    ) -> UserRecord:
        """Simpan atau perbarui kredensial SPADA user tanpa mereset status langganan."""
        enc = self._fernet.encrypt(spada_password.encode("utf-8")).decode("utf-8")
        now_iso = now.isoformat()
        existing = self._conn.execute(
            "SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if existing:
            self._conn.execute(
                """
                UPDATE users
                SET telegram_username = ?, spada_username = ?, spada_password_enc = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (telegram_username, spada_username, enc, now_iso, chat_id),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO users (
                    chat_id, telegram_username, spada_username, spada_password_enc,
                    attendance_urls, status, auto_enabled, expired_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '[]', 'pending_payment', 1, NULL, ?, ?)
                """,
                (chat_id, telegram_username, spada_username, enc, now_iso, now_iso),
            )
        self._conn.commit()
        user = self.get_user(chat_id)
        assert user is not None
        return user

    def get_user(self, chat_id: str) -> UserRecord | None:
        row = self._conn.execute(
            "SELECT * FROM users WHERE chat_id = ?", (str(chat_id),)
        ).fetchone()
        return self._row_to_user(row) if row else None

    def list_users(self) -> list[UserRecord]:
        rows = self._conn.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
        return [self._row_to_user(row) for row in rows]

    def list_active_users(self, now: datetime) -> list[UserRecord]:
        """Tandai user kadaluarsa dan kembalikan daftar user yang masih aktif."""
        now_iso = now.isoformat()
        for row in self._conn.execute(
            "SELECT chat_id, expired_at FROM users WHERE status = 'active'"
        ).fetchall():
            exp = datetime.fromisoformat(row["expired_at"]) if row["expired_at"] else None
            if exp is None or exp <= now:
                self._conn.execute(
                    "UPDATE users SET status = 'expired', updated_at = ? WHERE chat_id = ?",
                    (now_iso, row["chat_id"]),
                )
        self._conn.commit()
        return [u for u in self.list_users() if u.is_active(now)]

    def set_targets(self, chat_id: str, targets: list[str], now: datetime) -> None:
        self._conn.execute(
            "UPDATE users SET attendance_urls = ?, updated_at = ? WHERE chat_id = ?",
            (json.dumps(list(dict.fromkeys(targets))), now.isoformat(), str(chat_id)),
        )
        self._conn.commit()

    def set_auto(self, chat_id: str, enabled: bool, now: datetime) -> None:
        self._conn.execute(
            "UPDATE users SET auto_enabled = ?, updated_at = ? WHERE chat_id = ?",
            (1 if enabled else 0, now.isoformat(), str(chat_id)),
        )
        self._conn.commit()

    def create_payment(self, chat_id: str, package_key: str, now: datetime) -> dict[str, Any]:
        """Buat tagihan pending baru (batalkan tagihan pending lama milik user ini)."""
        resolved = get_package(package_key)
        if resolved is None:
            raise ValueError(f"Paket tidak dikenal: {package_key}")
        key, label, days, amount = resolved
        self._conn.execute(
            "UPDATE payments SET status = 'cancelled' WHERE chat_id = ? AND status = 'pending'",
            (str(chat_id),),
        )
        cur = self._conn.execute(
            """
            INSERT INTO payments (chat_id, package, days, amount, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (str(chat_id), key, days, amount, now.isoformat()),
        )
        self._conn.commit()
        return {
            "id": cur.lastrowid,
            "chat_id": str(chat_id),
            "package": key,
            "label": label,
            "days": days,
            "amount": amount,
            "status": "pending",
            "proof_file_id": "",
        }

    def get_pending_payment(self, chat_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM payments WHERE chat_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
            (str(chat_id),),
        ).fetchone()
        return dict(row) if row else None

    def attach_payment_proof(self, chat_id: str, proof_file_id: str) -> dict[str, Any] | None:
        pending = self.get_pending_payment(chat_id)
        if not pending:
            return None
        self._conn.execute(
            "UPDATE payments SET proof_file_id = ? WHERE id = ?",
            (proof_file_id, pending["id"]),
        )
        self._conn.commit()
        pending["proof_file_id"] = proof_file_id
        return pending

    def approve_payment(
        self, chat_id: str, now: datetime, package_override: str | None = None
    ) -> tuple[UserRecord, str, int]:
        """Aktifkan atau perpanjang masa aktif user sesuai paket."""
        user = self.get_user(chat_id)
        if user is None:
            raise ValueError(f"User `{chat_id}` belum terdaftar.")

        pending = self.get_pending_payment(chat_id)
        pkg_raw = package_override or (pending["package"] if pending else "")
        resolved = get_package(pkg_raw)
        if resolved is None:
            raise ValueError(
                "User belum memilih paket. Sertakan paket: `/approve <chat_id> mingguan|bulanan|semester`"
            )

        pkg_key, label, days, amount = resolved
        now_iso = now.isoformat()
        if pending:
            self._conn.execute(
                """
                UPDATE payments
                SET status = 'approved', package = ?, days = ?, amount = ?, reviewed_at = ?
                WHERE id = ?
                """,
                (pkg_key, days, amount, now_iso, pending["id"]),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO payments (chat_id, package, days, amount, status, note, created_at, reviewed_at)
                VALUES (?, ?, ?, ?, 'approved', 'manual admin approve', ?, ?)
                """,
                (str(chat_id), pkg_key, days, amount, now_iso, now_iso),
            )

        base_dt = user.expired_at if user.is_active(now) and user.expired_at else now
        new_expiry = base_dt + timedelta(days=days)
        self._conn.execute(
            """
            UPDATE users
            SET status = 'active', auto_enabled = 1, expired_at = ?, updated_at = ?
            WHERE chat_id = ?
            """,
            (new_expiry.isoformat(), now_iso, str(chat_id)),
        )
        self._conn.commit()
        updated = self.get_user(chat_id)
        assert updated is not None
        return updated, label, amount

    def reject_payment(self, chat_id: str, now: datetime, reason: str = "") -> bool:
        pending = self.get_pending_payment(chat_id)
        if not pending:
            return False
        self._conn.execute(
            "UPDATE payments SET status = 'rejected', note = ?, reviewed_at = ? WHERE id = ?",
            (reason, now.isoformat(), pending["id"]),
        )
        self._conn.commit()
        return True

    def has_announcement(self, announcement_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sent_announcements WHERE id = ?", (announcement_id,)
        ).fetchone()
        return row is not None

    def mark_announcement_sent(self, announcement_id: str, now: datetime) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO sent_announcements (id, sent_at) VALUES (?, ?)",
            (announcement_id, now.isoformat()),
        )
        self._conn.commit()
