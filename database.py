# -*- coding: utf-8 -*-
import os
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import config
from config import DB_PATH, SUPPORT_USERNAME, BINANCE_ID, INSTAPAY_ADDRESS, CURRENCY

_INITIAL_DB_PATH = DB_PATH
_WAL_INITIALIZED_PATHS = set()


def _current_db_path() -> str:
    if DB_PATH != _INITIAL_DB_PATH:
        return str(DB_PATH)
    return str(getattr(config, "DB_PATH", "") or DB_PATH or "esim_store.db")


def get_connection() -> sqlite3.Connection:
    db_path = _current_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    if db_path not in _WAL_INITIALIZED_PATHS:
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            _WAL_INITIALIZED_PATHS.add(db_path)
        except sqlite3.OperationalError:
            pass
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                balance REAL NOT NULL DEFAULT 0.0,
                is_banned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS countries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                country_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                price REAL NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (country_id) REFERENCES countries(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS esims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                item_type TEXT NOT NULL CHECK(item_type IN ('lpa', 'qr')),
                lpa_code TEXT DEFAULT '',
                qr_file_id TEXT DEFAULT '',
                qr_local_path TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'available' CHECK(status IN ('available', 'sold')),
                sold_to INTEGER,
                sold_price REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                sold_at TEXT,
                FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS topup_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                method TEXT NOT NULL CHECK(method IN ('binance', 'instapay')),
                amount REAL NOT NULL,
                proof_text TEXT DEFAULT '',
                proof_file_id TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                processed_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS refund_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                esim_id INTEGER NOT NULL,
                reason TEXT DEFAULT '',
                video_file_id TEXT NOT NULL,
                video_type TEXT NOT NULL DEFAULT 'video' CHECK(video_type IN ('video', 'video_note', 'document')),
                refund_amount REAL NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'reviewed')),
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (esim_id) REFERENCES esims(id)
            );

            CREATE TABLE IF NOT EXISTS stock_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, plan_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_plans_country ON plans(country_id);
            CREATE INDEX IF NOT EXISTS idx_esims_plan_status ON esims(plan_id, status, id);
            CREATE INDEX IF NOT EXISTS idx_esims_sold_to ON esims(sold_to, status, sold_at DESC);
            CREATE INDEX IF NOT EXISTS idx_esims_status_sold_at ON esims(status, sold_at DESC);
            CREATE INDEX IF NOT EXISTS idx_esims_lpa_status ON esims(lpa_code, status);
            CREATE INDEX IF NOT EXISTS idx_topup_status_user ON topup_requests(status, user_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_topup_user_id ON topup_requests(user_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_refund_status_esim ON refund_requests(status, esim_id);
            CREATE INDEX IF NOT EXISTS idx_refund_user_id ON refund_requests(user_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_stock_alerts_plan ON stock_alerts(plan_id);
            """
        )
        # Clean up any corrupted mojibake values from previous runs
        conn.execute("DELETE FROM settings WHERE value LIKE '%\\u00d8%' OR value LIKE '%\\u00d9%' OR value LIKE '%\\u00f0%';".encode("utf-8").decode("unicode_escape"))
        # Seed default settings from config/.env if not present
        default_settings = {
            "support_username": SUPPORT_USERNAME,
            "binance_id": BINANCE_ID,
            "instapay_address": INSTAPAY_ADDRESS,
            "currency": CURRENCY,
        }
        for k, v in default_settings.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (k, v),
            )
        conn.commit()


def _cleanup_unreferenced_qr_files(conn: sqlite3.Connection, paths: List[str]) -> None:
    """Safely deletes local QR image files that are no longer referenced by any eSIM row."""
    for raw_path in set(p.strip() for p in paths if p and p.strip()):
        still_used = conn.execute(
            "SELECT 1 FROM esims WHERE qr_local_path = ? LIMIT 1",
            (raw_path,),
        ).fetchone()
        if still_used:
            continue
        try:
            target = Path(raw_path)
            if target.is_file():
                target.unlink(missing_ok=True)
        except OSError:
            pass


# ----------------- Settings Operations -----------------
def get_setting(key: str, default: str = "") -> str:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value.strip()),
        )
        conn.commit()


# ----------------- User Operations -----------------
def upsert_user(user_id: int, username: Optional[str], full_name: Optional[str]) -> Dict[str, Any]:
    clean_username = (username or "").strip().lstrip("@")
    clean_name = (full_name or "").strip()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, full_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name
            WHERE users.username IS NOT excluded.username
               OR users.full_name IS NOT excluded.full_name
            """,
            (user_id, clean_username, clean_name),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row)


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def is_user_banned(user_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return bool(row and row["is_banned"] == 1)


def set_user_ban(user_id: int, banned: bool) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE users SET is_banned = ? WHERE user_id = ?",
            (1 if banned else 0, user_id),
        )
        if cur.rowcount == 0 and banned:
            conn.execute(
                "INSERT INTO users (user_id, username, full_name, is_banned) VALUES (?, '', 'مستخدم محظور', 1)",
                (user_id,),
            )
            conn.commit()
            return True
        conn.commit()
        return cur.rowcount > 0


def get_banned_users() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM users WHERE is_banned = 1 ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_all_active_users() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM users WHERE is_banned = 0 ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]


def get_stats() -> Dict[str, Any]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS total_users,
                (SELECT COUNT(*) FROM users WHERE is_banned = 1) AS banned_users,
                (SELECT COUNT(*) FROM countries) AS total_countries,
                (SELECT COUNT(*) FROM plans) AS total_plans,
                (SELECT COUNT(*) FROM esims WHERE status = 'available') AS available_esims,
                (SELECT COUNT(*) FROM esims WHERE status = 'sold') AS sold_esims,
                (SELECT COALESCE(SUM(sold_price), 0) FROM esims WHERE status = 'sold') AS total_revenue,
                (SELECT COUNT(*) FROM topup_requests WHERE status = 'pending') AS pending_topups,
                (SELECT COUNT(*) FROM refund_requests WHERE status = 'pending') AS pending_refunds,
                (SELECT COUNT(*) FROM stock_alerts) AS active_stock_alerts
            """
        ).fetchone()
        return {
            "total_users": int(row["total_users"] or 0),
            "banned_users": int(row["banned_users"] or 0),
            "total_countries": int(row["total_countries"] or 0),
            "total_plans": int(row["total_plans"] or 0),
            "available_esims": int(row["available_esims"] or 0),
            "sold_esims": int(row["sold_esims"] or 0),
            "pending_topups": int(row["pending_topups"] or 0),
            "pending_refunds": int(row["pending_refunds"] or 0),
            "active_stock_alerts": int(row["active_stock_alerts"] or 0),
            "total_revenue": round(float(row["total_revenue"] or 0.0), 2),
        }


def get_revenue_analytics() -> Dict[str, Any]:
    with get_connection() as conn:
        summary = conn.execute(
            """
            SELECT
                COUNT(CASE WHEN status = 'sold' THEN 1 END) AS sold_esims,
                COUNT(CASE WHEN status = 'available' THEN 1 END) AS available_esims,
                COALESCE(SUM(CASE WHEN status = 'sold' THEN sold_price ELSE 0 END), 0) AS total_revenue,
                COUNT(CASE WHEN status = 'sold' AND sold_at >= date('now') THEN 1 END) AS today_sold,
                COALESCE(SUM(CASE WHEN status = 'sold' AND sold_at >= date('now') THEN sold_price ELSE 0 END), 0) AS today_revenue,
                COUNT(CASE WHEN status = 'sold' AND sold_at >= datetime('now', '-7 days') THEN 1 END) AS week_sold,
                COALESCE(SUM(CASE WHEN status = 'sold' AND sold_at >= datetime('now', '-7 days') THEN sold_price ELSE 0 END), 0) AS week_revenue,
                COUNT(CASE WHEN status = 'sold' AND sold_at >= datetime('now', '-30 days') THEN 1 END) AS month_sold,
                COALESCE(SUM(CASE WHEN status = 'sold' AND sold_at >= datetime('now', '-30 days') THEN sold_price ELSE 0 END), 0) AS month_revenue
            FROM esims
            """
        ).fetchone()

        topups = conn.execute(
            """
            SELECT
                COUNT(CASE WHEN status = 'approved' THEN 1 END) AS approved_count,
                COALESCE(SUM(CASE WHEN status = 'approved' THEN amount ELSE 0 END), 0) AS approved_total,
                COALESCE(SUM(CASE WHEN status = 'approved' AND method = 'binance' THEN amount ELSE 0 END), 0) AS binance_total,
                COALESCE(SUM(CASE WHEN status = 'approved' AND method = 'instapay' THEN amount ELSE 0 END), 0) AS instapay_total
            FROM topup_requests
            """
        ).fetchone()

        wallets_row = conn.execute(
            """
            SELECT
                COALESCE(SUM(balance), 0) AS total_balances,
                (SELECT COUNT(*) FROM stock_alerts) AS active_alerts
            FROM users
            """
        ).fetchone()

        top_plans_rows = conn.execute(
            """
            SELECT
                p.id AS plan_id,
                p.name AS plan_name,
                c.name AS country_name,
                COUNT(CASE WHEN e.status = 'sold' THEN 1 END) AS sold_count,
                COALESCE(SUM(CASE WHEN e.status = 'sold' THEN e.sold_price ELSE 0 END), 0) AS revenue,
                COUNT(CASE WHEN e.status = 'available' THEN 1 END) AS available_count
            FROM plans p
            JOIN countries c ON c.id = p.country_id
            LEFT JOIN esims e ON e.plan_id = p.id
            GROUP BY p.id
            HAVING sold_count > 0
            ORDER BY revenue DESC, sold_count DESC
            LIMIT 5
            """
        ).fetchall()

        return {
            "sold_esims": int(summary["sold_esims"] or 0),
            "available_esims": int(summary["available_esims"] or 0),
            "total_revenue": round(float(summary["total_revenue"] or 0.0), 2),
            "today_sold": int(summary["today_sold"] or 0),
            "today_revenue": round(float(summary["today_revenue"] or 0.0), 2),
            "week_sold": int(summary["week_sold"] or 0),
            "week_revenue": round(float(summary["week_revenue"] or 0.0), 2),
            "month_sold": int(summary["month_sold"] or 0),
            "month_revenue": round(float(summary["month_revenue"] or 0.0), 2),
            "approved_topups_count": int(topups["approved_count"] or 0),
            "approved_topups_total": round(float(topups["approved_total"] or 0.0), 2),
            "binance_topups_total": round(float(topups["binance_total"] or 0.0), 2),
            "instapay_topups_total": round(float(topups["instapay_total"] or 0.0), 2),
            "total_user_balances": round(float(wallets_row["total_balances"] or 0.0), 2),
            "active_stock_alerts": int(wallets_row["active_alerts"] or 0),
            "top_plans": [dict(r) for r in top_plans_rows],
        }


def find_user_admin(identifier: str) -> Optional[Dict[str, Any]]:
    """Finds a user by numeric Telegram ID or @username and includes summary stats."""
    q = (identifier or "").strip()
    if not q:
        return None
    with get_connection() as conn:
        if q.isdigit():
            user = conn.execute("SELECT * FROM users WHERE user_id = ?", (int(q),)).fetchone()
        else:
            clean_uname = q.lstrip("@")
            user = conn.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE ORDER BY created_at DESC LIMIT 1",
                (clean_uname,),
            ).fetchone()
        if not user:
            return None

        uid = int(user["user_id"])
        agg = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM esims WHERE sold_to = ? AND status = 'sold') AS purchases_count,
                (SELECT COALESCE(SUM(sold_price), 0) FROM esims WHERE sold_to = ? AND status = 'sold') AS total_spent,
                (SELECT COUNT(*) FROM topup_requests WHERE user_id = ? AND status = 'approved') AS approved_topups_count,
                (SELECT COALESCE(SUM(amount), 0) FROM topup_requests WHERE user_id = ? AND status = 'approved') AS approved_topups_sum,
                (SELECT COUNT(*) FROM topup_requests WHERE user_id = ? AND status = 'pending') AS pending_topups_count,
                (SELECT COUNT(*) FROM refund_requests WHERE user_id = ?) AS refunds_count,
                (SELECT COUNT(*) FROM stock_alerts WHERE user_id = ?) AS stock_alerts_count
            """,
            (uid, uid, uid, uid, uid, uid, uid),
        ).fetchone()

        result = dict(user)
        result["purchases_count"] = int(agg["purchases_count"] or 0)
        result["total_spent"] = round(float(agg["total_spent"] or 0.0), 2)
        result["approved_topups_count"] = int(agg["approved_topups_count"] or 0)
        result["approved_topups_sum"] = round(float(agg["approved_topups_sum"] or 0.0), 2)
        result["pending_topups_count"] = int(agg["pending_topups_count"] or 0)
        result["refunds_count"] = int(agg["refunds_count"] or 0)
        result["stock_alerts_count"] = int(agg["stock_alerts_count"] or 0)
        return result


def update_user_balance(user_id: int, delta: float) -> Optional[float]:
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        new_balance = round(float(row["balance"]) + float(delta), 2)
        if new_balance < 0:
            new_balance = 0.0
        conn.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))
        conn.commit()
        return new_balance


def set_user_balance(user_id: int, new_balance: float) -> Optional[float]:
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        clean_bal = round(max(0.0, float(new_balance)), 2)
        conn.execute("UPDATE users SET balance = ? WHERE user_id = ?", (clean_bal, user_id))
        conn.commit()
        return clean_bal


# ----------------- Country Operations -----------------
def add_country(name: str) -> Optional[int]:
    with get_connection() as conn:
        try:
            cur = conn.execute("INSERT INTO countries (name) VALUES (?)", (name.strip(),))
            conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def get_countries() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT c.*,
                   COUNT(DISTINCT p.id) AS plans_count,
                   SUM(CASE WHEN e.status = 'available' THEN 1 ELSE 0 END) AS available_stock
            FROM countries c
            LEFT JOIN plans p ON p.country_id = c.id
            LEFT JOIN esims e ON e.plan_id = p.id
            GROUP BY c.id
            ORDER BY c.id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_country(country_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM countries WHERE id = ?", (country_id,)).fetchone()
        return dict(row) if row else None


def update_country(country_id: int, new_name: str) -> bool:
    with get_connection() as conn:
        try:
            cur = conn.execute("UPDATE countries SET name = ? WHERE id = ?", (new_name.strip(), country_id))
            conn.commit()
            return cur.rowcount > 0
        except sqlite3.IntegrityError:
            return False


def delete_country(country_id: int) -> bool:
    with get_connection() as conn:
        try:
            qr_rows = conn.execute(
                """
                SELECT e.qr_local_path
                FROM esims e
                JOIN plans p ON p.id = e.plan_id
                WHERE p.country_id = ? AND e.qr_local_path != ''
                """,
                (country_id,),
            ).fetchall()
            paths = [r["qr_local_path"] for r in qr_rows]
            cur = conn.execute("DELETE FROM countries WHERE id = ?", (country_id,))
            conn.commit()
            if cur.rowcount > 0 and paths:
                _cleanup_unreferenced_qr_files(conn, paths)
            return cur.rowcount > 0
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return False


# ----------------- Plan Operations -----------------
def add_plan(country_id: int, name: str, description: str, price: float) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO plans (country_id, name, description, price) VALUES (?, ?, ?, ?)",
            (country_id, name.strip(), description.strip(), round(float(price), 2)),
        )
        conn.commit()
        return cur.lastrowid


def get_plans_by_country(country_id: int) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   c.name AS country_name,
                   SUM(CASE WHEN e.status = 'available' THEN 1 ELSE 0 END) AS available_count
            FROM plans p
            JOIN countries c ON c.id = p.country_id
            LEFT JOIN esims e ON e.plan_id = p.id
            WHERE p.country_id = ?
            GROUP BY p.id
            ORDER BY p.price ASC, p.id ASC
            """,
            (country_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_plan(plan_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT p.*,
                   c.name AS country_name,
                   SUM(CASE WHEN e.status = 'available' THEN 1 ELSE 0 END) AS available_count
            FROM plans p
            JOIN countries c ON c.id = p.country_id
            LEFT JOIN esims e ON e.plan_id = p.id
            WHERE p.id = ?
            GROUP BY p.id
            """,
            (plan_id,),
        ).fetchone()
        return dict(row) if row else None


def update_plan(plan_id: int, name: str, description: str, price: float) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE plans SET name = ?, description = ?, price = ? WHERE id = ?",
            (name.strip(), description.strip(), round(float(price), 2), plan_id),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_plan(plan_id: int) -> bool:
    with get_connection() as conn:
        try:
            qr_rows = conn.execute(
                "SELECT qr_local_path FROM esims WHERE plan_id = ? AND qr_local_path != ''",
                (plan_id,),
            ).fetchall()
            paths = [r["qr_local_path"] for r in qr_rows]
            cur = conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
            conn.commit()
            if cur.rowcount > 0 and paths:
                _cleanup_unreferenced_qr_files(conn, paths)
            return cur.rowcount > 0
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return False


# ----------------- Stock Waitlist Alert Operations -----------------
def has_stock_alert(user_id: int, plan_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM stock_alerts WHERE user_id = ? AND plan_id = ? LIMIT 1",
            (user_id, plan_id),
        ).fetchone()
        return row is not None


def toggle_stock_alert(user_id: int, plan_id: int) -> bool:
    """Toggles a user's restock alert for a plan. Returns True if subscribed, False if removed."""
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM stock_alerts WHERE user_id = ? AND plan_id = ?",
            (user_id, plan_id),
        ).fetchone()
        if existing:
            conn.execute("DELETE FROM stock_alerts WHERE id = ?", (existing["id"],))
            conn.commit()
            return False
        conn.execute(
            "INSERT OR IGNORE INTO stock_alerts (user_id, plan_id) VALUES (?, ?)",
            (user_id, plan_id),
        )
        conn.commit()
        return True


def get_plan_alert_count(plan_id: int) -> int:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM stock_alerts sa
            JOIN users u ON u.user_id = sa.user_id
            WHERE sa.plan_id = ? AND u.is_banned = 0
            """,
            (plan_id,),
        ).fetchone()
        return int(row["c"] or 0)


def get_plan_alert_subscribers(plan_id: int) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT u.*
            FROM stock_alerts sa
            JOIN users u ON u.user_id = sa.user_id
            WHERE sa.plan_id = ? AND u.is_banned = 0
            ORDER BY sa.id ASC
            """,
            (plan_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def clear_plan_alerts(plan_id: int, user_ids: Optional[List[int]] = None) -> int:
    with get_connection() as conn:
        if user_ids is None:
            cur = conn.execute("DELETE FROM stock_alerts WHERE plan_id = ?", (plan_id,))
        elif not user_ids:
            return 0
        else:
            placeholders = ",".join("?" for _ in user_ids)
            cur = conn.execute(
                f"DELETE FROM stock_alerts WHERE plan_id = ? AND user_id IN ({placeholders})",
                [plan_id, *user_ids],
            )
        conn.commit()
        return cur.rowcount


# ----------------- eSIM Inventory Operations -----------------
def add_esim(
    plan_id: int,
    item_type: str,
    lpa_code: str = "",
    qr_file_id: str = "",
    qr_local_path: str = "",
    notes: str = "",
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO esims (plan_id, item_type, lpa_code, qr_file_id, qr_local_path, notes, status)
            VALUES (?, ?, ?, ?, ?, ?, 'available')
            """,
            (plan_id, item_type, lpa_code.strip(), qr_file_id.strip(), qr_local_path.strip(), notes.strip()),
        )
        conn.commit()
        return cur.lastrowid


def add_lpa_esims_bulk(plan_id: int, lpa_codes: List[str]) -> Tuple[int, int]:
    """
    Inserts multiple LPA codes in one transaction while skipping empty lines,
    duplicate lines in the same batch, and codes already available in inventory.
    Returns (added_count, skipped_duplicates).
    """
    cleaned = [c.strip() for c in lpa_codes if c and c.strip()]
    if not cleaned:
        return 0, 0

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing_rows = conn.execute(
            "SELECT lpa_code FROM esims WHERE status = 'available' AND lpa_code != ''"
        ).fetchall()
        existing_codes = {r["lpa_code"].strip() for r in existing_rows}

        to_insert: List[Tuple[int, str]] = []
        skipped = 0
        for code in cleaned:
            if code in existing_codes:
                skipped += 1
                continue
            existing_codes.add(code)
            to_insert.append((plan_id, code))

        if to_insert:
            conn.executemany(
                """
                INSERT INTO esims (plan_id, item_type, lpa_code, qr_file_id, qr_local_path, notes, status)
                VALUES (?, 'lpa', ?, '', '', '', 'available')
                """,
                to_insert,
            )
        conn.commit()
        return len(to_insert), skipped


def get_available_esims(plan_id: int) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT e.*, p.name AS plan_name, p.price AS plan_price, c.name AS country_name
            FROM esims e
            JOIN plans p ON p.id = e.plan_id
            JOIN countries c ON c.id = p.country_id
            WHERE e.plan_id = ? AND e.status = 'available'
            ORDER BY e.id ASC
            """,
            (plan_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_esim(esim_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT e.*,
                   p.name AS plan_name,
                   p.description AS plan_description,
                   p.price AS plan_price,
                   p.country_id,
                   c.name AS country_name,
                   u.username AS buyer_username,
                   u.full_name AS buyer_full_name
            FROM esims e
            JOIN plans p ON p.id = e.plan_id
            JOIN countries c ON c.id = p.country_id
            LEFT JOIN users u ON u.user_id = e.sold_to
            WHERE e.id = ?
            """,
            (esim_id,),
        ).fetchone()
        return dict(row) if row else None


def get_esim_refunds(esim_id: int) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM refund_requests WHERE esim_id = ? ORDER BY id DESC",
            (esim_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_sold_esims(limit: int = 15, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM esims WHERE status = 'sold'").fetchone()["c"]
        rows = conn.execute(
            """
            SELECT e.*,
                   p.name AS plan_name,
                   c.name AS country_name,
                   u.username AS buyer_username,
                   u.full_name AS buyer_full_name
            FROM esims e
            JOIN plans p ON p.id = e.plan_id
            JOIN countries c ON c.id = p.country_id
            LEFT JOIN users u ON u.user_id = e.sold_to
            WHERE e.status = 'sold'
            ORDER BY e.sold_at DESC, e.id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows], int(total or 0)


def update_esim(
    esim_id: int,
    item_type: str,
    lpa_code: str = "",
    qr_file_id: str = "",
    qr_local_path: str = "",
    notes: str = "",
) -> bool:
    new_local_path = qr_local_path.strip()
    with get_connection() as conn:
        old_row = conn.execute(
            "SELECT qr_local_path FROM esims WHERE id = ? AND status = 'available'",
            (esim_id,),
        ).fetchone()
        if not old_row:
            return False
        old_path = (old_row["qr_local_path"] or "").strip()
        cur = conn.execute(
            """
            UPDATE esims
            SET item_type = ?, lpa_code = ?, qr_file_id = ?, qr_local_path = ?, notes = ?
            WHERE id = ? AND status = 'available'
            """,
            (item_type, lpa_code.strip(), qr_file_id.strip(), new_local_path, notes.strip(), esim_id),
        )
        conn.commit()
        updated = cur.rowcount > 0
        if updated and old_path and old_path != new_local_path:
            _cleanup_unreferenced_qr_files(conn, [old_path])
        return updated


def delete_esim(esim_id: int) -> bool:
    with get_connection() as conn:
        old_row = conn.execute(
            "SELECT qr_local_path FROM esims WHERE id = ? AND status = 'available'",
            (esim_id,),
        ).fetchone()
        if not old_row:
            return False
        old_path = (old_row["qr_local_path"] or "").strip()
        cur = conn.execute("DELETE FROM esims WHERE id = ? AND status = 'available'", (esim_id,))
        conn.commit()
        deleted = cur.rowcount > 0
        if deleted and old_path:
            _cleanup_unreferenced_qr_files(conn, [old_path])
        return deleted


def purchase_esims_atomic(
    user_id: int,
    plan_id: int,
    quantity: int = 1,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Atomically verifies user eligibility, available stock for `quantity` eSIMs,
    deducts `price * quantity`, and marks `quantity` eSIMs as sold with strict
    rowcount verification.
    """
    qty = int(quantity)
    if qty < 1:
        return False, "invalid_quantity", None

    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not user:
                conn.execute("ROLLBACK")
                return False, "user_not_found", None
            if user["is_banned"] == 1:
                conn.execute("ROLLBACK")
                return False, "user_banned", None

            plan = conn.execute(
                """
                SELECT p.*, c.name AS country_name
                FROM plans p
                JOIN countries c ON c.id = p.country_id
                WHERE p.id = ?
                """,
                (plan_id,),
            ).fetchone()
            if not plan:
                conn.execute("ROLLBACK")
                return False, "plan_not_found", None

            unit_price = round(float(plan["price"]), 2)
            total_price = round(unit_price * qty, 2)
            balance = round(float(user["balance"]), 2)

            esims = conn.execute(
                "SELECT * FROM esims WHERE plan_id = ? AND status = 'available' ORDER BY id ASC LIMIT ?",
                (plan_id, qty),
            ).fetchall()
            if len(esims) < qty:
                conn.execute("ROLLBACK")
                return False, "out_of_stock", {"available": len(esims), "requested": qty}

            if balance < total_price:
                conn.execute("ROLLBACK")
                return False, "insufficient_balance", {
                    "required": total_price,
                    "balance": balance,
                    "quantity": qty,
                }

            new_balance = round(balance - total_price, 2)
            cur_bal = conn.execute(
                "UPDATE users SET balance = ? WHERE user_id = ? AND balance >= ?",
                (new_balance, user_id, round(total_price - 1e-6, 6)),
            )
            if cur_bal.rowcount != 1:
                conn.execute("ROLLBACK")
                return False, "insufficient_balance", {
                    "required": total_price,
                    "balance": balance,
                    "quantity": qty,
                }

            esim_ids = [int(e["id"]) for e in esims]
            for eid in esim_ids:
                cur_esim = conn.execute(
                    """
                    UPDATE esims
                    SET status = 'sold',
                        sold_to = ?,
                        sold_price = ?,
                        sold_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'available'
                    """,
                    (user_id, unit_price, eid),
                )
                if cur_esim.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return False, "out_of_stock", {"available": 0, "requested": qty}

            conn.execute(
                "DELETE FROM stock_alerts WHERE user_id = ? AND plan_id = ?",
                (user_id, plan_id),
            )

            placeholders = ",".join("?" for _ in esim_ids)
            updated_rows = conn.execute(
                f"SELECT * FROM esims WHERE id IN ({placeholders}) ORDER BY id ASC",
                esim_ids,
            ).fetchall()
            conn.commit()

            items: List[Dict[str, Any]] = []
            for row in updated_rows:
                item = dict(row)
                item["plan_name"] = plan["name"]
                item["plan_description"] = plan["description"]
                item["country_name"] = plan["country_name"]
                item["paid_price"] = unit_price
                item["new_balance"] = new_balance
                items.append(item)

            summary = dict(items[0])
            summary["items"] = items
            summary["quantity"] = qty
            summary["unit_price"] = unit_price
            summary["paid_price"] = total_price
            summary["total_paid"] = total_price
            summary["new_balance"] = new_balance
            return True, "success", summary
        except Exception as exc:
            conn.execute("ROLLBACK")
            return False, f"error:{exc}", None


def purchase_esim_atomic(user_id: int, plan_id: int) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Single-item atomic purchase wrapper preserved for full backward compatibility."""
    return purchase_esims_atomic(user_id, plan_id, quantity=1)


def get_user_purchases(user_id: int) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT e.*, p.name AS plan_name, p.description AS plan_description, c.name AS country_name
            FROM esims e
            JOIN plans p ON p.id = e.plan_id
            JOIN countries c ON c.id = p.country_id
            WHERE e.sold_to = ? AND e.status = 'sold'
            ORDER BY e.sold_at DESC, e.id DESC
            """,
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ----------------- Top-Up Operations -----------------
def create_topup_request(
    user_id: int,
    method: str,
    amount: float,
    proof_text: str = "",
    proof_file_id: str = "",
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO topup_requests (user_id, method, amount, proof_text, proof_file_id, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (user_id, method, round(float(amount), 2), proof_text.strip(), proof_file_id.strip()),
        )
        conn.commit()
        return cur.lastrowid


def get_topup_request(req_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT t.*, u.username, u.full_name, u.balance
            FROM topup_requests t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.id = ?
            """,
            (req_id,),
        ).fetchone()
        return dict(row) if row else None


def get_user_topups(user_id: int) -> List[Dict[str, Any]]:
    """Returns all top-up requests submitted by a user, newest first."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM topup_requests
            WHERE user_id = ?
            ORDER BY id DESC
            """,
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_pending_topups() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT t.*, u.username, u.full_name
            FROM topup_requests t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.status = 'pending'
            ORDER BY t.id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def process_topup_request(req_id: int, approve: bool) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            req = conn.execute("SELECT * FROM topup_requests WHERE id = ?", (req_id,)).fetchone()
            if not req:
                conn.execute("ROLLBACK")
                return False, "not_found", None
            if req["status"] != "pending":
                conn.execute("ROLLBACK")
                return False, "already_processed", dict(req)

            new_status = "approved" if approve else "rejected"
            conn.execute(
                "UPDATE topup_requests SET status = ?, processed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_status, req_id),
            )
            new_balance = None
            if approve:
                user = conn.execute("SELECT balance FROM users WHERE user_id = ?", (req["user_id"],)).fetchone()
                current_bal = float(user["balance"]) if user else 0.0
                new_balance = round(current_bal + float(req["amount"]), 2)
                conn.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, req["user_id"]))

            conn.commit()
            result = dict(req)
            result["status"] = new_status
            result["new_balance"] = new_balance
            return True, new_status, result
        except Exception as exc:
            conn.execute("ROLLBACK")
            return False, f"error:{exc}", None


# ----------------- Refund Request Operations -----------------
def get_user_refunds(user_id: int) -> List[Dict[str, Any]]:
    """All refund requests submitted by a user, newest first."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.*, p.name AS plan_name, c.name AS country_name
            FROM refund_requests r
            JOIN esims e ON e.id = r.esim_id
            JOIN plans p ON p.id = e.plan_id
            JOIN countries c ON c.id = p.country_id
            WHERE r.user_id = ?
            ORDER BY r.id DESC
            """,
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def create_refund_request(
    user_id: int,
    esim_id: int,
    reason: str,
    video_file_id: str,
    video_type: str = "video",
    refund_amount: float = 0.0,
) -> Optional[int]:
    """
    Creates a pending refund request. Returns the request id, or None when the
    same eSIM already has a request still waiting for review (spam guard only;
    previously reviewed requests do not block a new one).
    """
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM refund_requests WHERE esim_id = ? AND status = 'pending'",
            (esim_id,),
        ).fetchone()
        if existing:
            return None
        cur = conn.execute(
            """
            INSERT INTO refund_requests (user_id, esim_id, reason, video_file_id, video_type, refund_amount, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (user_id, esim_id, reason.strip(), video_file_id.strip(), video_type, round(float(refund_amount), 2)),
        )
        conn.commit()
        return cur.lastrowid


def get_refund_request(req_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT r.*, u.username, u.full_name, u.balance,
                   e.sold_price, e.item_type, p.name AS plan_name, c.name AS country_name
            FROM refund_requests r
            JOIN users u ON u.user_id = r.user_id
            JOIN esims e ON e.id = r.esim_id
            JOIN plans p ON p.id = e.plan_id
            JOIN countries c ON c.id = p.country_id
            WHERE r.id = ?
            """,
            (req_id,),
        ).fetchone()
        return dict(row) if row else None


def get_pending_refunds() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.*, u.username, u.full_name,
                   p.name AS plan_name, c.name AS country_name
            FROM refund_requests r
            JOIN users u ON u.user_id = r.user_id
            JOIN esims e ON e.id = r.esim_id
            JOIN plans p ON p.id = e.plan_id
            JOIN countries c ON c.id = p.country_id
            WHERE r.status = 'pending'
            ORDER BY r.id ASC
            """,
        ).fetchall()
        return [dict(r) for r in rows]


def mark_refund_reviewed(req_id: int) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Marks a pending refund request as reviewed. This changes NO balances and
    issues NO refunds; any compensation decision stays with the store owner
    (for example via the existing manual balance tool).
    Returns (success, status, request_dict).
    """
    with get_connection() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            req = conn.execute("SELECT * FROM refund_requests WHERE id = ?", (req_id,)).fetchone()
            if not req:
                conn.execute("ROLLBACK")
                return False, "not_found", None
            if req["status"] != "pending":
                conn.execute("ROLLBACK")
                return False, "already_processed", dict(req)
            conn.execute(
                "UPDATE refund_requests SET status = 'reviewed', reviewed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (req_id,),
            )
            conn.commit()
            result = dict(req)
            result["status"] = "reviewed"
            return True, "reviewed", result
        except Exception as exc:
            conn.execute("ROLLBACK")
            return False, f"error:{exc}", None


# ----------------- Admin CSV Export Queries -----------------
def get_sales_export_rows() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                e.id AS esim_id,
                c.name AS country_name,
                p.name AS plan_name,
                e.item_type,
                e.sold_price,
                e.sold_to AS buyer_id,
                COALESCE(u.username, '') AS buyer_username,
                COALESCE(u.full_name, '') AS buyer_full_name,
                e.sold_at,
                e.created_at
            FROM esims e
            JOIN plans p ON p.id = e.plan_id
            JOIN countries c ON c.id = p.country_id
            LEFT JOIN users u ON u.user_id = e.sold_to
            WHERE e.status = 'sold'
            ORDER BY e.sold_at DESC, e.id DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_inventory_export_rows() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                e.id AS esim_id,
                e.status,
                c.name AS country_name,
                p.name AS plan_name,
                p.price AS current_plan_price,
                e.item_type,
                e.lpa_code,
                e.notes,
                e.sold_to AS buyer_id,
                e.sold_price,
                e.created_at,
                e.sold_at
            FROM esims e
            JOIN plans p ON p.id = e.plan_id
            JOIN countries c ON c.id = p.country_id
            ORDER BY e.id DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_users_export_rows() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                u.user_id,
                u.username,
                u.full_name,
                u.balance,
                u.is_banned,
                u.created_at,
                COUNT(CASE WHEN e.status = 'sold' THEN 1 END) AS purchases_count,
                COALESCE(SUM(CASE WHEN e.status = 'sold' THEN e.sold_price ELSE 0 END), 0) AS total_spent
            FROM users u
            LEFT JOIN esims e ON e.sold_to = u.user_id
            GROUP BY u.user_id
            ORDER BY u.created_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
