import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from config import DB_PATH, SUPPORT_USERNAME, BINANCE_ID, INSTAPAY_ADDRESS, CURRENCY


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
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
            """
        )
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
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, full_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name
            """,
            (user_id, username or "", full_name or ""),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row)


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def is_user_banned(user_id: int) -> bool:
    user = get_user(user_id)
    return bool(user and user["is_banned"] == 1)


def set_user_ban(user_id: int, banned: bool) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE users SET is_banned = ? WHERE user_id = ?",
            (1 if banned else 0, user_id),
        )
        if cur.rowcount == 0 and banned:
            conn.execute(
                "INSERT INTO users (user_id, username, full_name, is_banned) VALUES (?, '', 'ÙØ³ØªØ®Ø¯Ù ÙØ­Ø¸ÙØ±', 1)",
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
        total_users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        banned_users = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_banned = 1").fetchone()["c"]
        total_countries = conn.execute("SELECT COUNT(*) AS c FROM countries").fetchone()["c"]
        total_plans = conn.execute("SELECT COUNT(*) AS c FROM plans").fetchone()["c"]
        available_esims = conn.execute("SELECT COUNT(*) AS c FROM esims WHERE status = 'available'").fetchone()["c"]
        sold_esims = conn.execute("SELECT COUNT(*) AS c FROM esims WHERE status = 'sold'").fetchone()["c"]
        pending_topups = conn.execute("SELECT COUNT(*) AS c FROM topup_requests WHERE status = 'pending'").fetchone()["c"]
        total_revenue = conn.execute("SELECT COALESCE(SUM(sold_price), 0) AS s FROM esims WHERE status = 'sold'").fetchone()["s"]
        return {
            "total_users": total_users,
            "banned_users": banned_users,
            "total_countries": total_countries,
            "total_plans": total_plans,
            "available_esims": available_esims,
            "sold_esims": sold_esims,
            "pending_topups": pending_topups,
            "total_revenue": round(float(total_revenue), 2),
        }


def update_user_balance(user_id: int, delta: float) -> Optional[float]:
    with get_connection() as conn:
        row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return None
        new_balance = round(float(row["balance"]) + float(delta), 2)
        if new_balance < 0:
            new_balance = 0.0
        conn.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))
        conn.commit()
        return new_balance


def set_user_balance(user_id: int, new_balance: float) -> Optional[float]:
    with get_connection() as conn:
        row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
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
        cur = conn.execute("DELETE FROM countries WHERE id = ?", (country_id,))
        conn.commit()
        return cur.rowcount > 0


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
        cur = conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
        conn.commit()
        return cur.rowcount > 0


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
            SELECT e.*, p.name AS plan_name, p.price AS plan_price, p.country_id, c.name AS country_name
            FROM esims e
            JOIN plans p ON p.id = e.plan_id
            JOIN countries c ON c.id = p.country_id
            WHERE e.id = ?
            """,
            (esim_id,),
        ).fetchone()
        return dict(row) if row else None


def update_esim(
    esim_id: int,
    item_type: str,
    lpa_code: str = "",
    qr_file_id: str = "",
    qr_local_path: str = "",
    notes: str = "",
) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE esims
            SET item_type = ?, lpa_code = ?, qr_file_id = ?, qr_local_path = ?, notes = ?
            WHERE id = ? AND status = 'available'
            """,
            (item_type, lpa_code.strip(), qr_file_id.strip(), qr_local_path.strip(), notes.strip(), esim_id),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_esim(esim_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM esims WHERE id = ? AND status = 'available'", (esim_id,))
        conn.commit()
        return cur.rowcount > 0


def purchase_esim_atomic(user_id: int, plan_id: int) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Atomically verifies balance, deducts plan price, and marks one available eSIM as sold.
    Returns (success, message_code, esim_and_purchase_dict).
    """
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

            price = round(float(plan["price"]), 2)
            balance = round(float(user["balance"]), 2)
            if balance < price:
                conn.execute("ROLLBACK")
                return False, "insufficient_balance", {"required": price, "balance": balance}

            esim = conn.execute(
                "SELECT * FROM esims WHERE plan_id = ? AND status = 'available' ORDER BY id ASC LIMIT 1",
                (plan_id,),
            ).fetchone()
            if not esim:
                conn.execute("ROLLBACK")
                return False, "out_of_stock", None

            new_balance = round(balance - price, 2)
            conn.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))
            conn.execute(
                """
                UPDATE esims
                SET status = 'sold',
                    sold_to = ?,
                    sold_price = ?,
                    sold_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'available'
                """,
                (user_id, price, esim["id"]),
            )
            conn.commit()

            result = dict(esim)
            result["plan_name"] = plan["name"]
            result["plan_description"] = plan["description"]
            result["country_name"] = plan["country_name"]
            result["paid_price"] = price
            result["new_balance"] = new_balance
            return True, "success", result
        except Exception as exc:
            conn.execute("ROLLBACK")
            return False, f"error:{exc}", None


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