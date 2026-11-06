"""Cached database schema with structured + summary representations.

The cache is owned by ``DatabaseServer`` and built once on connect. It uses
the server's existing adapter introspection paths so we don't duplicate any
backend-specific SQL. Mode-aware summaries are exposed via ``tech_summary``
and ``biz_summary``; the full structured view is exposed via ``structured``.

The cache is invalidated by ``rebuild()`` (called manually via the
``refresh_schema`` tool, or automatically after a DDL commit).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db_server import DatabaseServer


def _friendly_label(name: str) -> str:
    """Turn ``order_date`` into ``Order Date`` for biz-mode summaries."""
    return " ".join(part.capitalize() for part in name.replace("-", "_").split("_"))


class SchemaCache:
    """Snapshot of the database schema, refreshable on demand."""

    def __init__(self, server: "DatabaseServer"):
        self._server = server
        self.structured: dict[str, dict] = {}
        self.tech_summary: str = ""
        self.biz_summary: str = ""
        self._adjacency: dict[str, list[str]] = {}
        self.rebuild()

    def rebuild(self) -> None:
        """Re-introspect the database and rebuild all derived artifacts."""
        adapter = self._server._adapter
        structured: dict[str, dict] = {}

        tables = adapter.list_tables()
        for entry in tables:
            table_name = entry["name"]
            kind = entry.get("type", "table")
            columns = adapter.describe_table(table_name)
            try:
                indexes = adapter.list_indexes(table_name)
            except Exception:
                indexes = []
            try:
                fks = adapter.get_foreign_keys(table_name) if kind == "table" else []
            except Exception:
                fks = []
            try:
                row_count = self._approx_row_count(table_name)
            except Exception:
                row_count = None

            structured[table_name] = {
                "type": kind,
                "columns": columns,
                "indexes": indexes,
                "foreign_keys": fks,
                "approx_row_count": row_count,
            }

        self.structured = structured
        self._adjacency = self._build_adjacency(structured)
        self.tech_summary = self._render_tech_summary(structured)
        self.biz_summary = self._render_biz_summary(structured)

    def summary_for(self, mode_name: str | None) -> str:
        if mode_name == "business":
            return self.biz_summary
        return self.tech_summary

    def _approx_row_count(self, table_name: str) -> int | None:
        """Return an approximate row count for ``table_name``.

        On MySQL we read ``information_schema.TABLES.TABLE_ROWS``, an InnoDB
        statistic that the server updates periodically. Reading it is O(1)
        and works even on multi-million-row tables; the trade-off is a stale
        value (minutes to hours), which is acceptable for "roughly how big
        is this?" purposes.

        On SQLite (and any other backend without that stat) we fall back to
        ``SELECT COUNT(*)``.
        """
        adapter = self._server._adapter
        if adapter.settings.get("type") == "mysql":
            try:
                cursor = adapter.execute(
                    "SELECT TABLE_ROWS FROM information_schema.TABLES "
                    "WHERE table_schema = %s AND table_name = %s",
                    (adapter.settings["name"], table_name),
                )
                row = cursor.fetchone()
                if row and row[0] is not None:
                    return int(row[0])
            except Exception:  # noqa: BLE001 - fall through to COUNT(*)
                pass
        cursor = adapter.execute(f"SELECT COUNT(*) FROM `{table_name}`")
        row = cursor.fetchone()
        return int(row[0]) if row else None

    def _build_adjacency(self, structured: dict[str, dict]) -> dict[str, list[str]]:
        graph: dict[str, set[str]] = {name: set() for name in structured}
        for name, meta in structured.items():
            for fk in meta.get("foreign_keys", []):
                target = fk.get("table")
                if target and target in graph:
                    graph[name].add(target)
                    graph[target].add(name)
        return {key: sorted(values) for key, values in graph.items()}

    def _render_tech_summary(self, structured: dict[str, dict]) -> str:
        lines: list[str] = ["# Database schema", ""]
        for name in sorted(structured):
            meta = structured[name]
            kind = meta["type"]
            row_count = meta.get("approx_row_count")
            count_str = f" (~{row_count} rows)" if row_count is not None else ""
            lines.append(f"## {name} [{kind}]{count_str}")

            col_parts = []
            for col in meta["columns"]:
                pieces = [f"`{col['name']}` {col['type']}"]
                if col.get("primary_key"):
                    pieces.append("PK")
                if col.get("notnull"):
                    pieces.append("NOT NULL")
                col_parts.append(" ".join(pieces))
            lines.append("Columns: " + ", ".join(col_parts))

            fks = meta.get("foreign_keys", [])
            if fks:
                fk_strs = [f"{fk['from']} -> {fk['table']}.{fk['to']}" for fk in fks]
                lines.append("Foreign keys: " + "; ".join(fk_strs))

            indexes = meta.get("indexes", [])
            if indexes:
                idx_strs = [
                    idx.get("name", "?") + (" [unique]" if idx.get("unique") else "")
                    for idx in indexes
                ]
                lines.append("Indexes: " + ", ".join(idx_strs))

            lines.append("")
        return "\n".join(lines).rstrip()

    def _render_biz_summary(self, structured: dict[str, dict]) -> str:
        lines: list[str] = ["# Available data", ""]
        for name in sorted(structured):
            meta = structured[name]
            if meta["type"] != "table":
                continue
            row_count = meta.get("approx_row_count")
            count_str = f" (~{row_count} records)" if row_count is not None else ""
            lines.append(f"## {_friendly_label(name)}{count_str}")
            friendly_cols = [_friendly_label(col["name"]) for col in meta["columns"]]
            lines.append("Available fields: " + ", ".join(friendly_cols))

            related = self._adjacency.get(name, [])
            if related:
                lines.append(
                    "Related to: " + ", ".join(_friendly_label(r) for r in related)
                )
            lines.append("")
        return "\n".join(lines).rstrip()
