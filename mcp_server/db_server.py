"""
Backend database server with a stable tool interface for the agent and GUI.

Supports a legacy SQLite backend and a hosted MySQL backend selected from
configuration. Mutating operations use deferred commits with SAVEPOINTs so the
user can undo changes before permanently saving them.
"""

from __future__ import annotations

import csv
import json
import json as _stdlib_json
import logging
import os
import re
import sqlite3
import time
from collections import OrderedDict
from datetime import date, datetime
from decimal import Decimal
from typing import Any, TYPE_CHECKING


def _json_default(value):
    """Coerce DB-native types JSON does not handle out of the box."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(
        f"Object of type {type(value).__name__} is not JSON serializable"
    )


def _dumps(obj, **kwargs) -> str:
    """``json.dumps`` with our default encoder pre-wired."""
    kwargs.setdefault("default", _json_default)
    return _stdlib_json.__dict__["dumps"](obj, **kwargs)


class ConnectionLostError(RuntimeError):
    """Raised when the DB connection died while uncommitted work existed.

    The caller's pending savepoints are physically gone (server-side
    transaction was aborted), so any in-memory ``_savepoint_stack`` is
    now stale and must be cleared.
    """

try:
    import pymysql
except ImportError:  # pragma: no cover - exercised only when dependency missing
    pymysql = None

import sqlparse
from sqlparse import sql as sqlparse_sql
from sqlparse import tokens as sqlparse_tokens

from .schema_cache import SchemaCache

if TYPE_CHECKING:
    from agent.modes import Mode

# Rows shown to the LLM in run_query responses. Full result stays in the
# server-side cache for the GUI to render; the LLM only sees this preview.
PREVIEW_ROWS = 10
EXPORT_PREVIEW_ROWS = 5
RESULT_CACHE_SIZE = 20
EXPORTS_DIR = "exports"

# Hard cap on rows materialized from a single SELECT. Anything beyond this
# is dropped before json.dumps so a careless ``SELECT * FROM huge_table``
# cannot OOM the Flask process.
MAX_RESULT_ROWS = 5000

# Per-query timeout (ms) enforced server-side on MySQL via
# ``MAX_EXECUTION_TIME``. SQLite has no equivalent and ignores this.
QUERY_TIMEOUT_MS = 15000

# When ``count_records`` is asked for an unfiltered count on a table whose
# schema-cache row estimate is at or above this threshold, return the
# estimate instead of running ``SELECT COUNT(*)``.
APPROX_COUNT_THRESHOLD = 100_000

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
SELECT_PREFIX_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE | re.DOTALL)
RISKY_SQL_KEYWORDS = {
    "create user",
    "alter user",
    "drop user",
    "grant ",
    "revoke ",
    "set password",
    "flush ",
    "reset ",
    "shutdown",
    "load data",
    "into outfile",
    "into dumpfile",
    "install plugin",
    "uninstall plugin",
    "drop database",
    "truncate ",
}
DDL_PREFIXES = ("create ", "alter ", "drop ", "rename ", "truncate ")
MASS_MUTATION_RE = re.compile(
    r"^\s*(UPDATE|DELETE)\b.*\bWHERE\s+(1\s*=\s*1|TRUE)\b",
    re.IGNORECASE | re.DOTALL,
)

logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.setLevel(logging.INFO)
    file_handler = logging.FileHandler("agentic_db_backend.log", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(file_handler)
    logger.propagate = False


class BaseAdapter:
    """Common interface for backend-specific database adapters."""

    def __init__(self, settings: dict):
        self.settings = settings
        self.conn = None
        self._allow_reconnect = True

    def connect(self) -> None:
        raise NotImplementedError

    def begin(self) -> None:
        raise NotImplementedError

    def commit(self) -> None:
        raise NotImplementedError

    def rollback(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None

    def ensure_connection(self, allow_reconnect: bool = True) -> None:
        if self.conn is None:
            if not allow_reconnect:
                raise RuntimeError("Database connection is not available.")
            self.connect()

    def set_reconnect_policy(self, allow_reconnect: bool) -> None:
        self._allow_reconnect = allow_reconnect

    def execute(self, sql: str, params: list | tuple | None = None):
        self.ensure_connection(allow_reconnect=self._allow_reconnect)
        cursor = self.conn.cursor()
        cursor.execute(sql, params or ())
        return cursor

    def create_savepoint(self, name: str) -> None:
        self.execute(f"SAVEPOINT {name}")

    def rollback_to_savepoint(self, name: str) -> None:
        self.execute(f"ROLLBACK TO SAVEPOINT {name}")

    def release_savepoint(self, name: str) -> None:
        self.execute(f"RELEASE SAVEPOINT {name}")

    def set_query_timeout(self, ms: int) -> None:
        """No-op default. MySQL overrides this with MAX_EXECUTION_TIME."""
        return None

    def is_query_timeout(self, exc: Exception) -> bool:
        """Backend-specific: did this exception come from a server-side cancel?"""
        return False

    def table_exists(self, table_name: str) -> bool:
        raise NotImplementedError

    def list_tables(self) -> list[dict]:
        raise NotImplementedError

    def describe_table(self, table_name: str) -> list[dict]:
        raise NotImplementedError

    def get_schema(self) -> dict[str, list[dict]]:
        raise NotImplementedError

    def list_indexes(self, table_name: str | None = None) -> list[dict]:
        raise NotImplementedError

    def get_foreign_keys(self, table_name: str) -> list[dict]:
        raise NotImplementedError

    def database_label(self) -> str:
        raise NotImplementedError

    def parameter_token(self) -> str:
        raise NotImplementedError

    def lookup_index_table(self, index_name: str) -> str | None:
        raise NotImplementedError

    def date_bucket_expr(self, column: str, granularity: str) -> str:
        raise NotImplementedError

    def explain_query(self, sql: str) -> tuple[list[str], list[tuple]]:
        raise NotImplementedError


class SQLiteAdapter(BaseAdapter):
    """Legacy SQLite adapter retained for local development fallback."""

    def connect(self) -> None:
        db_path = self.settings["path"]
        self.conn = sqlite3.connect(
            db_path, isolation_level=None, check_same_thread=False
        )
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.begin()

    def ensure_connection(self, allow_reconnect: bool = True) -> None:
        if self.conn is None:
            if not allow_reconnect:
                raise RuntimeError("SQLite connection is not available.")
            self.connect()

    def begin(self) -> None:
        self.conn.execute("BEGIN")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    def table_exists(self, table_name: str) -> bool:
        cursor = self.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (table_name,),
        )
        return cursor.fetchone() is not None

    def list_tables(self) -> list[dict]:
        cursor = self.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'"
        )
        return [{"name": row[0], "type": row[1]} for row in cursor.fetchall()]

    def describe_table(self, table_name: str) -> list[dict]:
        cursor = self.execute(f"PRAGMA table_info(`{table_name}`)")
        return [
            {
                "name": row[1],
                "type": row[2],
                "notnull": bool(row[3]),
                "default": row[4],
                "primary_key": bool(row[5]),
            }
            for row in cursor.fetchall()
        ]

    def get_schema(self) -> dict[str, list[dict]]:
        schema: dict[str, list[dict]] = {}
        cursor = self.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        for (table_name,) in cursor.fetchall():
            schema[table_name] = self.describe_table(table_name)
        return schema

    def list_indexes(self, table_name: str | None = None) -> list[dict]:
        if table_name:
            cursor = self.execute(f"PRAGMA index_list(`{table_name}`)")
            return [
                {"name": row[1], "unique": bool(row[2])} for row in cursor.fetchall()
            ]
        cursor = self.execute(
            "SELECT name, tbl_name FROM sqlite_master "
            "WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
        return [{"name": row[0], "table": row[1]} for row in cursor.fetchall()]

    def get_foreign_keys(self, table_name: str) -> list[dict]:
        cursor = self.execute(f"PRAGMA foreign_key_list(`{table_name}`)")
        return [
            {
                "id": row[0],
                "table": row[2],
                "from": row[3],
                "to": row[4],
                "on_update": row[5],
                "on_delete": row[6],
            }
            for row in cursor.fetchall()
        ]

    def database_label(self) -> str:
        return "Local SQLite database"

    def parameter_token(self) -> str:
        return "?"

    def lookup_index_table(self, index_name: str) -> str | None:
        cursor = self.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name = ?",
            (index_name,),
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def date_bucket_expr(self, column: str, granularity: str) -> str:
        col = f"`{column}`"
        formats = {
            "day": f"strftime('%Y-%m-%d', {col})",
            "week": f"strftime('%Y-W%W', {col})",
            "month": f"strftime('%Y-%m', {col})",
            "quarter": (
                f"strftime('%Y', {col}) || '-Q' || "
                f"((CAST(strftime('%m', {col}) AS INTEGER) + 2) / 3)"
            ),
        }
        if granularity not in formats:
            raise ValueError(f"Unsupported granularity: {granularity}")
        return formats[granularity]

    def explain_query(self, sql: str) -> tuple[list[str], list[tuple]]:
        cursor = self.execute(f"EXPLAIN QUERY PLAN {sql}")
        columns = [desc[0] for desc in cursor.description]
        rows = list(cursor.fetchall())
        return columns, rows


class MySQLAdapter(BaseAdapter):
    """Hosted MySQL adapter."""

    def connect(self) -> None:
        if pymysql is None:
            raise RuntimeError(
                "PyMySQL is not installed. Run 'pip install -r requirements.txt'."
            )

        connect_args = {
            "host": self.settings["host"],
            "port": self.settings["port"],
            "user": self.settings["user"],
            "password": self.settings["password"],
            "database": self.settings["name"],
            "autocommit": False,
            "connect_timeout": self.settings.get("connect_timeout", 10),
            "read_timeout": self.settings.get("read_timeout", 30),
            "write_timeout": self.settings.get("write_timeout", 30),
            "charset": "utf8mb4",
        }
        ssl_mode = self.settings.get("ssl_mode", "required")
        ssl_config = self._build_ssl_config(ssl_mode)
        if ssl_config is not None:
            connect_args["ssl"] = ssl_config

        retries = self.settings.get("connect_retries", 3)
        retry_delay = self.settings.get("connect_retry_delay", 1.0)
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                self.conn = pymysql.connect(**connect_args)
                self.begin()
                return
            except Exception as exc:  # noqa: BLE001 - preserve last driver error
                last_error = exc
                self.close()
                if attempt == retries:
                    break
                logger.warning(
                    "db_event=connect_retry attempt=%s/%s error=%s",
                    attempt,
                    retries,
                    type(exc).__name__,
                )
                time.sleep(retry_delay)

        raise RuntimeError(
            "Unable to establish a stable MySQL connection after "
            f"{retries} attempt(s): {type(last_error).__name__}: {last_error}"
        )

    def _build_ssl_config(self, ssl_mode: str) -> dict | None:
        if ssl_mode == "disabled":
            return None

        ssl_config: dict[str, Any] = {}
        ca_cert = self.settings.get("ca_cert")
        client_cert = self.settings.get("client_cert")
        client_key = self.settings.get("client_key")

        if ssl_mode in {"verify_ca", "verify_identity"}:
            ssl_config["ca"] = ca_cert
        if client_cert:
            ssl_config["cert"] = client_cert
        if client_key:
            ssl_config["key"] = client_key
        if ssl_mode == "verify_identity":
            ssl_config["check_hostname"] = True
            ssl_config["verify_mode"] = True
        return ssl_config

    def begin(self) -> None:
        self.ensure_connection()
        self.conn.begin()

    def commit(self) -> None:
        self.ensure_connection(allow_reconnect=False)
        self.conn.commit()

    def rollback(self) -> None:
        self.ensure_connection(allow_reconnect=False)
        self.conn.rollback()

    def set_query_timeout(self, ms: int) -> None:
        # MAX_EXECUTION_TIME only applies to SELECT statements server-side.
        # Mutations / DDL are unaffected; that is acceptable here since the
        # row cap protects analytical reads while staging operations are
        # already shielded by other guardrails.
        if ms is None or ms <= 0:
            return
        try:
            self.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(ms)}")
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.warning("db_event=timeout_setup_failed error=%s", type(exc).__name__)

    def is_query_timeout(self, exc: Exception) -> bool:
        # Server-side cancellations:
        #   1317  ER_QUERY_INTERRUPTED  (KILL QUERY or MAX_EXECUTION_TIME hit)
        #   3024  ER_QUERY_TIMEOUT      (MySQL 5.7.8+ MAX_EXECUTION_TIME)
        # Client-side cancellations (when read_timeout fires before the server
        # finishes the query, common on hosts that don't enforce
        # MAX_EXECUTION_TIME):
        #   2013  CR_SERVER_LOST with a "timed out" suffix
        args = getattr(exc, "args", ())
        if not args:
            return False
        errno = args[0]
        if errno in (1317, 3024):
            return True
        if errno == 2013:
            msg = " ".join(str(a) for a in args).lower()
            return "timed out" in msg or "timeout" in msg
        return False

    def ensure_connection(self, allow_reconnect: bool = True) -> None:
        if self.conn is None:
            if not allow_reconnect:
                raise ConnectionLostError("MySQL connection is not available.")
            self.connect()
            return

        try:
            self.conn.ping(reconnect=False)
        except Exception as exc:  # noqa: BLE001 - normalize driver-specific errors
            self.close()
            if not allow_reconnect:
                raise ConnectionLostError(
                    "The MySQL connection was lost while pending changes "
                    "existed. The server discarded the uncommitted transaction."
                ) from exc
            logger.warning(
                "db_event=reconnect reason=%s",
                type(exc).__name__,
            )
            self.connect()

    def table_exists(self, table_name: str) -> bool:
        cursor = self.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s LIMIT 1",
            (self.settings["name"], table_name),
        )
        return cursor.fetchone() is not None

    def list_tables(self) -> list[dict]:
        cursor = self.execute(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = %s ORDER BY table_name",
            (self.settings["name"],),
        )
        rows = []
        for table_name, table_type in cursor.fetchall():
            rows.append(
                {
                    "name": table_name,
                    "type": "view" if table_type.upper() == "VIEW" else "table",
                }
            )
        return rows

    def describe_table(self, table_name: str) -> list[dict]:
        cursor = self.execute(
            "SELECT column_name, column_type, is_nullable, column_default, column_key "
            "FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position",
            (self.settings["name"], table_name),
        )
        return [
            {
                "name": row[0],
                "type": row[1],
                "notnull": row[2] == "NO",
                "default": row[3],
                "primary_key": row[4] == "PRI",
            }
            for row in cursor.fetchall()
        ]

    def get_schema(self) -> dict[str, list[dict]]:
        schema: dict[str, list[dict]] = {}
        for entry in self.list_tables():
            if entry["type"] != "table":
                continue
            schema[entry["name"]] = self.describe_table(entry["name"])
        return schema

    def list_indexes(self, table_name: str | None = None) -> list[dict]:
        if table_name:
            cursor = self.execute(
                "SELECT index_name, non_unique "
                "FROM information_schema.statistics "
                "WHERE table_schema = %s AND table_name = %s "
                "GROUP BY index_name, non_unique ORDER BY index_name",
                (self.settings["name"], table_name),
            )
            return [
                {"name": row[0], "unique": row[1] == 0} for row in cursor.fetchall()
            ]

        cursor = self.execute(
            "SELECT table_name, index_name, non_unique "
            "FROM information_schema.statistics "
            "WHERE table_schema = %s "
            "GROUP BY table_name, index_name, non_unique "
            "ORDER BY table_name, index_name",
            (self.settings["name"],),
        )
        return [
            {"name": row[1], "table": row[0], "unique": row[2] == 0}
            for row in cursor.fetchall()
        ]

    def get_foreign_keys(self, table_name: str) -> list[dict]:
        cursor = self.execute(
            "SELECT rc.constraint_name, "
            "       kcu.referenced_table_name, "
            "       kcu.column_name, "
            "       kcu.referenced_column_name, "
            "       rc.update_rule, "
            "       rc.delete_rule "
            "FROM information_schema.referential_constraints rc "
            "JOIN information_schema.key_column_usage kcu "
            "ON rc.constraint_schema = kcu.constraint_schema "
            "AND rc.table_name = kcu.table_name "
            "AND rc.constraint_name = kcu.constraint_name "
            "WHERE rc.constraint_schema = %s AND rc.table_name = %s "
            "ORDER BY kcu.ordinal_position",
            (self.settings["name"], table_name),
        )
        return [
            {
                "id": row[0],
                "table": row[1],
                "from": row[2],
                "to": row[3],
                "on_update": row[4],
                "on_delete": row[5],
            }
            for row in cursor.fetchall()
        ]

    def database_label(self) -> str:
        return "Connected database"

    def parameter_token(self) -> str:
        return "%s"

    def lookup_index_table(self, index_name: str) -> str | None:
        cursor = self.execute(
            "SELECT table_name FROM information_schema.statistics "
            "WHERE table_schema = %s AND index_name = %s "
            "ORDER BY table_name LIMIT 1",
            (self.settings["name"], index_name),
        )
        row = cursor.fetchone()
        return row[0] if row else None

    def date_bucket_expr(self, column: str, granularity: str) -> str:
        col = f"`{column}`"
        formats = {
            # PyMySQL treats literal '%' in SQL as a parameter placeholder.
            # Double them so cursor.execute() does not try to substitute.
            "day": f"DATE_FORMAT({col}, '%%Y-%%m-%%d')",
            "week": f"DATE_FORMAT({col}, '%%x-W%%v')",
            "month": f"DATE_FORMAT({col}, '%%Y-%%m')",
            "quarter": f"CONCAT(YEAR({col}), '-Q', QUARTER({col}))",
        }
        if granularity not in formats:
            raise ValueError(f"Unsupported granularity: {granularity}")
        return formats[granularity]

    def explain_query(self, sql: str) -> tuple[list[str], list[tuple]]:
        cursor = self.execute(f"EXPLAIN {sql}")
        columns = [desc[0] for desc in cursor.description]
        rows = list(cursor.fetchall())
        return columns, rows


class DatabaseServer:
    """Database tool server with SQLite and MySQL backends."""

    def __init__(self, db_config: str | dict, mode: "Mode | None" = None):
        self.settings = self._normalize_settings(db_config)
        self.mode = mode
        if mode is not None:
            self.settings["allow_ddl"] = mode.allow_ddl
            self.settings["block_dangerous_sql"] = mode.block_dangerous_sql
            self.settings["require_where_for_update_delete"] = mode.require_where
        self.db_path = self._display_label(self.settings)
        self.conn = None
        self._adapter = self._create_adapter(self.settings)
        self._adapter.connect()
        self.conn = self._adapter.conn
        self._result_cache: OrderedDict[str, dict] = OrderedDict()
        self._query_counter = 0
        self._savepoint_counter = 0
        self._savepoint_stack: list[dict] = []
        self._transaction_counter = 0
        self._audit(
            "connect",
            backend=self.settings["type"],
            target=self.db_path,
        )
        self._schema_cache = SchemaCache(self)

    def _normalize_settings(self, db_config: str | dict) -> dict:
        if isinstance(db_config, str):
            return {
                "type": "sqlite",
                "path": db_config,
                "allow_ddl": True,
                "block_dangerous_sql": False,
                "require_where_for_update_delete": False,
            }
        settings = dict(db_config)
        settings.setdefault("type", "mysql")
        settings.setdefault("allow_ddl", True)
        settings.setdefault("block_dangerous_sql", True)
        settings.setdefault("require_where_for_update_delete", True)
        return settings

    def _display_label(self, settings: dict) -> str:
        if settings["type"] == "sqlite":
            return "Local SQLite database"
        return "Connected database"

    def _create_adapter(self, settings: dict) -> BaseAdapter:
        if settings["type"] == "sqlite":
            return SQLiteAdapter(settings)
        if settings["type"] == "mysql":
            return MySQLAdapter(settings)
        raise ValueError(f"Unsupported database type: {settings['type']}")

    def _audit(self, event: str, **fields: Any) -> None:
        safe_fields = {key: value for key, value in fields.items() if value is not None}
        logger.info("db_event=%s %s", event, safe_fields)

    def _store_result(self, sql: str, columns: list, rows: list) -> str:
        self._query_counter += 1
        query_id = f"q{self._query_counter}"
        self._result_cache[query_id] = {
            "query_id": query_id,
            "sql": sql,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
        }
        while len(self._result_cache) > RESULT_CACHE_SIZE:
            self._result_cache.popitem(last=False)
        return query_id

    def get_last_result(self) -> dict | None:
        if not self._result_cache:
            return None
        return next(reversed(self._result_cache.values()))

    def fetch_page(self, query_id: str, page: int = 0, page_size: int = 50) -> str:
        """Page through the cached rows of a previous SELECT.

        The cache is bounded by ``RESULT_CACHE_SIZE`` (LRU) and a per-result
        cap of ``MAX_RESULT_ROWS``, so ``page_size`` is also clamped.
        """
        if not isinstance(query_id, str) or not query_id:
            return _dumps({"error": "query_id is required."})
        try:
            page = max(0, int(page))
        except (TypeError, ValueError):
            return _dumps({"error": "page must be a non-negative integer."})
        try:
            page_size = int(page_size)
        except (TypeError, ValueError):
            page_size = 50
        page_size = max(1, min(page_size, 500))

        cached = self._result_cache.get(query_id)
        if cached is None:
            return _dumps(
                {
                    "error": "unknown_query_id",
                    "query_id": query_id,
                    "message": (
                        "That result is no longer cached (process restart or "
                        f"more than {RESULT_CACHE_SIZE} newer queries ran). "
                        "Re-run the SELECT to refresh the cache."
                    ),
                }
            )
        all_rows = cached["rows"]
        start = page * page_size
        end = start + page_size
        rows = all_rows[start:end]
        return _dumps(
            {
                "query_id": query_id,
                "page": page,
                "page_size": page_size,
                "page_count": (len(all_rows) + page_size - 1) // page_size,
                "total_rows": len(all_rows),
                "columns": cached["columns"],
                "rows": rows,
                "has_more": end < len(all_rows),
            }
        )

    def _validate_identifier(self, value: str, kind: str = "identifier") -> None:
        if not value or not IDENTIFIER_RE.fullmatch(value):
            raise ValueError(f"Invalid {kind}: {value!r}")

    def _validate_table_name(self, table_name: str) -> None:
        self._validate_identifier(table_name, "table name")
        if not self._adapter.table_exists(table_name):
            raise ValueError(f"Table or view '{table_name}' does not exist.")

    def _create_savepoint(
        self, tool: str, description: str, is_ddl: bool = False
    ) -> str:
        self._savepoint_counter += 1
        name = f"sp_{self._savepoint_counter}"
        self._adapter.create_savepoint(name)
        self._savepoint_stack.append(
            {
                "name": name,
                "tool": tool,
                "description": description,
                "timestamp": datetime.now().isoformat(),
                "is_ddl": is_ddl,
            }
        )
        return name

    def _starts_with_select(self, sql: str) -> bool:
        return bool(SELECT_PREFIX_RE.match(sql or ""))

    def _sql_parse(self, sql: str) -> list[str]:
        """Split SQL into statements via sqlparse, dropping empties + comments."""
        if not sql:
            return []
        cleaned: list[str] = []
        for piece in sqlparse.split(sql):
            without_comments = sqlparse.format(piece, strip_comments=True).strip()
            without_comments = without_comments.rstrip(";").strip()
            if without_comments:
                cleaned.append(without_comments)
        return cleaned

    def _walk_tokens(self, parent):
        """Yield every token under ``parent`` recursively (depth-first)."""
        for token in parent.tokens:
            yield token
            if hasattr(token, "tokens"):
                yield from self._walk_tokens(token)

    def _strip_identifier(self, name: str) -> str:
        return (name or "").strip().strip("`\"[]")

    def _extract_from_info(
        self, statement
    ) -> tuple[list[str], bool]:
        """Return (tables, has_join) for a parsed SELECT statement.

        Walks the token list after the ``FROM`` keyword and stops at the
        first ``WHERE`` / ``GROUP`` / ``ORDER`` / ``LIMIT`` / ``HAVING``.
        Handles ``Identifier``, ``IdentifierList``, and quoted names; skips
        aliases via ``get_real_name``.
        """
        tables: list[str] = []
        has_join = False
        from_index: int | None = None
        for index, token in enumerate(statement.tokens):
            if (
                token.ttype is sqlparse_tokens.Keyword
                and token.normalized.upper() == "FROM"
            ):
                from_index = index
                break
        if from_index is None:
            return tables, has_join

        for token in statement.tokens[from_index + 1 :]:
            if isinstance(token, sqlparse_sql.Where):
                break
            if token.ttype is sqlparse_tokens.Keyword:
                kw = token.normalized.upper()
                if kw in {"WHERE", "GROUP", "ORDER", "LIMIT", "HAVING"}:
                    break
                if "JOIN" in kw:
                    has_join = True
                    continue
            if isinstance(token, sqlparse_sql.IdentifierList):
                for ident in token.get_identifiers():
                    name = ident.get_real_name() if hasattr(ident, "get_real_name") else None
                    if name:
                        tables.append(self._strip_identifier(name))
            elif isinstance(token, sqlparse_sql.Identifier):
                name = token.get_real_name()
                if name:
                    tables.append(self._strip_identifier(name))
            elif token.ttype is sqlparse_tokens.Name:
                tables.append(self._strip_identifier(token.value))
        return tables, has_join

    def _normalize_sql(self, sql: str) -> str:
        statements = self._sql_parse(sql)
        if len(statements) == 0:
            raise ValueError("SQL statement is empty.")
        if len(statements) > 1:
            raise ValueError("Multiple SQL statements are not allowed.")
        return statements[0]

    def _check_mutation_condition(self, condition: str) -> None:
        if not condition or not condition.strip():
            raise ValueError("A WHERE condition is required for this operation.")
        normalized = re.sub(r"\s+", " ", condition.strip().lower())
        if normalized in {"1=1", "true"}:
            raise ValueError("Refusing a full-table mutation condition.")

    def _guard_execute_sql(self, sql: str) -> str:
        normalized = re.sub(r"\s+", " ", sql.strip().lower())

        if self.settings.get("block_dangerous_sql", True):
            if any(keyword in normalized for keyword in RISKY_SQL_KEYWORDS):
                return "Blocked dangerous SQL statement by backend policy."
            if MASS_MUTATION_RE.match(sql):
                return "Blocked full-table mutation by backend policy."

        if normalized.startswith(DDL_PREFIXES) and not self.settings.get("allow_ddl", True):
            return "DDL statements are disabled by backend policy."

        if self.settings.get("require_where_for_update_delete", True):
            if normalized.startswith("update ") and " where " not in normalized:
                return "UPDATE without WHERE is blocked by backend policy."
            if normalized.startswith("delete ") and " where " not in normalized:
                return "DELETE without WHERE is blocked by backend policy."

        return ""

    _MUTATION_TOOL_NAMES = frozenset(
        {
            "insert_record",
            "update_record",
            "delete_record",
            "create_view",
            "drop_view",
            "create_index",
            "drop_index",
            "execute_sql",
            "undo",
            "commit_changes",
            "begin_transaction",
            "commit_transaction",
        }
    )

    _LOCAL_TOOLS = frozenset(
        {"get_pending_changes", "get_schema_summary", "begin_transaction"}
    )

    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        tool_map = {
            "list_tables": self.list_tables,
            "describe_table": self.describe_table,
            "run_query": self.run_query,
            "insert_record": self.insert_record,
            "update_record": self.update_record,
            "delete_record": self.delete_record,
            "create_view": self.create_view,
            "drop_view": self.drop_view,
            "get_schema": self.get_schema,
            "count_records": self.count_records,
            "list_indexes": self.list_indexes,
            "create_index": self.create_index,
            "drop_index": self.drop_index,
            "get_foreign_keys": self.get_foreign_keys,
            "export_query": self.export_query,
            "execute_sql": self.execute_sql,
            "undo": self.undo,
            "commit_changes": self.commit_changes,
            "get_pending_changes": self.get_pending_changes,
            "explain_query": self.explain_query,
            "dry_run_mutation": self.dry_run_mutation,
            "begin_transaction": self.begin_transaction,
            "commit_transaction": self.commit_transaction,
            "lint_query": self.lint_query,
            "refresh_schema": self.refresh_schema,
            "get_schema_summary": self.get_schema_summary,
            "fetch_page": self.fetch_page,
            "sample_rows": self.sample_rows,
            "trend_over_time": self.trend_over_time,
            "compare_periods": self.compare_periods,
            "top_n_breakdown": self.top_n_breakdown,
            "distribution": self.distribution,
            "generate_chart": self.generate_chart,
        }

        if tool_name not in tool_map:
            return _dumps({"error": f"Unknown tool: {tool_name}"})

        if self.mode is not None:
            # Read-only check first - if a mutation tool is invoked under a
            # read-only mode, surface the more specific error even if the
            # tool also happens to be outside the allowlist.
            if self.mode.read_only and tool_name in self._MUTATION_TOOL_NAMES:
                self._audit("tool_blocked", tool=tool_name, reason="read_only")
                return _dumps(
                    {
                        "error": "read_only_mode",
                        "mode": self.mode.name,
                        "tool": tool_name,
                    }
                )
            if self.mode.read_only and tool_name == "execute_sql":
                raw_sql = arguments.get("sql", "")
                if raw_sql and not self._starts_with_select(raw_sql.lstrip()):
                    return _dumps(
                        {
                            "error": "read_only_mode",
                            "mode": self.mode.name,
                            "tool": tool_name,
                        }
                    )
            if tool_name not in self.mode.allowed_tools:
                self._audit("tool_blocked", tool=tool_name, reason="not_in_mode")
                return _dumps(
                    {
                        "error": "tool_not_allowed_in_mode",
                        "mode": self.mode.name,
                        "tool": tool_name,
                    }
                )

        needs_database = tool_name not in self._LOCAL_TOOLS
        allow_reconnect = not self._savepoint_stack
        self._adapter.set_reconnect_policy(allow_reconnect)

        try:
            if needs_database:
                self._adapter.ensure_connection(allow_reconnect=allow_reconnect)
                self.conn = self._adapter.conn
            return tool_map[tool_name](**arguments)
        except Exception as exc:  # noqa: BLE001 - tool boundary returns JSON errors
            self._audit("tool_error", tool=tool_name, error=type(exc).__name__)
            return _dumps({"error": f"{type(exc).__name__}: {exc}"})
        finally:
            self._adapter.set_reconnect_policy(True)

    def list_tables(self) -> str:
        return _dumps({"tables": self._adapter.list_tables()})

    def describe_table(self, table_name: str) -> str:
        self._validate_table_name(table_name)
        return _dumps(
            {"table": table_name, "columns": self._adapter.describe_table(table_name)}
        )

    def _bounded_select(self, sql: str) -> tuple[list[str], list, bool]:
        """Run a SELECT with a row cap and per-query timeout.

        Returns ``(columns, rows, truncated_at_cap)``. Raises whatever the
        adapter raises (the caller is expected to translate query-timeout
        errors into a structured response).
        """
        self._adapter.set_query_timeout(QUERY_TIMEOUT_MS)
        cursor = self._adapter.execute(sql)
        columns = [description[0] for description in cursor.description]
        # fetchmany(cap + 1) lets us know if there were extra rows beyond the
        # cap without materializing them all in memory.
        fetched = cursor.fetchmany(MAX_RESULT_ROWS + 1)
        truncated = len(fetched) > MAX_RESULT_ROWS
        rows = fetched[:MAX_RESULT_ROWS] if truncated else fetched
        # Drain the rest server-side so the connection is ready for reuse
        # without holding the cursor open.
        if truncated:
            try:
                cursor.fetchall()
            except Exception:  # noqa: BLE001 - drain is best-effort
                pass
        return columns, rows, truncated

    def run_query(self, sql: str) -> str:
        sql = self._normalize_sql(sql)
        if not self._starts_with_select(sql):
            return _dumps(
                {
                    "error": "Only SELECT queries are allowed. "
                    "Use insert_record, update_record, delete_record, or execute_sql for modifications."
                }
            )

        try:
            columns, rows, truncated_at_cap = self._bounded_select(sql)
        except Exception as exc:  # noqa: BLE001 - structured error per backend
            if self._adapter.is_query_timeout(exc):
                self._audit("query_timeout", backend=self.settings["type"])
                return _dumps(
                    {
                        "error": "query_timeout",
                        "timeout_ms": QUERY_TIMEOUT_MS,
                        "message": (
                            f"Query exceeded the {QUERY_TIMEOUT_MS} ms server-side "
                            "time limit and was cancelled. Add filters / LIMIT, or "
                            "use sample_rows for exploration."
                        ),
                    }
                )
            raise
        query_id = self._store_result(sql, columns, rows)
        self._audit(
            "query",
            rows=len(rows),
            truncated=truncated_at_cap,
            backend=self.settings["type"],
        )
        note = (
            f"Preview shows up to {PREVIEW_ROWS} rows. "
            f"Full result (up to {MAX_RESULT_ROWS}) is cached and "
            "fetchable via fetch_page(query_id, page). "
            "For analytics use SQL aggregates (COUNT, AVG, SUM, ...)."
        )
        if truncated_at_cap:
            note = (
                f"Result truncated at the {MAX_RESULT_ROWS}-row cap. Narrow the "
                "query with WHERE/LIMIT, or use sample_rows for exploration. "
                "Cached rows can still be paged with fetch_page(query_id, page)."
            )
        return _dumps(
            {
                "query_id": query_id,
                "columns": columns,
                "row_count": len(rows),
                "preview": rows[:PREVIEW_ROWS],
                "truncated": len(rows) > PREVIEW_ROWS,
                "truncated_at_cap": truncated_at_cap,
                "note": note,
            }
        )

    def insert_record(self, table_name: str, data: dict) -> str:
        self._validate_table_name(table_name)
        if not data:
            raise ValueError("Insert data cannot be empty.")
        for column in data:
            self._validate_identifier(column, "column name")

        self._create_savepoint("insert_record", f"Insert into {table_name}")
        columns = ", ".join(f"`{column}`" for column in data.keys())
        placeholders = ", ".join([self._adapter.parameter_token()] * len(data))
        values = list(data.values())
        cursor = self._adapter.execute(
            f"INSERT INTO `{table_name}` ({columns}) VALUES ({placeholders})",
            values,
        )
        self._audit("insert", table=table_name)
        return _dumps(
            {
                "success": True,
                "table": table_name,
                "inserted_id": cursor.lastrowid,
                "committed": False,
                "note": "Change is pending. Use commit_changes to save or undo to revert.",
            }
        )

    def update_record(self, table_name: str, data: dict, condition: str) -> str:
        self._validate_table_name(table_name)
        if not data:
            raise ValueError("Update data cannot be empty.")
        self._check_mutation_condition(condition)
        for column in data:
            self._validate_identifier(column, "column name")

        self._create_savepoint("update_record", f"Update {table_name} WHERE {condition}")
        token = self._adapter.parameter_token()
        set_clause = ", ".join(f"`{col}` = {token}" for col in data.keys())
        values = list(data.values())
        cursor = self._adapter.execute(
            f"UPDATE `{table_name}` SET {set_clause} WHERE {condition}",
            values,
        )
        self._audit("update", table=table_name, rows=cursor.rowcount)
        return _dumps(
            {
                "success": True,
                "table": table_name,
                "rows_affected": cursor.rowcount,
                "committed": False,
                "note": "Change is pending. Use commit_changes to save or undo to revert.",
            }
        )

    def delete_record(self, table_name: str, condition: str) -> str:
        self._validate_table_name(table_name)
        self._check_mutation_condition(condition)

        self._create_savepoint("delete_record", f"Delete from {table_name} WHERE {condition}")
        cursor = self._adapter.execute(
            f"DELETE FROM `{table_name}` WHERE {condition}"
        )
        self._audit("delete", table=table_name, rows=cursor.rowcount)
        return _dumps(
            {
                "success": True,
                "table": table_name,
                "rows_affected": cursor.rowcount,
                "committed": False,
                "note": "Change is pending. Use commit_changes to save or undo to revert.",
            }
        )

    def create_view(self, view_name: str, query: str) -> str:
        self._validate_identifier(view_name, "view name")
        query = self._normalize_sql(query)
        if not self._starts_with_select(query):
            raise ValueError("Views must be created from a SELECT query.")

        self._create_savepoint("create_view", f"Create view {view_name}", is_ddl=True)
        self._adapter.execute(f"CREATE VIEW `{view_name}` AS {query}")
        self._audit("create_view", view=view_name)
        return _dumps(
            {
                "success": True,
                "view": view_name,
                "committed": False,
                "note": "Change is pending. Use commit_changes to save or undo to revert.",
            }
        )

    def drop_view(self, view_name: str) -> str:
        self._validate_identifier(view_name, "view name")
        self._create_savepoint("drop_view", f"Drop view {view_name}", is_ddl=True)
        self._adapter.execute(f"DROP VIEW IF EXISTS `{view_name}`")
        self._audit("drop_view", view=view_name)
        return _dumps(
            {
                "success": True,
                "view": view_name,
                "committed": False,
                "note": "Change is pending. Use commit_changes to save or undo to revert.",
            }
        )

    def get_schema(self) -> str:
        return _dumps({"schema": self._adapter.get_schema()})

    def count_records(self, table_name: str, condition: str = None) -> str:
        self._validate_table_name(table_name)

        # Unfiltered count on a known-large table: return the cached approx
        # instead of issuing SELECT COUNT(*), which scans the whole table on
        # InnoDB and can take many seconds at 1M+ rows.
        if not condition:
            meta = self._schema_cache.structured.get(table_name) or {}
            approx = meta.get("approx_row_count")
            if isinstance(approx, int) and approx >= APPROX_COUNT_THRESHOLD:
                return _dumps(
                    {
                        "table": table_name,
                        "count": approx,
                        "approximate": True,
                        "note": (
                            f"Returned the approximate row count from "
                            f"information_schema.TABLES (threshold: "
                            f"{APPROX_COUNT_THRESHOLD:,} rows). Values can be "
                            "stale by minutes. For an exact count pass a "
                            "WHERE condition."
                        ),
                    }
                )

        sql = f"SELECT COUNT(*) FROM `{table_name}`"
        if condition:
            sql += f" WHERE {condition}"
        cursor = self._adapter.execute(sql)
        return _dumps(
            {
                "table": table_name,
                "count": cursor.fetchone()[0],
                "approximate": False,
            }
        )

    def sample_rows(self, table_name: str, n: int = 20, seed: int | None = None) -> str:
        """Return a random sample of rows from ``table_name``.

        Exploration helper for tables too large to scan. Bounded by the
        per-query timeout and the row cap. Result is cached so ``fetch_page``
        can extend the same sample.
        """
        self._validate_table_name(table_name)
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 20
        n = max(1, min(n, 200))
        if self.settings.get("type") == "mysql":
            seed_expr = "" if seed is None else f"{int(seed)}"
            sql = f"SELECT * FROM `{table_name}` ORDER BY RAND({seed_expr}) LIMIT {n}"
        else:
            sql = f"SELECT * FROM `{table_name}` ORDER BY RANDOM() LIMIT {n}"
        try:
            columns, rows, _ = self._bounded_select(sql)
        except Exception as exc:  # noqa: BLE001
            if self._adapter.is_query_timeout(exc):
                return _dumps(
                    {
                        "error": "query_timeout",
                        "timeout_ms": QUERY_TIMEOUT_MS,
                        "message": (
                            f"Sampling {table_name} took longer than "
                            f"{QUERY_TIMEOUT_MS} ms. Try a smaller n."
                        ),
                    }
                )
            raise
        query_id = self._store_result(sql, columns, rows)
        return _dumps(
            {
                "query_id": query_id,
                "table": table_name,
                "n": n,
                "columns": columns,
                "rows": rows,
                "note": (
                    f"Random sample of {len(rows)} rows. "
                    "Use fetch_page(query_id, page) to keep paging through "
                    "the cached sample, or call again for a fresh sample."
                ),
            }
        )

    def list_indexes(self, table_name: str = None) -> str:
        if table_name:
            self._validate_table_name(table_name)
        return _dumps({"indexes": self._adapter.list_indexes(table_name)})

    def create_index(
        self,
        index_name: str,
        table_name: str,
        columns: list,
        unique: bool = False,
    ) -> str:
        self._validate_identifier(index_name, "index name")
        self._validate_table_name(table_name)
        if not columns:
            raise ValueError("At least one column is required.")
        for column in columns:
            self._validate_identifier(column, "column name")

        self._create_savepoint(
            "create_index",
            f"Create index {index_name} on {table_name}",
            is_ddl=True,
        )
        unique_str = "UNIQUE " if unique else ""
        cols = ", ".join(f"`{column}`" for column in columns)
        self._adapter.execute(
            f"CREATE {unique_str}INDEX `{index_name}` ON `{table_name}` ({cols})"
        )
        self._audit("create_index", table=table_name, index=index_name)
        return _dumps(
            {
                "success": True,
                "index": index_name,
                "table": table_name,
                "columns": columns,
                "unique": unique,
                "committed": False,
                "note": "Change is pending. Use commit_changes to save or undo to revert.",
            }
        )

    def drop_index(self, index_name: str) -> str:
        self._validate_identifier(index_name, "index name")
        self._create_savepoint("drop_index", f"Drop index {index_name}", is_ddl=True)
        if self.settings["type"] == "mysql":
            table_name = self._adapter.lookup_index_table(index_name)
            if not table_name:
                raise ValueError(f"Index '{index_name}' does not exist.")
            self._adapter.execute(f"DROP INDEX `{index_name}` ON `{table_name}`")
        else:
            self._adapter.execute(f"DROP INDEX `{index_name}`")
        self._audit("drop_index", index=index_name)
        return _dumps(
            {
                "success": True,
                "index": index_name,
                "committed": False,
                "note": "Change is pending. Use commit_changes to save or undo to revert.",
            }
        )

    def get_foreign_keys(self, table_name: str) -> str:
        self._validate_table_name(table_name)
        return _dumps(
            {"table": table_name, "foreign_keys": self._adapter.get_foreign_keys(table_name)}
        )

    def export_query(self, sql: str, format: str = "json") -> str:
        sql = self._normalize_sql(sql)
        if not self._starts_with_select(sql):
            return _dumps({"error": "Only SELECT queries can be exported."})

        try:
            columns, rows, truncated_at_cap = self._bounded_select(sql)
        except Exception as exc:  # noqa: BLE001
            if self._adapter.is_query_timeout(exc):
                self._audit("export_timeout", backend=self.settings["type"])
                return _dumps(
                    {
                        "error": "query_timeout",
                        "timeout_ms": QUERY_TIMEOUT_MS,
                        "message": (
                            f"Export query exceeded the {QUERY_TIMEOUT_MS} ms "
                            "limit. Narrow with WHERE/LIMIT before exporting."
                        ),
                    }
                )
            raise

        os.makedirs(EXPORTS_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        fmt = format.lower()

        if fmt == "csv":
            path = os.path.join(EXPORTS_DIR, f"query_{timestamp}.csv")
            with open(path, "w", newline="", encoding="utf-8") as file_obj:
                writer = csv.writer(file_obj)
                writer.writerow(columns)
                writer.writerows(rows)
        else:
            fmt = "json"
            path = os.path.join(EXPORTS_DIR, f"query_{timestamp}.json")
            data = [dict(zip(columns, row)) for row in rows]
            with open(path, "w", encoding="utf-8") as file_obj:
                json.dump({"columns": columns, "rows": data}, file_obj, indent=2)

        query_id = self._store_result(sql, columns, rows)
        self._audit(
            "export_query",
            format=fmt,
            rows=len(rows),
            truncated=truncated_at_cap,
        )
        return _dumps(
            {
                "query_id": query_id,
                "format": fmt,
                "path": path,
                "row_count": len(rows),
                "truncated_at_cap": truncated_at_cap,
                "columns": columns,
                "preview": rows[:EXPORT_PREVIEW_ROWS],
            }
        )

    def execute_sql(self, sql: str) -> str:
        sql = self._normalize_sql(sql)
        rejection_reason = self._guard_execute_sql(sql)
        if rejection_reason:
            self._audit("blocked_sql", reason=rejection_reason)
            return _dumps({"error": rejection_reason})

        is_ddl = re.sub(r"\s+", " ", sql.strip().lower()).startswith(DDL_PREFIXES)
        self._create_savepoint("execute_sql", f"Execute: {sql[:80]}", is_ddl=is_ddl)
        cursor = self._adapter.execute(sql)
        affected = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        self._audit("execute_sql", statement=sql.split()[0].upper(), rows=affected)
        return _dumps(
            {
                "success": True,
                "rows_affected": affected,
                "committed": False,
                "note": "Change is pending. Use commit_changes to save or undo to revert.",
            }
        )

    def undo(self, steps: int = 1) -> str:
        if not self._savepoint_stack:
            return _dumps({"error": "Nothing to undo."})

        original_count = len(self._savepoint_stack)
        original_descriptions = [sp["description"] for sp in self._savepoint_stack]
        steps = min(max(steps, 1), original_count)
        undone = []
        for _ in range(steps):
            sp = self._savepoint_stack.pop()
            # Transaction markers (begin_transaction / commit_transaction)
            # don't have a real SQL savepoint - pop them without issuing SQL.
            if sp["tool"] in {"begin_transaction", "commit_transaction"}:
                undone.append(sp)
                continue
            try:
                self._adapter.rollback_to_savepoint(sp["name"])
                self._adapter.release_savepoint(sp["name"])
            except ConnectionLostError:
                # Put the entry we just popped back so the recovery message
                # reflects the full lost set, then drain the rest.
                self._savepoint_stack.append(sp)
                return self._recover_lost_connection(
                    original_count, original_descriptions, "undo"
                )
            undone.append(sp)

        self._audit("undo", steps=len(undone))
        return _dumps(
            {
                "success": True,
                "undone_count": len(undone),
                "undone": [
                    {
                        "tool": item["tool"],
                        "description": item["description"],
                        "timestamp": item["timestamp"],
                    }
                    for item in undone
                ],
                "remaining_pending": len(self._savepoint_stack),
            }
        )

    def _recover_lost_connection(
        self, count: int, descriptions: list[str], operation: str
    ) -> str:
        """Clear stale local state after the server killed our transaction.

        Returns the structured JSON response the route / agent will surface.
        """
        self._savepoint_stack.clear()
        try:
            # Re-establish so the next request lands on a live connection.
            self._adapter.connect()
        except Exception as reconnect_exc:  # noqa: BLE001 - best-effort
            self._audit(
                "reconnect_failed_after_loss",
                error=type(reconnect_exc).__name__,
            )
        self._audit("connection_lost", operation=operation, lost_count=count)
        return _dumps(
            {
                "error": "connection_lost",
                "operation": operation,
                "lost_count": count,
                "lost": descriptions,
                "message": (
                    "The database connection was dropped while your changes "
                    "were uncommitted. The server has discarded them. Please "
                    "re-stage your changes in the chat and commit promptly "
                    "(shared MySQL hosts close idle connections quickly)."
                ),
            }
        )

    def commit_changes(self) -> str:
        if not self._savepoint_stack:
            return _dumps({"message": "No pending changes to commit."})

        count = len(self._savepoint_stack)
        descriptions = [sp["description"] for sp in self._savepoint_stack]
        had_ddl = any(sp.get("is_ddl") for sp in self._savepoint_stack)

        try:
            self._adapter.commit()
        except ConnectionLostError:
            return self._recover_lost_connection(count, descriptions, "commit")
        self._savepoint_stack.clear()
        self._adapter.begin()
        if had_ddl:
            try:
                self._schema_cache.rebuild()
            except Exception as exc:  # noqa: BLE001 - log and continue
                self._audit("schema_cache_rebuild_failed", error=type(exc).__name__)
        self._audit("commit", count=count, schema_refreshed=had_ddl)
        return _dumps(
            {
                "success": True,
                "committed_count": count,
                "committed": descriptions,
                "schema_refreshed": had_ddl,
            }
        )

    # ------------------------------------------------------------------
    # Schema cache / introspection tools
    # ------------------------------------------------------------------

    def get_schema_summary(self, mode: str | None = None) -> str:
        which = mode or (self.mode.name if self.mode else "technical")
        summary = self._schema_cache.summary_for(which)
        return _dumps({"mode": which, "summary": summary})

    def refresh_schema(self) -> str:
        self._schema_cache.rebuild()
        which = self.mode.name if self.mode else "technical"
        return _dumps(
            {
                "tables": len(self._schema_cache.structured),
                "summary": self._schema_cache.summary_for(which),
            }
        )

    # ------------------------------------------------------------------
    # Tech-mode helper tools
    # ------------------------------------------------------------------

    def explain_query(self, sql: str) -> str:
        sql = self._normalize_sql(sql)
        if not self._starts_with_select(sql):
            return _dumps({"error": "explain_query only accepts SELECT/WITH statements."})
        columns, rows = self._adapter.explain_query(sql)
        plan_rows = [dict(zip(columns, row)) for row in rows]
        raw = "\n".join(" | ".join(str(cell) for cell in row) for row in rows)
        return _dumps(
            {
                "sql": sql,
                "columns": columns,
                "plan": plan_rows,
                "raw": raw,
            }
        )

    def dry_run_mutation(self, sql: str) -> str:
        sql = self._normalize_sql(sql)
        parsed = sqlparse.parse(sql)
        if not parsed:
            return _dumps({"error": "Could not parse the statement."})
        statement = parsed[0]

        kind: str | None = None
        for token in statement.tokens:
            if token.ttype is sqlparse_tokens.DML:
                kind = token.normalized.upper()
                break
        if kind not in {"UPDATE", "DELETE"}:
            return _dumps(
                {"error": "dry_run_mutation only accepts UPDATE or DELETE statements."}
            )

        where_token: sqlparse_sql.Where | None = None
        for token in statement.tokens:
            if isinstance(token, sqlparse_sql.Where):
                where_token = token
                break
        if where_token is None:
            return _dumps(
                {"error": "dry_run_mutation requires a WHERE clause for safety."}
            )
        where_clause = str(where_token).strip()
        if where_clause.lower().startswith("where"):
            where_clause = where_clause[5:].strip()

        table: str | None = None
        if kind == "UPDATE":
            seen_dml = False
            for token in statement.tokens:
                if (
                    not seen_dml
                    and token.ttype is sqlparse_tokens.DML
                    and token.normalized.upper() == "UPDATE"
                ):
                    seen_dml = True
                    continue
                if not seen_dml or token.is_whitespace:
                    continue
                if isinstance(token, sqlparse_sql.Identifier):
                    table = self._strip_identifier(token.get_real_name())
                    break
                if token.ttype is sqlparse_tokens.Name:
                    table = self._strip_identifier(token.value)
                    break
        else:  # DELETE
            seen_from = False
            for token in statement.tokens:
                if (
                    not seen_from
                    and token.ttype is sqlparse_tokens.Keyword
                    and token.normalized.upper() == "FROM"
                ):
                    seen_from = True
                    continue
                if not seen_from or token.is_whitespace:
                    continue
                if isinstance(token, sqlparse_sql.Identifier):
                    table = self._strip_identifier(token.get_real_name())
                    break
                if token.ttype is sqlparse_tokens.Name:
                    table = self._strip_identifier(token.value)
                    break

        if not table:
            return _dumps(
                {"error": "Could not parse the target table from the statement."}
            )

        count_sql = f"SELECT COUNT(*) FROM `{table}` WHERE {where_clause}"
        sample_sql = f"SELECT * FROM `{table}` WHERE {where_clause} LIMIT 5"
        cursor = self._adapter.execute(count_sql)
        affected_count = int(cursor.fetchone()[0])
        cursor = self._adapter.execute(sample_sql)
        sample_cols = [desc[0] for desc in cursor.description]
        sample_rows = [list(row) for row in cursor.fetchall()]
        return _dumps(
            {
                "kind": kind,
                "table": table,
                "where": where_clause,
                "affected_count": affected_count,
                "sample_columns": sample_cols,
                "sample_rows": sample_rows,
            }
        )

    def begin_transaction(self, label: str) -> str:
        if not label or not label.strip():
            raise ValueError("Transaction label is required.")
        label = label.strip()
        # Block nested begins under the same label.
        for sp in self._savepoint_stack:
            if sp["tool"] == "begin_transaction" and sp.get("label") == label:
                return _dumps({"error": f"Transaction '{label}' is already open."})
        self._transaction_counter += 1
        transaction_id = f"txn_{self._transaction_counter}"
        self._savepoint_stack.append(
            {
                "name": transaction_id,
                "tool": "begin_transaction",
                "description": f"Begin transaction '{label}'",
                "timestamp": datetime.now().isoformat(),
                "is_ddl": False,
                "label": label,
            }
        )
        self._audit("begin_transaction", label=label)
        return _dumps(
            {
                "success": True,
                "transaction_id": transaction_id,
                "label": label,
            }
        )

    def commit_transaction(self, label: str) -> str:
        if not label or not label.strip():
            raise ValueError("Transaction label is required.")
        label = label.strip()
        opening_index = None
        for index in range(len(self._savepoint_stack) - 1, -1, -1):
            sp = self._savepoint_stack[index]
            if sp["tool"] == "begin_transaction" and sp.get("label") == label:
                opening_index = index
                break
        if opening_index is None:
            return _dumps({"error": f"No open transaction with label '{label}'."})
        children = self._savepoint_stack[opening_index + 1 :]
        child_names = [c["name"] for c in children]
        self._savepoint_stack.append(
            {
                "name": f"{self._savepoint_stack[opening_index]['name']}_close",
                "tool": "commit_transaction",
                "description": f"Commit transaction '{label}'",
                "timestamp": datetime.now().isoformat(),
                "is_ddl": False,
                "label": label,
            }
        )
        self._audit("commit_transaction", label=label, children=len(child_names))
        return _dumps(
            {
                "success": True,
                "label": label,
                "committed": child_names,
                "note": "Transaction grouped. Use commit_changes to persist or undo to revert.",
            }
        )

    def lint_query(self, sql: str) -> str:
        sql = self._normalize_sql(sql)
        if not self._starts_with_select(sql):
            return _dumps(
                {"error": "lint_query only accepts SELECT/WITH statements."}
            )
        warnings: list[dict] = []
        parsed = sqlparse.parse(sql)
        if not parsed:
            return _dumps({"sql": sql, "warnings": warnings})
        statement = parsed[0]

        tables, has_join = self._extract_from_info(statement)
        where_token: sqlparse_sql.Where | None = None
        for token in statement.tokens:
            if isinstance(token, sqlparse_sql.Where):
                where_token = token
                break

        # 1) Cartesian join: more than one FROM table, no JOIN, no WHERE.
        if len(tables) > 1 and not has_join and where_token is None:
            warnings.append(
                {
                    "code": "cartesian_join",
                    "message": (
                        "FROM lists multiple tables with no JOIN or WHERE "
                        "clause - this is a Cartesian product. Use explicit "
                        "JOIN ... ON instead."
                    ),
                }
            )

        # 2) SELECT * heuristic on wide tables.
        has_wildcard = any(
            tok.ttype is sqlparse_tokens.Wildcard for tok in self._walk_tokens(statement)
        )
        if has_wildcard:
            for table in tables:
                meta = self._schema_cache.structured.get(table)
                if meta and len(meta.get("columns", [])) >= 8:
                    warnings.append(
                        {
                            "code": "select_star_wide_table",
                            "message": (
                                f"`SELECT *` on `{table}` (>={len(meta['columns'])} "
                                "columns) - list only the columns you need."
                            ),
                        }
                    )

        # 3) Missing-index hint: walk Comparison tokens under WHERE.
        if where_token is not None:
            referenced_columns: set[str] = set()
            for tok in self._walk_tokens(where_token):
                if isinstance(tok, sqlparse_sql.Comparison):
                    for sub in tok.tokens:
                        if isinstance(sub, sqlparse_sql.Identifier):
                            name = sub.get_real_name()
                            if name:
                                referenced_columns.add(self._strip_identifier(name))
                            break
                        if sub.ttype is sqlparse_tokens.Name:
                            referenced_columns.add(self._strip_identifier(sub.value))
                            break
            for table in tables:
                meta = self._schema_cache.structured.get(table)
                if not meta:
                    continue
                col_names = {c["name"] for c in meta.get("columns", [])}
                indexed_columns = {
                    c["name"] for c in meta.get("columns", []) if c.get("primary_key")
                }
                # Adapter index entries don't always carry per-column membership;
                # treat an index whose name equals a column name as a positive
                # signal (best-effort, same as the previous heuristic).
                for idx in meta.get("indexes", []):
                    name = idx.get("name", "")
                    if name in col_names:
                        indexed_columns.add(name)
                for col in referenced_columns:
                    if col in col_names and col not in indexed_columns:
                        warnings.append(
                            {
                                "code": "missing_index_hint",
                                "message": (
                                    f"WHERE on `{table}.{col}` has no covering "
                                    "index - consider adding one if this query "
                                    "runs often."
                                ),
                            }
                        )

        return _dumps({"sql": sql, "warnings": warnings})

    # ------------------------------------------------------------------
    # Biz-mode analytical tools
    # ------------------------------------------------------------------

    def _validate_column(self, table: str, column: str) -> None:
        meta = self._schema_cache.structured.get(table)
        if not meta:
            raise ValueError(f"Unknown table: {table}")
        names = {col["name"] for col in meta.get("columns", [])}
        if column not in names:
            raise ValueError(f"Unknown column '{column}' on table '{table}'.")

    def _validate_filter_sql(self, filter_sql: str | None) -> str:
        if not filter_sql:
            return ""
        statements = self._sql_parse(filter_sql)
        if len(statements) != 1:
            raise ValueError("filter_sql must be a single fragment without ';'.")
        fragment = statements[0]
        lowered = fragment.lower()
        forbidden = (
            "insert",
            "update",
            "delete",
            "drop",
            "alter",
            "create",
            "truncate",
            "grant",
            "revoke",
            "--",
            "/*",
        )
        for token in forbidden:
            if token in lowered:
                raise ValueError(f"filter_sql contains a forbidden token: {token!r}.")
        return fragment

    def _resolve_agg(self, agg: str, column: str) -> str:
        agg = (agg or "sum").lower()
        col = f"`{column}`"
        if agg == "count":
            return f"COUNT({col})"
        if agg == "sum":
            return f"SUM({col})"
        if agg == "avg":
            return f"AVG({col})"
        if agg == "min":
            return f"MIN({col})"
        if agg == "max":
            return f"MAX({col})"
        raise ValueError(f"Unsupported aggregate: {agg}")

    # ------------------------------------------------------------------
    # JOIN helpers shared by the analytical tools
    # ------------------------------------------------------------------

    def _build_join_clauses(
        self, base_table: str, joins: list[dict] | None
    ) -> tuple[str, dict[str, str]]:
        """Validate ``joins`` against the schema cache and emit SQL clauses.

        Returns ``(join_sql, alias_map)`` where ``alias_map[table_name]``
        gives the SQL alias to use when qualifying a column. The base table
        is always ``t0``; subsequent join targets are ``t1``, ``t2``, ...

        Each join entry must be a dict ``{target_table, on_column,
        target_column?, type?}``. ``on_column`` must be a foreign-key column
        on the base table pointing at ``target_table``; ``target_column``
        defaults to the FK target column. Anything else is rejected so the
        LLM cannot invent joins or smuggle in cross-products.
        """
        alias_map: dict[str, str] = {base_table: "t0"}
        if not joins:
            return "", alias_map

        base_meta = self._schema_cache.structured.get(base_table)
        if not base_meta:
            raise ValueError(f"Unknown table: {base_table}")
        base_fks = base_meta.get("foreign_keys", [])

        pieces: list[str] = []
        for index, join in enumerate(joins, start=1):
            if not isinstance(join, dict):
                raise ValueError("each join entry must be an object")
            target_table = join.get("target_table")
            on_column = join.get("on_column")
            target_column = join.get("target_column")
            join_kind = (join.get("type") or "LEFT").upper()
            if join_kind not in {"LEFT", "INNER"}:
                raise ValueError("join type must be 'LEFT' or 'INNER'")
            if not target_table or not on_column:
                raise ValueError("each join requires target_table and on_column")
            if target_table not in self._schema_cache.structured:
                raise ValueError(f"Unknown join target table: {target_table}")
            matching = [
                fk
                for fk in base_fks
                if fk.get("from") == on_column and fk.get("table") == target_table
            ]
            if not matching:
                raise ValueError(
                    f"No foreign key from {base_table}.{on_column} -> {target_table}."
                )
            if target_column is None:
                target_column = matching[0].get("to")
            alias = f"t{index}"
            alias_map[target_table] = alias
            pieces.append(
                f"{join_kind} JOIN `{target_table}` {alias} "
                f"ON t0.`{on_column}` = {alias}.`{target_column}`"
            )
        return " ".join(pieces), alias_map

    def _resolve_qualified(
        self, reference: str, alias_map: dict[str, str], base_table: str
    ) -> str:
        """Convert ``col`` or ``Table.col`` into ``aliasN.`col```.

        Bare names resolve to the base alias (``t0``). Qualified names must
        reference a table present in ``alias_map`` (i.e., the base table or
        a join target). The column is validated against the schema cache.
        """
        if "." in reference:
            tbl, _, col = reference.partition(".")
            alias = alias_map.get(tbl)
            if alias is None:
                raise ValueError(
                    f"Reference {reference!r} needs a join on {tbl!r}."
                )
            meta = self._schema_cache.structured.get(tbl) or {}
            names = {c["name"] for c in meta.get("columns", [])}
            if col not in names:
                raise ValueError(f"Unknown column {col!r} on table {tbl!r}.")
            return f"{alias}.`{col}`"
        # bare name -> base table
        self._validate_column(base_table, reference)
        return f"t0.`{reference}`"

    def _resolve_agg_qualified(
        self, agg: str, alias_map: dict[str, str], base_table: str, column: str
    ) -> str:
        agg = (agg or "sum").lower()
        col_expr = self._resolve_qualified(column, alias_map, base_table)
        if agg == "count":
            return f"COUNT({col_expr})"
        if agg == "sum":
            return f"SUM({col_expr})"
        if agg == "avg":
            return f"AVG({col_expr})"
        if agg == "min":
            return f"MIN({col_expr})"
        if agg == "max":
            return f"MAX({col_expr})"
        raise ValueError(f"Unsupported aggregate: {agg}")

    def _auto_display_join(
        self, base_table: str, group_column: str
    ) -> tuple[dict | None, str | None]:
        """If ``group_column`` is a single-FK column, propose an auto JOIN.

        Returns ``(join_dict, display_column)`` to be added to the user's
        joins, or ``(None, None)`` if no unambiguous resolution exists. The
        display column is the first non-PK text-typed column on the FK
        target table.
        """
        if not group_column.endswith("_id"):
            return None, None
        meta = self._schema_cache.structured.get(base_table) or {}
        fks = [fk for fk in meta.get("foreign_keys", []) if fk.get("from") == group_column]
        if len(fks) != 1:
            return None, None
        fk = fks[0]
        target_table = fk.get("table")
        target_meta = self._schema_cache.structured.get(target_table)
        if not target_meta:
            return None, None
        for col in target_meta.get("columns", []):
            if col.get("primary_key"):
                continue
            ctype = (col.get("type") or "").lower()
            if any(token in ctype for token in ("char", "text")):
                return (
                    {
                        "target_table": target_table,
                        "on_column": group_column,
                        "target_column": fk.get("to"),
                    },
                    f"{target_table}.{col['name']}",
                )
        return None, None

    def trend_over_time(
        self,
        table: str,
        value_column: str,
        time_column: str,
        granularity: str = "month",
        agg: str = "sum",
        filter_sql: str | None = None,
        joins: list[dict] | None = None,
    ) -> str:
        self._validate_identifier(table, "table name")
        self._validate_column(table, value_column)
        self._validate_column(table, time_column)
        join_sql, alias_map = self._build_join_clauses(table, joins or [])
        # date_bucket_expr emits backtick-only references; rewrap to use
        # the base-table alias so it composes with JOIN clauses.
        bucket = self._adapter.date_bucket_expr(time_column, granularity)
        bucket = bucket.replace(f"`{time_column}`", f"t0.`{time_column}`")
        agg_expr = self._resolve_agg_qualified(agg, alias_map, table, value_column)
        clean_filter = self._validate_filter_sql(filter_sql)
        where_clause = f"WHERE {clean_filter}" if clean_filter else ""
        sql = (
            f"SELECT {bucket} AS period, {agg_expr} AS value "
            f"FROM `{table}` t0 {join_sql} {where_clause} "
            f"GROUP BY period ORDER BY period"
        )
        cursor = self._adapter.execute(sql)
        rows = list(cursor.fetchall())
        series = [
            {"period": row[0], "value": float(row[1]) if row[1] is not None else 0.0}
            for row in rows
        ]
        slope = 0.0
        change_pct = 0.0
        inflection: str | None = None
        if len(series) >= 2:
            first = series[0]["value"]
            last = series[-1]["value"]
            if first not in (0.0, None):
                change_pct = (last - first) / abs(first) * 100.0
            xs = list(range(len(series)))
            ys = [point["value"] for point in series]
            mean_x = sum(xs) / len(xs)
            mean_y = sum(ys) / len(ys)
            numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
            denominator = sum((x - mean_x) ** 2 for x in xs) or 1.0
            slope = numerator / denominator
            # Inflection: largest absolute change between consecutive buckets.
            biggest = 0.0
            for index in range(1, len(series)):
                delta = abs(series[index]["value"] - series[index - 1]["value"])
                if delta > biggest:
                    biggest = delta
                    inflection = series[index]["period"]
        return _dumps(
            {
                "table": table,
                "value_column": value_column,
                "time_column": time_column,
                "granularity": granularity,
                "agg": agg,
                "series": series,
                "slope": slope,
                "change_pct": change_pct,
                "inflection_period": inflection,
                "interpretation": (
                    f"{agg.upper()} of {value_column} on {table}, bucketed by "
                    f"{granularity}. "
                    f"Change first->last: {change_pct:.1f}%."
                ),
            }
        )

    def compare_periods(
        self,
        table: str,
        value_column: str,
        time_column: str,
        period_a: dict,
        period_b: dict,
        breakdown_column: str | None = None,
        agg: str = "sum",
        joins: list[dict] | None = None,
    ) -> str:
        self._validate_identifier(table, "table name")
        self._validate_column(table, value_column)
        self._validate_column(table, time_column)
        for period_name, period in (("period_a", period_a), ("period_b", period_b)):
            if not isinstance(period, dict) or "start" not in period or "end" not in period:
                raise ValueError(f"{period_name} must be an object with 'start' and 'end'.")

        join_sql, alias_map = self._build_join_clauses(table, joins or [])
        agg_expr = self._resolve_agg_qualified(agg, alias_map, table, value_column)
        time_expr = self._resolve_qualified(time_column, alias_map, table)
        breakdown_expr = (
            self._resolve_qualified(breakdown_column, alias_map, table)
            if breakdown_column else None
        )
        token = self._adapter.parameter_token()

        def _scalar(period: dict) -> float:
            sql = (
                f"SELECT {agg_expr} FROM `{table}` t0 {join_sql} "
                f"WHERE {time_expr} >= {token} AND {time_expr} < {token}"
            )
            cursor = self._adapter.execute(sql, [period["start"], period["end"]])
            value = cursor.fetchone()[0]
            return float(value) if value is not None else 0.0

        a_value = _scalar(period_a)
        b_value = _scalar(period_b)
        delta = b_value - a_value
        change_pct = (delta / abs(a_value) * 100.0) if a_value not in (0.0, 0) else 0.0

        by_breakdown: list[dict] | None = None
        if breakdown_expr:
            sql = (
                f"SELECT {breakdown_expr} AS key_, "
                f"{agg_expr} AS value, "
                f"CASE WHEN {time_expr} >= {token} AND {time_expr} < {token} "
                f"THEN 'a' ELSE 'b' END AS bucket "
                f"FROM `{table}` t0 {join_sql} "
                f"WHERE ({time_expr} >= {token} AND {time_expr} < {token}) "
                f"   OR ({time_expr} >= {token} AND {time_expr} < {token}) "
                f"GROUP BY key_, bucket"
            )
            params = [
                period_a["start"], period_a["end"],
                period_a["start"], period_a["end"],
                period_b["start"], period_b["end"],
            ]
            cursor = self._adapter.execute(sql, params)
            rolled: dict[Any, dict[str, float]] = {}
            for row in cursor.fetchall():
                key, value, bucket = row
                rolled.setdefault(key, {"a": 0.0, "b": 0.0})
                rolled[key][bucket] = float(value) if value is not None else 0.0
            by_breakdown = []
            for key, values in rolled.items():
                a_v = values["a"]
                b_v = values["b"]
                pct = (b_v - a_v) / abs(a_v) * 100.0 if a_v not in (0.0, 0) else 0.0
                by_breakdown.append(
                    {
                        "key": key,
                        "a": a_v,
                        "b": b_v,
                        "change_pct": pct,
                    }
                )
            by_breakdown.sort(key=lambda item: item["b"] - item["a"], reverse=True)

        return _dumps(
            {
                "table": table,
                "value_column": value_column,
                "time_column": time_column,
                "agg": agg,
                "a": a_value,
                "b": b_value,
                "delta": delta,
                "change_pct": change_pct,
                "period_a": period_a,
                "period_b": period_b,
                "breakdown_column": breakdown_column,
                "by_breakdown": by_breakdown,
                "interpretation": (
                    f"{agg.upper()}({value_column}) changed by {change_pct:.1f}% "
                    f"from period A to period B."
                ),
            }
        )

    def top_n_breakdown(
        self,
        table: str,
        group_column: str,
        value_column: str,
        agg: str = "sum",
        n: int = 5,
        filter_sql: str | None = None,
        display_column: str | None = None,
        joins: list[dict] | None = None,
    ) -> str:
        self._validate_identifier(table, "table name")
        self._validate_column(table, group_column)
        self._validate_column(table, value_column)
        n = max(1, min(int(n), 50))

        # Auto-resolve FK label: if no explicit display_column and no joins,
        # try to follow a single FK on group_column to its target's first
        # text column. Falls through silently if no clean match.
        effective_joins = list(joins) if joins else []
        if display_column is None and not effective_joins:
            auto_join, auto_display = self._auto_display_join(table, group_column)
            if auto_join is not None:
                effective_joins.append(auto_join)
                display_column = auto_display

        join_sql, alias_map = self._build_join_clauses(table, effective_joins)
        agg_expr = self._resolve_agg_qualified(agg, alias_map, table, value_column)
        group_expr = self._resolve_qualified(group_column, alias_map, table)
        display_expr = (
            self._resolve_qualified(display_column, alias_map, table)
            if display_column else None
        )
        clean_filter = self._validate_filter_sql(filter_sql)
        where_clause = f"WHERE {clean_filter}" if clean_filter else ""

        agg_lower = (agg or "sum").lower()
        share_is_meaningful = agg_lower in {"sum", "count"}

        total: float | None = None
        if share_is_meaningful:
            total_sql = (
                f"SELECT {agg_expr} FROM `{table}` t0 {join_sql} {where_clause}"
            )
            cursor = self._adapter.execute(total_sql)
            total_row = cursor.fetchone()
            total = (
                float(total_row[0])
                if total_row and total_row[0] is not None
                else 0.0
            )

        select_parts = [f"{group_expr} AS group_id"]
        group_by_parts = ["group_id"]
        if display_expr:
            select_parts.append(f"{display_expr} AS group_label")
            group_by_parts.append("group_label")
        select_parts.append(f"{agg_expr} AS value")

        top_sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM `{table}` t0 {join_sql} {where_clause} "
            f"GROUP BY {', '.join(group_by_parts)} "
            f"ORDER BY value DESC LIMIT {n}"
        )
        cursor = self._adapter.execute(top_sql)
        rows = []
        for row in cursor.fetchall():
            group_id = row[0]
            if display_expr:
                label = row[1] if row[1] is not None else group_id
                value = float(row[2]) if row[2] is not None else 0.0
            else:
                label = group_id
                value = float(row[1]) if row[1] is not None else 0.0
            share = (value / total * 100.0) if (share_is_meaningful and total) else None
            rows.append(
                {
                    "group": label,
                    "group_id": group_id,
                    "value": value,
                    "share": share,
                }
            )

        if rows and share_is_meaningful and rows[0].get("share") is not None:
            interpretation = (
                f"Top {len(rows)} {group_column} by {agg.upper()}({value_column}). "
                f"Top entry holds {rows[0]['share']:.1f}% of total."
            )
        elif rows:
            interpretation = (
                f"Top {len(rows)} {group_column} by {agg.upper()}({value_column}). "
                f"`share` is not shown because '{agg_lower}' is not additive across "
                f"groups."
            )
        else:
            interpretation = "No data."

        return _dumps(
            {
                "table": table,
                "group_column": group_column,
                "value_column": value_column,
                "display_column": display_column,
                "joins": effective_joins or None,
                "agg": agg,
                "n": n,
                "total": total,
                "rows": rows,
                "interpretation": interpretation,
            }
        )

    def _histogram_from_values(
        self, values: list[float], bins: int
    ) -> tuple[dict, list[dict]]:
        """Compute summary stats + histogram from an in-memory list of floats.

        Used by ``distribution`` in aggregated mode (derived datasets that
        always fit in memory) and as a fallback for tiny tables. Returns
        ``(summary_dict, histogram_list)``.
        """
        n = len(values)
        if n == 0:
            empty_pct = {f"p{p}": None for p in (25, 50, 75, 90, 95, 99)}
            return (
                {"count": 0, "min": None, "max": None, "mean": None,
                 "median": None, "stddev": None, "percentiles": empty_pct,
                 "percentiles_sampled": False},
                [],
            )
        values_sorted = sorted(values)
        mn = values_sorted[0]
        mx = values_sorted[-1]
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / max(n - 1, 1)
        stddev = variance ** 0.5
        percentiles: dict[str, float] = {}
        for p in (25, 50, 75, 90, 95, 99):
            rank = max(1, min(n, int(round(p / 100.0 * n))))
            percentiles[f"p{p}"] = values_sorted[rank - 1]
        median = percentiles["p50"]

        if mx > mn:
            bin_width = (mx - mn) / bins
            edges = [mn + i * bin_width for i in range(bins + 1)]
            edges[-1] = mx
            counts = [0] * bins
            for v in values:
                if v >= mx:
                    idx = bins - 1
                else:
                    idx = min(bins - 1, max(0, int((v - mn) / bin_width)))
                counts[idx] += 1
            histogram = [
                {"bin_index": i, "bin_start": edges[i],
                 "bin_end": edges[i + 1], "count": counts[i]}
                for i in range(bins)
            ]
        else:
            histogram = [{
                "bin_index": 0, "bin_start": mn, "bin_end": mx, "count": n,
            }]
        return (
            {"count": n, "min": mn, "max": mx, "mean": mean, "median": median,
             "stddev": stddev, "percentiles": percentiles,
             "percentiles_sampled": False},
            histogram,
        )

    def distribution(
        self,
        table: str,
        column: str,
        bins: int = 10,
        filter_sql: str | None = None,
        joins: list[dict] | None = None,
        group_by: str | None = None,
        pre_agg: str | None = None,
    ) -> str:
        """Summary stats + histogram for a numeric column or per-group aggregate.

        Two modes:

        **Raw mode** (default): bins individual values of ``column``.
        ``distribution(table='trips', column='total_fare')`` => "histogram
        of trip fares".

        **Aggregated mode** (when ``group_by`` and ``pre_agg`` are set):
        first computes ``pre_agg(column)`` per ``group_by``, then bins
        those per-group values. ``distribution(table='reviews',
        column='rating', group_by='location_id', pre_agg='avg')``
        => "histogram of locations by their average review rating".

        ``joins`` works the same as in the other analytical tools - pass
        ``[{target_table, on_column, ...}]`` to follow foreign keys, then
        qualify ``column`` / ``group_by`` as ``Target.col`` to reach the
        joined table.

        Returns count, min, max, mean, median, stddev, percentiles
        (p25/p50/p75/p90/p95/p99), and a ``bins``-bucket histogram.
        """
        self._validate_identifier(table, "table name")
        try:
            bins = int(bins)
        except (TypeError, ValueError):
            bins = 10
        bins = max(2, min(bins, 50))
        clean_filter = self._validate_filter_sql(filter_sql)
        where_clause = f"WHERE {clean_filter}" if clean_filter else ""

        # Build the JOIN clauses + qualified expressions ONCE up front.
        join_sql, alias_map = self._build_join_clauses(table, joins or [])
        value_expr = self._resolve_qualified(column, alias_map, table)

        aggregated = bool(group_by)
        if aggregated:
            if not pre_agg:
                raise ValueError("group_by requires pre_agg (sum/avg/count/min/max)")
            group_expr = self._resolve_qualified(group_by, alias_map, table)
            agg_expr = self._resolve_agg_qualified(
                pre_agg, alias_map, table, column
            )
            source_sql = (
                f"SELECT {group_expr} AS k, {agg_expr} AS v "
                f"FROM `{table}` t0 {join_sql} {where_clause} "
                f"GROUP BY k HAVING v IS NOT NULL "
                f"LIMIT {MAX_RESULT_ROWS}"
            )
            self._adapter.set_query_timeout(QUERY_TIMEOUT_MS)
            cursor = self._adapter.execute(source_sql)
            values = [float(r[1]) for r in cursor.fetchall() if r[1] is not None]
            summary, histogram = self._histogram_from_values(values, bins)
            summary["mode"] = "aggregated"
            summary["groups"] = len(values)
            interpretation_subject = (
                f"{pre_agg.upper()}({column}) per {group_by}"
            )
            n = len(values)
            mn = summary["min"]
            mx = summary["max"]
            mean = summary["mean"]
            stddev = summary["stddev"]
            median = summary["median"]
            sampled = False
        else:
            # Raw mode - keep the SQL-based exact stats so a 10M-row table
            # doesn't have to materialize through Python. Percentiles are
            # still computed from a random sample when the table is huge.
            col_alias = "v"
            # Stats round trip
            stats_sql = (
                f"SELECT COUNT({value_expr}) AS n, MIN({value_expr}) AS mn, "
                f"MAX({value_expr}) AS mx, AVG({value_expr}) AS avg_, "
                f"STDDEV({value_expr}) AS sd "
                f"FROM `{table}` t0 {join_sql} {where_clause}"
            )
            self._adapter.set_query_timeout(QUERY_TIMEOUT_MS)
            cursor = self._adapter.execute(stats_sql)
            n, mn, mx, mean, stddev = cursor.fetchone()
            if not n:
                return _dumps(
                    {
                        "table": table, "column": column, "bins": bins,
                        "mode": "raw",
                        "summary": {"count": 0},
                        "histogram": [],
                        "interpretation": f"No non-null values in {table}.{column}.",
                    }
                )
            mn = float(mn) if mn is not None else 0.0
            mx = float(mx) if mx is not None else 0.0
            mean = float(mean) if mean is not None else 0.0
            stddev = float(stddev) if stddev is not None else 0.0
            n = int(n)

            # Percentiles via Python on a sample (large tables) or full sort.
            sampled = n > MAX_RESULT_ROWS
            if sampled:
                rand_expr = (
                    "RAND()" if self.settings.get("type") == "mysql" else "RANDOM()"
                )
                values_sql = (
                    f"SELECT {value_expr} AS {col_alias} "
                    f"FROM `{table}` t0 {join_sql} "
                    f"{where_clause} {'AND' if where_clause else 'WHERE'} "
                    f"{value_expr} IS NOT NULL "
                    f"ORDER BY {rand_expr} LIMIT {MAX_RESULT_ROWS}"
                )
            else:
                values_sql = (
                    f"SELECT {value_expr} AS {col_alias} "
                    f"FROM `{table}` t0 {join_sql} "
                    f"{where_clause} {'AND' if where_clause else 'WHERE'} "
                    f"{value_expr} IS NOT NULL "
                    f"ORDER BY {value_expr} ASC"
                )
            cursor = self._adapter.execute(values_sql)
            values = [float(r[0]) for r in cursor.fetchall() if r[0] is not None]
            if sampled:
                values.sort()
            percentile_targets = (25, 50, 75, 90, 95, 99)
            percentiles: dict[str, float | None] = {}
            if values:
                for p in percentile_targets:
                    rank = max(1, min(len(values), int(round(p / 100.0 * len(values)))))
                    percentiles[f"p{p}"] = values[rank - 1]
            else:
                for p in percentile_targets:
                    percentiles[f"p{p}"] = None
            median = percentiles.get("p50")

            # Histogram via SQL CASE bucketing (exact, single round trip).
            histogram = []
            if mx > mn:
                bin_width = (mx - mn) / bins
                edges = [mn + i * bin_width for i in range(bins + 1)]
                edges[-1] = mx
                case_parts = []
                for index in range(bins):
                    low = edges[index]
                    high = edges[index + 1]
                    if index == bins - 1:
                        cond = f"{value_expr} >= {low} AND {value_expr} <= {high}"
                    else:
                        cond = f"{value_expr} >= {low} AND {value_expr} < {high}"
                    case_parts.append(
                        f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END) AS b{index}"
                    )
                hist_sql = (
                    f"SELECT {', '.join(case_parts)} "
                    f"FROM `{table}` t0 {join_sql} {where_clause}"
                )
                cursor = self._adapter.execute(hist_sql)
                counts = cursor.fetchone()
                histogram = [
                    {
                        "bin_index": i,
                        "bin_start": edges[i],
                        "bin_end": edges[i + 1],
                        "count": int(counts[i] or 0),
                    }
                    for i in range(bins)
                ]
            else:
                histogram = [{
                    "bin_index": 0, "bin_start": mn, "bin_end": mx,
                    "count": int(n),
                }]
            summary = {
                "count": int(n), "min": mn, "max": mx, "mean": mean,
                "median": median, "stddev": stddev,
                "percentiles": percentiles, "percentiles_sampled": sampled,
                "mode": "raw",
            }
            interpretation_subject = f"{column} on {table}"

        spread = (mx - mn) if (mn is not None and mx is not None) else 0
        skew_note = ""
        if median is not None and stddev and stddev > 0:
            distance = (mean - median) / stddev
            if distance > 0.5:
                skew_note = " Distribution is right-skewed (mean > median)."
            elif distance < -0.5:
                skew_note = " Distribution is left-skewed (mean < median)."
        interpretation = (
            f"{interpretation_subject}: "
            f"{summary['count']:,} values, range {(mn or 0):,.2f} to {(mx or 0):,.2f} "
            f"(spread {spread:,.2f}). Mean {(mean or 0):,.2f}, median "
            f"{(median if median is not None else 0):,.2f}, stddev "
            f"{(stddev or 0):,.2f}.{skew_note}"
        )

        return _dumps(
            {
                "table": table,
                "column": column,
                "bins": bins,
                "group_by": group_by,
                "pre_agg": pre_agg,
                "joins": joins or None,
                "summary": summary,
                "histogram": histogram,
                "interpretation": interpretation,
            }
        )

    def generate_chart(self, spec: dict) -> str:
        if not isinstance(spec, dict):
            raise ValueError("Chart spec must be an object.")
        if "mark" not in spec:
            return _dumps({"error": "Chart spec must include 'mark'."})
        if "data" not in spec:
            return _dumps({"error": "Chart spec must include 'data'."})
        encoding = spec.get("encoding") or {}
        for channel in ("x", "y"):
            if channel in encoding and isinstance(encoding[channel], dict):
                field = encoding[channel].get("field")
                if field and "title" not in encoding[channel]:
                    encoding[channel]["title"] = " ".join(
                        part.capitalize() for part in str(field).replace("_", " ").split()
                    )
        vega_spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "title": spec.get("title"),
            "mark": spec["mark"],
            "data": spec["data"],
            "encoding": encoding,
            "width": spec.get("width", "container"),
            "height": spec.get("height", 300),
        }
        return _dumps({"vega_lite": vega_spec})

    # ------------------------------------------------------------------
    # Pending changes view
    # ------------------------------------------------------------------

    def get_pending_changes(self) -> str:
        changes: list[dict] = []
        next_id = 1
        i = 0
        stack = self._savepoint_stack
        while i < len(stack):
            sp = stack[i]
            if sp["tool"] == "begin_transaction":
                label = sp.get("label") or sp["description"]
                children: list[dict] = []
                j = i + 1
                closed = False
                while j < len(stack):
                    inner = stack[j]
                    if inner["tool"] == "commit_transaction" and inner.get("label") == label:
                        closed = True
                        break
                    if inner["tool"] == "begin_transaction":
                        break
                    children.append(
                        {
                            "id": next_id,
                            "tool": inner["tool"],
                            "description": inner["description"],
                            "timestamp": inner["timestamp"],
                        }
                    )
                    next_id += 1
                    j += 1
                if closed:
                    changes.append(
                        {
                            "type": "transaction",
                            "label": label,
                            "started_at": sp["timestamp"],
                            "children": children,
                        }
                    )
                    i = j + 1
                    continue
                # No matching commit_transaction yet — render the marker on its own.
                changes.append(
                    {
                        "id": next_id,
                        "type": "transaction_open",
                        "label": label,
                        "started_at": sp["timestamp"],
                        "children": children,
                    }
                )
                next_id += 1
                i = j
                continue

            if sp["tool"] == "commit_transaction":
                # Unmatched commit marker — surface so the user can see it.
                changes.append(
                    {
                        "id": next_id,
                        "tool": sp["tool"],
                        "description": sp["description"],
                        "timestamp": sp["timestamp"],
                    }
                )
                next_id += 1
                i += 1
                continue

            changes.append(
                {
                    "id": next_id,
                    "tool": sp["tool"],
                    "description": sp["description"],
                    "timestamp": sp["timestamp"],
                }
            )
            next_id += 1
            i += 1

        return _dumps(
            {
                "pending_count": len(self._savepoint_stack),
                "changes": changes,
            }
        )

    def close(self) -> None:
        if not self.conn:
            return
        if self._savepoint_stack:
            self._adapter.commit()
            self._savepoint_stack.clear()
        self._adapter.close()
        self.conn = None
        self._audit("close", backend=self.settings["type"])


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": (
                "List all tables and views in the database. "
                "Call this first to discover what data is available."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_table",
            "description": (
                "Describe the columns of a specific table or view. "
                "Use this before querying to understand the structure."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Name of the table to describe",
                    }
                },
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_query",
            "description": (
                "Execute a read-only SELECT query. Returns columns, total row "
                "count, and a small preview only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "The SELECT SQL query to execute",
                    }
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_record",
            "description": "Insert a new record into a table.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Name of the table to insert into",
                    },
                    "data": {
                        "type": "object",
                        "description": "Column-value pairs for the new row",
                    },
                },
                "required": ["table_name", "data"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_record",
            "description": "Update existing records in a table using a WHERE condition.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Name of the table to update",
                    },
                    "data": {
                        "type": "object",
                        "description": "Columns to update and their new values",
                    },
                    "condition": {
                        "type": "string",
                        "description": "SQL WHERE clause without the WHERE keyword",
                    },
                },
                "required": ["table_name", "data", "condition"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_record",
            "description": "Delete records from a table using a WHERE condition.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Name of the table to delete from",
                    },
                    "condition": {
                        "type": "string",
                        "description": "SQL WHERE clause without the WHERE keyword",
                    },
                },
                "required": ["table_name", "condition"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_view",
            "description": "Create a named SQL view from a SELECT query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "view_name": {
                        "type": "string",
                        "description": "Name for the new view",
                    },
                    "query": {
                        "type": "string",
                        "description": "The SELECT query that defines the view",
                    },
                },
                "required": ["view_name", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drop_view",
            "description": "Drop an existing SQL view.",
            "parameters": {
                "type": "object",
                "properties": {
                    "view_name": {
                        "type": "string",
                        "description": "Name of the view to drop",
                    }
                },
                "required": ["view_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_schema",
            "description": "Get the complete table schema for the current database.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_records",
            "description": "Count records in a table, optionally filtered by a condition.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Name of the table to count rows in",
                    },
                    "condition": {
                        "type": "string",
                        "description": "Optional WHERE clause without the WHERE keyword",
                    },
                },
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_indexes",
            "description": "List database indexes, optionally filtered to one table.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Optional table name to filter indexes for",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_index",
            "description": "Create an index on one or more columns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index_name": {
                        "type": "string",
                        "description": "Name for the new index",
                    },
                    "table_name": {
                        "type": "string",
                        "description": "Table to create the index on",
                    },
                    "columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Column names to include in the index",
                    },
                    "unique": {
                        "type": "boolean",
                        "description": "If true, create a UNIQUE index",
                    },
                },
                "required": ["index_name", "table_name", "columns"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drop_index",
            "description": "Drop an existing index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index_name": {
                        "type": "string",
                        "description": "Name of the index to drop",
                    }
                },
                "required": ["index_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_foreign_keys",
            "description": "Show foreign-key relationships for a table.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Name of the table to inspect",
                    }
                },
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_query",
            "description": "Export SELECT results to a local CSV or JSON file under exports/.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "The SELECT query to execute and export",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["json", "csv"],
                        "description": "Output format",
                    },
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_sql",
            "description": (
                "Execute arbitrary SQL for operations not covered by other tools. "
                "Backend safety policies may block dangerous statements."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "The SQL statement to execute",
                    }
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "undo",
            "description": "Undo the last N pending database changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "integer",
                        "description": "Number of operations to undo",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commit_changes",
            "description": "Permanently save all pending database changes.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pending_changes",
            "description": "List all uncommitted database changes awaiting approval.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_schema_summary",
            "description": (
                "Return the cached schema summary for the current or requested "
                "mode. Use this instead of repeatedly listing tables and "
                "columns - it is already in your system prompt."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["technical", "business"],
                        "description": "Which summary flavor to return",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refresh_schema",
            "description": (
                "Rebuild the schema cache after structural changes. Call this "
                "if you just committed DDL or suspect the cache is stale."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain_query",
            "description": (
                "Show the query plan for a SELECT. Use before expensive reads "
                "or before adding indexes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "The SELECT or WITH ... SELECT statement to analyse",
                    }
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dry_run_mutation",
            "description": (
                "Count and preview rows an UPDATE or DELETE would touch, "
                "without staging the mutation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "An UPDATE or DELETE statement to simulate",
                    }
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "begin_transaction",
            "description": (
                "Open a labeled logical transaction. Subsequent mutations are "
                "grouped under this label in the pending-changes view until "
                "commit_transaction is called."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Short label for this transaction (e.g. 'cleanup')",
                    }
                },
                "required": ["label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commit_transaction",
            "description": (
                "Close a labeled logical transaction opened with "
                "begin_transaction. Does not persist - the user still has to "
                "commit_changes for the whole stack."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Label of the transaction to close",
                    }
                },
                "required": ["label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lint_query",
            "description": (
                "Static-analyze a SELECT for common issues: Cartesian joins, "
                "SELECT * on wide tables, missing-index hints."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "The SELECT or WITH ... SELECT to lint",
                    }
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trend_over_time",
            "description": (
                "Bucket a numeric value column by a time column at the chosen "
                "granularity. Returns a time series plus slope and change. "
                "Use `joins` to follow foreign keys to related tables."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "value_column": {"type": "string"},
                    "time_column": {"type": "string"},
                    "granularity": {
                        "type": "string",
                        "enum": ["day", "week", "month", "quarter"],
                    },
                    "agg": {
                        "type": "string",
                        "enum": ["sum", "avg", "count", "min", "max"],
                    },
                    "filter_sql": {
                        "type": "string",
                        "description": "Optional WHERE fragment (no SELECT keywords)",
                    },
                    "joins": {
                        "type": "array",
                        "description": (
                            "Optional LEFT JOINs to related tables via existing "
                            "foreign keys. Each entry: {target_table, on_column, "
                            "target_column?, type?}."
                        ),
                        "items": {"type": "object"},
                    },
                },
                "required": ["table", "value_column", "time_column"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_periods",
            "description": (
                "Compare an aggregate across two time periods, optionally "
                "broken down by a categorical column. Use `joins` to "
                "follow foreign keys to related tables."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "value_column": {"type": "string"},
                    "time_column": {"type": "string"},
                    "period_a": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                        },
                        "required": ["start", "end"],
                    },
                    "period_b": {
                        "type": "object",
                        "properties": {
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                        },
                        "required": ["start", "end"],
                    },
                    "breakdown_column": {
                        "type": "string",
                        "description": (
                            "Column to break down by. Bare name (on base table) "
                            "or 'Target.col' if you passed a join."
                        ),
                    },
                    "agg": {
                        "type": "string",
                        "enum": ["sum", "avg", "count", "min", "max"],
                    },
                    "joins": {
                        "type": "array",
                        "description": (
                            "Optional LEFT JOINs to related tables via existing "
                            "foreign keys."
                        ),
                        "items": {"type": "object"},
                    },
                },
                "required": [
                    "table",
                    "value_column",
                    "time_column",
                    "period_a",
                    "period_b",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "top_n_breakdown",
            "description": (
                "Rank a categorical column by an aggregate, returning the top "
                "N rows with each row's share of the total. If `group_column` "
                "is a foreign key (e.g. category_id) the tool will auto-resolve "
                "a friendly label from the linked table. Pass `display_column` "
                "(e.g. 'Categories.category_name') and/or `joins` for explicit "
                "control."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "group_column": {"type": "string"},
                    "value_column": {"type": "string"},
                    "display_column": {
                        "type": "string",
                        "description": (
                            "Optional friendly label column. Bare name (on base "
                            "table) or 'Target.col' for a joined target."
                        ),
                    },
                    "agg": {
                        "type": "string",
                        "enum": ["sum", "avg", "count", "min", "max"],
                    },
                    "n": {"type": "integer", "minimum": 1, "maximum": 50},
                    "filter_sql": {"type": "string"},
                    "joins": {
                        "type": "array",
                        "description": (
                            "Optional LEFT JOINs to related tables. Each entry: "
                            "{target_table, on_column, target_column?, type?}."
                        ),
                        "items": {"type": "object"},
                    },
                },
                "required": ["table", "group_column", "value_column"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "distribution",
            "description": (
                "Summary stats + histogram for a numeric column or for a "
                "per-group aggregate. Returns count, min, max, mean, median, "
                "stddev, percentiles (p25 to p99), and a `bins`-bucket "
                "histogram.\n\n"
                "Raw mode (default): bins individual values of `column`. "
                "Example: distribution(table='trips', column='total_fare') "
                "= 'how are individual trip fares distributed?'.\n\n"
                "Aggregated mode (when `group_by` and `pre_agg` are set): "
                "computes `pre_agg(column)` per `group_by` first, then bins "
                "the per-group values. Example: "
                "distribution(table='reviews', column='rating', "
                "group_by='location_id', pre_agg='avg') = 'histogram of "
                "locations by their average review rating'.\n\n"
                "Use `joins` and qualified column names (e.g. 'Target.col') "
                "to follow foreign keys to related tables."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "string"},
                    "column": {
                        "type": "string",
                        "description": (
                            "Numeric column to analyse. Bare name (on base "
                            "table) or 'Target.col' for a joined target."
                        ),
                    },
                    "bins": {
                        "type": "integer",
                        "minimum": 2,
                        "maximum": 50,
                        "description": "Number of histogram bins (default 10)",
                    },
                    "filter_sql": {
                        "type": "string",
                        "description": "Optional WHERE fragment",
                    },
                    "joins": {
                        "type": "array",
                        "description": (
                            "Optional LEFT JOINs to follow foreign keys. "
                            "Each entry: {target_table, on_column, "
                            "target_column?, type?}."
                        ),
                        "items": {"type": "object"},
                    },
                    "group_by": {
                        "type": "string",
                        "description": (
                            "Optional. If set, the tool first computes "
                            "pre_agg(column) per this group_by column, then "
                            "bins the per-group values (aggregated mode)."
                        ),
                    },
                    "pre_agg": {
                        "type": "string",
                        "enum": ["sum", "avg", "count", "min", "max"],
                        "description": (
                            "Required when group_by is set. The aggregate to "
                            "apply per group before binning."
                        ),
                    },
                },
                "required": ["table", "column"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_chart",
            "description": (
                "Return a Vega-Lite chart specification for the UI to render. "
                "Spec must include mark, data, and encoding."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spec": {
                        "type": "object",
                        "description": "Vega-Lite-shaped spec",
                    }
                },
                "required": ["spec"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": (
                "Page through the cached rows of a previous SELECT-style tool "
                "call (run_query, export_query, sample_rows). Use this when "
                "the preview was not enough or when truncated_at_cap was true "
                "- do NOT re-run the SELECT just to see more rows."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query_id": {
                        "type": "string",
                        "description": "query_id returned by an earlier tool call",
                    },
                    "page": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Zero-indexed page to fetch",
                    },
                    "page_size": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "description": "Rows per page (default 50, max 500)",
                    },
                },
                "required": ["query_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sample_rows",
            "description": (
                "Return a random sample of rows from a table. Prefer this over "
                "SELECT * for tables that may be large - it is bounded and "
                "cannot blow up the response."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {
                        "type": "string",
                        "description": "Name of the table to sample",
                    },
                    "n": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": "Number of rows to sample (default 20)",
                    },
                    "seed": {
                        "type": "integer",
                        "description": "Optional seed for reproducible MySQL samples",
                    },
                },
                "required": ["table_name"],
            },
        },
    },
]
