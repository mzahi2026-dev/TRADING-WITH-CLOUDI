import os
import re
import sqlite3
import threading
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if DATABASE_URL:
    import psycopg2
    from psycopg2.extras import DictCursor

    class PGCursorWrapper:
        def __init__(self, cur):
            self._cur = cur
            self._lastrowid = None

        def execute(self, sql, params=None):
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
        # Strip pooler channel binding if present for compatibility
        clean_url = DATABASE_URL.replace("&channel_binding=require", "").replace("channel_binding=require&", "")
        conn = psycopg2.connect(clean_url)
        return PGConnectionWrapper(conn)
    else:
        from . import config
        con = sqlite3.connect(str(config.DB_PATH), timeout=20)
        con.row_factory = sqlite3.Row
        return con

def is_postgres():
    return bool(DATABASE_URL)
