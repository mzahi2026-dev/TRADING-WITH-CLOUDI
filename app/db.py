import os
import re
import sqlite3
import threading
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

def _sanitize_error(e: Exception) -> str:
    """Mask sensitive credentials from exception messages."""
    msg = str(e)
    msg = re.sub(r':([^:@\s]+)@', ':***@', msg)
    msg = re.sub(r'password=(\S+)', 'password=***', msg, flags=re.IGNORECASE)
    return msg

try:
    import psycopg2
    from psycopg2.extras import DictCursor
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

class PGCursorWrapper:
    def __init__(self, cur):
        self._cur = cur
        self._lastrowid = None

    def execute(self, sql, params=None):
        # Handle SQLite PRAGMA table_info(tbl)
        pragma_match = re.match(r'^\s*PRAGMA\s+table_info\((["\w]+)\)', sql.strip(), flags=re.IGNORECASE)
        if pragma_match:
            tbl_name = pragma_match.group(1).strip('"\'').lower()
            pg_sql = (
                "SELECT ordinal_position as cid, column_name as name, data_type as type, "
                "CASE WHEN is_nullable='NO' THEN 1 ELSE 0 END as notnull, "
                "column_default as dflt_value, 0 as pk "
                "FROM information_schema.columns "
                "WHERE table_name = %s ORDER BY ordinal_position"
            )
            self._cur.execute(pg_sql, (tbl_name,))
            return self

        # Handle SQLite sqlite_master table queries
        if re.search(r'\bsqlite_master\b', sql, flags=re.IGNORECASE):
            name_eq_match = re.search(r"name\s*=\s*(?:'([^']+)'|\"([^\"]+)\"|\?|%s)", sql, flags=re.IGNORECASE)
            if name_eq_match:
                tbl_val = name_eq_match.group(1) or name_eq_match.group(2)
                if tbl_val:
                    pg_sql = "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s"
                    self._cur.execute(pg_sql, (tbl_val.lower(),))
                    return self
                elif params and len(params) > 0:
                    pg_sql = "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s"
                    self._cur.execute(pg_sql, (str(params[0]).lower(),))
                    return self

        pg_sql = sql.replace("?", "%s")
        # Handle AUTOINCREMENT
        pg_sql = re.sub(r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT', 'SERIAL PRIMARY KEY', pg_sql, flags=re.IGNORECASE)
        # Handle SQLite rowid pseudo-column -> id in Postgres
        pg_sql = re.sub(r'\browid\b', 'id', pg_sql, flags=re.IGNORECASE)
        # Handle lastrowid for INSERT
        is_insert = pg_sql.strip().upper().startswith("INSERT INTO") and "RETURNING" not in pg_sql.upper()
        if is_insert:
            pg_sql += " RETURNING id"

        if params is not None:
            self._cur.execute(pg_sql, params)
        else:
            self._cur.execute(pg_sql)

        if is_insert:
            try:
                res = self._cur.fetchone()
                if res:
                    self._lastrowid = res[0]
            except Exception:
                self._lastrowid = None
        return self

    def executemany(self, sql, params_list):
        pg_sql = sql.replace("?", "%s")
        return self._cur.executemany(pg_sql, params_list)

    def executescript(self, script_str):
        statements = [s.strip() for s in script_str.split(";") if s.strip()]
        for stmt in statements:
            self.execute(stmt)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def description(self):
        return self._cur.description

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount

    def close(self):
        return self._cur.close()

    def __iter__(self):
        return iter(self._cur)


class PGConnectionWrapper:
    def __init__(self, conn):
        self._conn = conn
        self.row_factory = None  # for sqlite3 compatibility

    def cursor(self):
        return PGCursorWrapper(self._conn.cursor(cursor_factory=DictCursor))

    def execute(self, sql, params=None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def executescript(self, script_str):
        cur = self.cursor()
        cur.executescript(script_str)
        self.commit()
        return cur

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
        self.close()


def get_db():
    db_url = os.getenv("DATABASE_URL", DATABASE_URL).strip()
    if db_url:
        if not HAS_PSYCOPG2:
            raise RuntimeError("DATABASE_URL is configured but psycopg2 is not installed.")
        try:
            # Strip pooler channel binding if present for compatibility
            clean_url = db_url.replace("&channel_binding=require", "").replace("channel_binding=require&", "")
            conn = psycopg2.connect(clean_url, connect_timeout=10)
            return PGConnectionWrapper(conn)
        except Exception as e:
            sanitized_err = _sanitize_error(e)
            import logging
            logging.getLogger("app.db").critical(f"PostgreSQL connection failed: {sanitized_err}")
            raise RuntimeError(f"Database connection failed (fail-closed): {sanitized_err}") from None

    from . import config
    con = sqlite3.connect(str(config.DB_PATH), timeout=20)
    con.row_factory = sqlite3.Row
    return con


def is_postgres():
    return bool(os.getenv("DATABASE_URL", DATABASE_URL).strip())


def market_db():
    from . import config
    con = sqlite3.connect(str(config.DB_PATH), timeout=30)
    con.row_factory = sqlite3.Row
    return con


def has_table(con, name: str) -> bool:
    try:
        if isinstance(con, PGConnectionWrapper):
            cur = con.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s",
                (name.lower(),)
            )
            return cur.fetchone() is not None
        else:
            cur = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
            return cur.fetchone() is not None
    except Exception:
        return False


