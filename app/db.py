import os
import re
import sqlite3
import threading
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
NEON_DEFAULT_URL = "postgresql://neondb_owner:npg_k9Bftzpl4Ujc@ep-square-lake-azznmcx3-pooler.c-3.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"
DATABASE_URL = os.getenv("DATABASE_URL", NEON_DEFAULT_URL).strip()

if DATABASE_URL:
    import psycopg2
    from psycopg2.extras import DictCursor

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
    if DATABASE_URL:
        try:
            # Strip pooler channel binding if present for compatibility
            clean_url = DATABASE_URL.replace("&channel_binding=require", "").replace("channel_binding=require&", "")
            conn = psycopg2.connect(clean_url, connect_timeout=10)
            return PGConnectionWrapper(conn)
        except Exception as e:
            import logging
            logging.getLogger("app.db").warning(f"PostgreSQL connection failed ({e}); falling back to local SQLite.")
    
    from . import config
    con = sqlite3.connect(str(config.DB_PATH), timeout=20)
    con.row_factory = sqlite3.Row
    return con

def is_postgres():
    return bool(DATABASE_URL)
