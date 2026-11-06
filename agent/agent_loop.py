"""Agentic loop driving the database tools via an OpenRouter-backed LLM."""

import json

from mcp_server.db_server import DatabaseServer, TOOL_DEFINITIONS

from .llm_client import LLMClient
from .modes import Mode, TECHNICAL_SYSTEM_PROMPT


# Tool-name -> response-field mapping for the per-turn artifact bag. Any
# tool whose JSON result has the listed key(s) gets surfaced to the
# frontend as a structured artifact alongside the textual reply.
_ARTIFACT_KEYS: dict[str, str] = {
    "explain_query": "explain",
    "dry_run_mutation": "dry_run",
    "trend_over_time": "analytics",
    "compare_periods": "analytics",
    "top_n_breakdown": "analytics",
    "distribution": "analytics",
    "generate_chart": "chart",
    # Row-returning tools - surface as a table in the chat bubble so the
    # model does NOT describe individual rows in text.
    "run_query": "table",
    "sample_rows": "table",
    "fetch_page": "table",
    "export_query": "table",
}

_ANALYTICS_KIND: dict[str, str] = {
    "trend_over_time": "trend",
    "compare_periods": "compare",
    "top_n_breakdown": "top_n",
    "distribution": "distribution",
}


class AgentLoop:
    """Multi-turn agent that dispatches LLM tool calls to ``DatabaseServer``.

    Maintains conversation history across calls to ``send`` so the LLM
    has the full context of the session. Call ``reset`` to clear history.

    When constructed with a ``mode`` profile, the loop:
      * builds the system prompt from ``mode.system_prompt_template`` with
        ``{{schema_summary}}`` substituted from the database's schema cache;
      * filters ``TOOL_DEFINITIONS`` down to ``mode.allowed_tools`` before
        each LLM call.
    """

    def __init__(
        self,
        llm: LLMClient,
        db: DatabaseServer,
        max_iterations: int = 10,
        on_tool_call=None,
        mode: Mode | None = None,
    ):
        self._llm = llm
        self._db = db
        self._max_iterations = max_iterations
        self._on_tool_call = on_tool_call  # Optional: (tool_name, args, result) -> None
        self.mode = mode
        self._last_turn_artifacts: dict = self._fresh_artifacts()

        self._allowed_tool_defs = self._filter_tool_defs(mode)
        self._history: list[dict] = [
            {"role": "system", "content": self._build_system_prompt()}
        ]

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        if self.mode is None:
            return TECHNICAL_SYSTEM_PROMPT
        try:
            summary = self._db._schema_cache.summary_for(self.mode.name)
        except AttributeError:
            summary = ""
        return self.mode.system_prompt_template.replace("{{schema_summary}}", summary)

    def _filter_tool_defs(self, mode: Mode | None) -> list[dict]:
        if mode is None:
            return TOOL_DEFINITIONS
        return [t for t in TOOL_DEFINITIONS if t["function"]["name"] in mode.allowed_tools]

    @staticmethod
    def _fresh_artifacts() -> dict:
        return {
            "chart": None,
            "explain": None,
            "dry_run": None,
            "analytics": None,
            "table": None,
        }

    def refresh_schema_summary(self) -> None:
        """Rebuild the system prompt with the latest schema summary.

        Call after a DDL commit so the agent's prompt reflects new tables,
        views, or indexes. Preserves conversation history.
        """
        if self.mode is None or not self._history:
            return
        self._history[0] = {"role": "system", "content": self._build_system_prompt()}

    def pop_last_turn_artifacts(self) -> dict:
        """Return artifacts collected during the most recent ``send`` call.

        The bag is cleared so the next call starts fresh.
        """
        result = self._last_turn_artifacts
        self._last_turn_artifacts = self._fresh_artifacts()
        return result

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def send(self, user_text: str) -> str:
        """Run one user-turn through the tool loop and return the final reply."""
        self._history.append({"role": "user", "content": user_text})
        self._last_turn_artifacts = self._fresh_artifacts()

        for _ in range(self._max_iterations):
            msg = self._llm.chat(self._history, self._allowed_tool_defs)
            self._history.append(_assistant_to_dict(msg))

            if not msg.tool_calls:
                return msg.content or ""

            for tc in msg.tool_calls:
                args = _parse_arguments(tc.function.arguments)
                result = self._db.execute_tool(tc.function.name, args)
                self._capture_artifact(tc.function.name, result)
                if self._on_tool_call:
                    self._on_tool_call(tc.function.name, args, result)
                self._history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                )

        # Iteration cap reached — force a non-tool summary.
        self._history.append(
            {
                "role": "user",
                "content": (
                    "You have hit the tool-call iteration cap. "
                    "Stop calling tools and summarize what you accomplished "
                    "and what is still pending."
                ),
            }
        )
        msg = self._llm.chat(self._history, self._allowed_tool_defs)
        self._history.append(_assistant_to_dict(msg))
        return msg.content or "(agent reached max iterations without a final reply)"

    def _capture_artifact(self, tool_name: str, result_json: str) -> None:
        field = _ARTIFACT_KEYS.get(tool_name)
        if not field:
            return
        try:
            payload = json.loads(result_json)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("error"):
            return
        if field == "analytics":
            self._last_turn_artifacts[field] = {
                "kind": _ANALYTICS_KIND.get(tool_name, tool_name),
                "data": payload,
            }
        elif field == "chart":
            self._last_turn_artifacts[field] = payload.get("vega_lite") and {
                "vega_lite": payload["vega_lite"]
            }
        elif field == "table":
            # Row-returning tools have different shapes - normalize.
            # run_query / export_query: rows live in `preview` (≤10/5);
            # sample_rows / fetch_page: `rows` carries the whole batch.
            rows = payload.get("rows")
            if rows is None:
                rows = payload.get("preview") or []
            columns = payload.get("columns") or []
            if not columns and not rows:
                return  # nothing useful to render
            self._last_turn_artifacts[field] = {
                "tool": tool_name,
                "query_id": payload.get("query_id"),
                "table_name": payload.get("table") or payload.get("table_name"),
                "columns": columns,
                "rows": rows,
                "row_count": payload.get("row_count") or len(rows),
                "truncated_at_cap": payload.get("truncated_at_cap", False),
                "has_more": payload.get("has_more", False),
                "page": payload.get("page"),
                "page_count": payload.get("page_count"),
                "total_rows": payload.get("total_rows"),
            }
        else:
            self._last_turn_artifacts[field] = payload

    def reset(self) -> None:
        """Clear conversation history, keeping the system prompt."""
        self._history = [self._history[0]]
        self._last_turn_artifacts = self._fresh_artifacts()

    def notify_committed(self, committed_count: int) -> None:
        """Tell the agent the user clicked Commit in the UI.

        Without this, the agent's history still shows mutations as
        pending and it will lie to the user on the next turn.
        """
        plural = "s" if committed_count != 1 else ""
        self._history.append(
            {
                "role": "user",
                "content": (
                    f"[SYSTEM EVENT] The user clicked Commit in the UI. "
                    f"All {committed_count} pending change{plural} are now "
                    f"permanently saved to disk and are no longer pending."
                ),
            }
        )

    def notify_undone(self, descriptions: list[str]) -> None:
        """Tell the agent the user clicked Undo in the UI."""
        desc_text = "; ".join(descriptions) if descriptions else "the last change"
        self._history.append(
            {
                "role": "user",
                "content": (
                    f"[SYSTEM EVENT] The user clicked Undo in the UI. "
                    f"The following pending change(s) were rolled back and "
                    f"no longer apply: {desc_text}."
                ),
            }
        )


def _assistant_to_dict(msg) -> dict:
    """Convert an OpenAI ChatCompletionMessage into the dict shape required
    when feeding it back into the next ``messages=`` payload.

    Tool calls must be preserved so subsequent ``role: tool`` messages
    bind to them via ``tool_call_id``.
    """
    # Some OpenRouter providers reject null content; coerce to "".
    out: dict = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]
    return out


def _parse_arguments(raw: str | None) -> dict:
    """Parse a tool call's JSON arguments string. Empty / invalid → {}."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}
