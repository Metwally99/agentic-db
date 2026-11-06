# Agentic-DB

A team project exploring natural-language interaction with SQL databases through an LLM tool-calling loop.

## Highlights

- OpenRouter or Azure-backed model client and a bounded tool-calling loop.
- SQLite and MySQL adapters with schema discovery and query tools.
- Technical and read-only business modes, staged mutations, and user-controlled commit or undo.
- Flask interface for conversational queries, tables, and analytics artifacts.

## Getting started

Requires Python 3.10+, a compatible model provider, and a database you are authorized to use.

```bash
python -m venv .venv
pip install -r requirements.txt
```

Activate the virtual environment for your platform. Set `LLM_PROVIDER`, your provider credentials, and database settings as environment variables. `DB_TYPE=sqlite` uses `mock_database/mock.db` by default. Provide your own SQLite database and schema before querying. Start the app with `python main.py`, or use `python main.py --cli` for the terminal interface.

This is an educational team project. The database tools are exposed through a local tool interface. Review database permissions and model output before connecting sensitive systems.
