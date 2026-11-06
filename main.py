"""Agentic-DB entry point for the web GUI or CLI REPL.

Usage:
    python main.py          # Web GUI mode (default)
    python main.py --cli    # CLI REPL mode
"""

import json
import sys

import config
from agent import AgentLoop, LLMClient
from agent.modes import BUSINESS_MODE, TECHNICAL_MODE
from mcp_server import DatabaseServer


def _fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def _validate_environment() -> None:
    try:
        config.validate_llm_config()
    except ValueError as exc:
        _fail(str(exc))
    try:
        config.validate_database_config()
    except ValueError as exc:
        _fail(str(exc))


def _build_llm() -> LLMClient:
    if config.LLM_PROVIDER == "azure":
        return LLMClient(
            provider="azure",
            api_key=config.AZURE_OPENAI_API_KEY,
            azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
            azure_deployment=config.AZURE_OPENAI_DEPLOYMENT,
        )
    return LLMClient(
        provider="openrouter",
        api_key=config.OPENROUTER_API_KEY,
        model=config.OPENROUTER_MODEL,
        base_url=config.OPENROUTER_BASE_URL,
    )


def _llm_label() -> str:
    if config.LLM_PROVIDER == "azure":
        return f"azure deployment {config.AZURE_OPENAI_DEPLOYMENT}"
    return config.OPENROUTER_MODEL


def _build_database(mode=None) -> DatabaseServer:
    try:
        return DatabaseServer(config.get_database_settings(), mode=mode)
    except Exception as exc:
        _fail(f"Database connection failed: {type(exc).__name__}: {exc}")
        raise


def gui_main() -> None:
    """Launch the Flask web interface."""
    _validate_environment()

    from gui import ChatWindow

    llm = _build_llm()

    db_tech = _build_database(mode=TECHNICAL_MODE)
    db_biz = _build_database(mode=BUSINESS_MODE)
    agent_tech = AgentLoop(
        llm, db_tech, mode=TECHNICAL_MODE, max_iterations=config.MAX_AGENT_ITERATIONS
    )
    agent_biz = AgentLoop(
        llm, db_biz, mode=BUSINESS_MODE, max_iterations=config.MAX_AGENT_ITERATIONS
    )

    agents = {"technical": agent_tech, "business": agent_biz}
    dbs = {"technical": db_tech, "business": db_biz}

    app = ChatWindow(agents, dbs)
    app.mainloop()


HELP_TEXT = """\
Commands:
  /help           Show this help.
  /pending        List pending (uncommitted) changes.
  /commit         Permanently save all pending changes.
  /undo [N]       Roll back the last N pending changes (default 1).
  /last           Show columns + first 50 rows of the most recent query.
  /reset          Clear the agent's conversation history.
  /exit, /quit    Exit the REPL.

Anything else is sent to the agent as a natural-language request.
"""


def _print_last_result(db: DatabaseServer, max_rows: int = 50) -> None:
    result = db.get_last_result()
    if result is None:
        print("(no query has been run yet)")
        return
    cols = result["columns"]
    rows = result["rows"]
    print(f"SQL: {result['sql']}")
    print(f"Rows: {result['row_count']}")
    print(" | ".join(cols))
    print("-+-".join("-" * len(c) for c in cols))
    for row in rows[:max_rows]:
        print(" | ".join(str(v) if v is not None else "NULL" for v in row))
    if len(rows) > max_rows:
        print(f"... ({len(rows) - max_rows} more rows not shown)")


def _handle_slash(command: str, db: DatabaseServer, agent: AgentLoop) -> bool:
    """Return False to signal exit; True to continue the REPL."""
    parts = command.strip().split()
    name = parts[0].lower()
    args = parts[1:]

    if name in ("/exit", "/quit"):
        return False

    if name == "/help":
        print(HELP_TEXT)
    elif name == "/pending":
        print(_pretty(db.execute_tool("get_pending_changes", {})))
    elif name == "/commit":
        print(_pretty(db.execute_tool("commit_changes", {})))
    elif name == "/undo":
        steps = int(args[0]) if args else 1
        print(_pretty(db.execute_tool("undo", {"steps": steps})))
    elif name == "/last":
        _print_last_result(db)
    elif name == "/reset":
        agent.reset()
        print("(agent history cleared)")
    else:
        print(f"Unknown command: {name}. Type /help for commands.")
    return True


def _pretty(raw_json: str) -> str:
    """Pretty-print a JSON string returned by execute_tool."""
    try:
        return json.dumps(json.loads(raw_json), indent=2)
    except json.JSONDecodeError:
        return raw_json


def cli_main() -> None:
    """Launch the developer CLI REPL."""
    _validate_environment()

    db = _build_database()
    llm = _build_llm()
    agent = AgentLoop(llm, db, max_iterations=config.MAX_AGENT_ITERATIONS)

    print(f"Agentic-DB REPL - llm: {_llm_label()}")
    print(f"Database:   {db.db_path}")
    print("Type /help for commands, /exit to quit.\n")

    try:
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line.startswith("/"):
                if not _handle_slash(line, db, agent):
                    break
                continue
            try:
                reply = agent.send(line)
            except Exception as exc:  # noqa: BLE001 - keep REPL alive on any error
                print(f"[agent error] {type(exc).__name__}: {exc}")
                continue
            print(reply)
    finally:
        db.close()


if __name__ == "__main__":
    if "--cli" in sys.argv:
        cli_main()
    else:
        gui_main()
