"""Flask web layer.

Wraps two ``AgentLoop`` / ``DatabaseServer`` pairs (one per mode) and gates
access on a username/password login that maps to one of the two modes.
"""

from __future__ import annotations

import json
import logging
import os

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import auth

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)


class ChatWindow:
    """Flask web server wrapper for Agentic-DB.

    Supports two agent/database pairs:
    - **technical** (full read-write access, pending-changes sidebar)
    - **business**  (read-only analytics, no sidebar)

    Access is gated by ``gui.auth``: callers must POST credentials to
    ``/login``; the resulting session binds them to a single mode, and
    routes for the other mode return 403.
    """

    def __init__(self, agents: dict, dbs: dict):
        self._agents = agents
        self._dbs = dbs
        self._last_sent_query_ids = {"technical": None, "business": None}

        self.app = Flask(__name__, template_folder="templates", static_folder="static")
        self.app.secret_key = os.environ.get(
            "FLASK_SECRET_KEY",
            "dev-only-secret-do-not-use-in-production",
        )
        self.app.config["SESSION_COOKIE_HTTPONLY"] = True
        self.app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

        auth.ensure_user_store()
        self._setup_routes()

    # ------------------------------------------------------------------ #
    @property
    def _db_path(self) -> str:
        db = self._dbs.get("technical") or next(iter(self._dbs.values()))
        return db.db_path

    # ------------------------------------------------------------------ #
    def _setup_routes(self):
        # ===== Login / logout =====
        @self.app.route("/", methods=["GET", "POST"])
        def login():
            # GET: render the form (redirect away if already signed in)
            if request.method == "GET":
                if session.get("username") and session.get("mode"):
                    return redirect(self._home_for(session["mode"]))
                return render_template("login.html", db_name=self._db_path)

            # POST: verify credentials
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            mode = auth.verify_credentials(username, password)
            if mode is None:
                return render_template(
                    "login.html",
                    db_name=self._db_path,
                    error="Invalid username or password.",
                    username=username,
                ), 401
            session.clear()
            session["username"] = username
            session["mode"] = mode
            return redirect(self._home_for(mode))

        @self.app.route("/logout", methods=["POST", "GET"])
        def logout():
            session.clear()
            return redirect(url_for("login"))

        # ===== Chat pages (mode-gated) =====
        @self.app.route("/technical")
        @auth.login_required
        @auth.mode_required("technical")
        def technical():
            return render_template(
                "index.html",
                db_name=self._db_path,
                username=session.get("username"),
            )

        @self.app.route("/manager")
        @auth.login_required
        @auth.mode_required("business")
        def manager():
            return render_template(
                "manager.html",
                db_name=self._db_path,
                username=session.get("username"),
            )

        # ===== Technical API (mode-gated) =====
        @self.app.route("/api/chat", methods=["POST"])
        @auth.login_required
        @auth.mode_required("technical")
        def chat():
            return self._handle_chat("technical")

        @self.app.route("/api/pending", methods=["GET"])
        @auth.login_required
        @auth.mode_required("technical")
        def get_pending():
            raw = self._dbs["technical"].execute_tool("get_pending_changes", {})
            return jsonify(json.loads(raw))

        @self.app.route("/api/commit", methods=["POST"])
        @auth.login_required
        @auth.mode_required("technical")
        def commit():
            # Direct method call bypasses the agent-facing mode gate. The
            # agent is no longer allowed to commit; only this route (driven
            # by the sidebar's "Commit All" button) can.
            db = self._dbs["technical"]
            result = json.loads(db.commit_changes())
            if result.get("error") == "connection_lost":
                # Server killed the transaction. Tell the agent so its
                # history stops describing those mutations as "pending".
                self._agents["technical"].notify_undone(result.get("lost", []))
                return jsonify(result), 409
            if result.get("success"):
                count = result.get("committed_count", 0)
                self._agents["technical"].notify_committed(count)
            return jsonify(result)

        @self.app.route("/api/undo", methods=["POST"])
        @auth.login_required
        @auth.mode_required("technical")
        def undo():
            # Same as /api/commit: sidebar-only path, not reachable by the agent.
            db = self._dbs["technical"]
            result = json.loads(db.undo(steps=1))
            if result.get("error") == "connection_lost":
                self._agents["technical"].notify_undone(result.get("lost", []))
                return jsonify(result), 409
            if result.get("success"):
                undone = result.get("undone", [])
                self._agents["technical"].notify_undone(
                    [item.get("description", "(unknown)") for item in undone]
                )
            return jsonify(result)

        @self.app.route("/api/reset", methods=["POST"])
        @auth.login_required
        @auth.mode_required("technical")
        def reset():
            self._agents["technical"].reset()
            self._last_sent_query_ids["technical"] = None
            return jsonify({"success": True})

        # ===== Manager API (business mode) =====
        @self.app.route("/api/manager/chat", methods=["POST"])
        @auth.login_required
        @auth.mode_required("business")
        def manager_chat():
            return self._handle_chat("business")

        @self.app.route("/api/manager/reset", methods=["POST"])
        @auth.login_required
        @auth.mode_required("business")
        def manager_reset():
            self._agents["business"].reset()
            self._last_sent_query_ids["business"] = None
            return jsonify({"success": True})

    # ------------------------------------------------------------------ #
    @staticmethod
    def _home_for(mode: str) -> str:
        return "/technical" if mode == "technical" else "/manager"

    # ------------------------------------------------------------------ #
    def _handle_chat(self, mode: str):
        """Process one chat turn and forward the agent's artifact bag."""
        data = request.get_json() or {}
        user_msg = data.get("message", "").strip()
        if not user_msg:
            return jsonify({"error": "Empty message"}), 400

        agent = self._agents[mode]
        db = self._dbs[mode]

        try:
            previous_result = db.get_last_result()
            previous_query_id = (
                previous_result.get("query_id") if previous_result else None
            )

            reply = agent.send(user_msg)

            # Forward whatever the loop captured this turn (chart, explain,
            # dry_run, analytics). Empty bag is fine - keys stay None.
            artifacts = agent.pop_last_turn_artifacts()

            result = db.get_last_result()
            result_query_id = result.get("query_id") if result else None
            has_new_result = (
                result
                and result_query_id is not None
                and result_query_id != previous_query_id
                and result_query_id != self._last_sent_query_ids[mode]
            )
            if has_new_result:
                self._last_sent_query_ids[mode] = result_query_id

            return jsonify({
                "reply": reply,
                "table": None,
                "mode": mode,
                "artifacts": artifacts,
            })
        except Exception as exc:  # noqa: BLE001 - keep API alive
            return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    # ------------------------------------------------------------------ #
    def mainloop(self):
        print("Starting Flask Web GUI on http://127.0.0.1:5000")
        self.app.run(host="127.0.0.1", port=5000, debug=False)
