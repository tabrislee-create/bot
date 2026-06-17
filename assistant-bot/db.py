import sqlite3
import os
from datetime import datetime
DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                event_time DATETIME NOT NULL,
                reminded_1h INTEGER DEFAULT 0,
                custom_remind_time DATETIME DEFAULT NULL,
                custom_reminded INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coffee_beans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                roaster TEXT NOT NULL,
                product TEXT NOT NULL,
                process TEXT,
                roast_level TEXT,
                price TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 舊資料庫升級
        for col, typedef in [
            ("custom_remind_time", "DATETIME DEFAULT NULL"),
            ("custom_reminded",    "INTEGER DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE events ADD COLUMN {col} {typedef}")
            except Exception:
                pass
        conn.commit()
def add_event(title: str, event_time: datetime, custom_remind_time: datetime = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO events (title, event_time, custom_remind_time) VALUES (?, ?, ?)",
            (
                title,
                event_time.strftime("%Y-%m-%d %H:%M:%S"),
                custom_remind_time.strftime("%Y-%m-%d %H:%M:%S") if custom_remind_time else None,
            )
        )
        conn.commit()
        return cur.lastrowid
def get_upcoming_events(days: int = 7) -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM events
            WHERE event_time >= datetime('now', 'localtime')
              AND event_time <= datetime('now', 'localtime', ? || ' days')
            ORDER BY event_time ASC
        """, (str(days),)).fetchall()
        return [dict(r) for r in rows]
def get_today_events() -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM events
            WHERE date(event_time) = date('now', 'localtime')
            ORDER BY event_time ASC
        """).fetchall()
        return [dict(r) for r in rows]
def get_pending_reminders() -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM events
            WHERE reminded_1h = 0
              AND event_time > datetime('now', 'localtime')
              AND event_time <= datetime('now', 'localtime', '+65 minutes')
              AND event_time >= datetime('now', 'localtime', '+55 minutes')
        """).fetchall()
        return [dict(r) for r in rows]
def get_pending_custom_reminders() -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM events
            WHERE custom_reminded = 0
              AND custom_remind_time IS NOT NULL
              AND custom_remind_time <= datetime('now', 'localtime', '+2 minutes')
              AND custom_remind_time >= datetime('now', 'localtime', '-2 minutes')
        """).fetchall()
        return [dict(r) for r in rows]
def mark_reminded(event_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE events SET reminded_1h = 1 WHERE id = ?", (event_id,))
        conn.commit()
def mark_custom_reminded(event_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE events SET custom_reminded = 1 WHERE id = ?", (event_id,))
        conn.commit()
def delete_event(event_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        conn.commit()
        return cur.rowcount > 0
def search_events(keyword: str) -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM events
            WHERE title LIKE ?
              AND event_time >= datetime('now', 'localtime')
            ORDER BY event_time ASC
        """, (f"%{keyword}%",)).fetchall()
        return [dict(r) for r in rows]
# ── 咖啡豆 ────────────────────────────────────────
def add_coffee(roaster: str, product: str, process: str,
               roast_level: str, price: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO coffee_beans
               (roaster, product, process, roast_level, price)
               VALUES (?, ?, ?, ?, ?)""",
            (roaster, product, process, roast_level, price)
        )
        conn.commit()
        return cur.lastrowid
def search_coffee(keyword: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM coffee_beans
               WHERE roaster LIKE ? OR product LIKE ?
               ORDER BY created_at DESC""",
            (f"%{keyword}%", f"%{keyword}%")
        ).fetchall()
        return [dict(r) for r in rows]
def get_recent_coffee(n: int = 5) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM coffee_beans
               ORDER BY created_at DESC LIMIT ?""",
            (n,)
        ).fetchall()
        return [dict(r) for r in rows]
