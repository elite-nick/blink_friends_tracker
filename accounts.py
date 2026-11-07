import os
import sqlite3
import threading
from contextlib import closing

import requests
from flask import abort, g, jsonify, request
from urllib.parse import urlsplit

DATABASE = os.path.join(os.path.dirname(__file__), "accounts.sqlite3")
BASE = "https://pin.apiblink.ru/api"
PUBLIC = "id, username, avatar, active"


def connect():
    db = sqlite3.connect(DATABASE, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def initialize():
    with closing(connect()) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY, username TEXT NOT NULL, avatar TEXT NOT NULL,
            provider TEXT NOT NULL, token TEXT NOT NULL, session TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)))""")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active ON accounts(active) WHERE active=1")


def login(provider, token):
    r = requests.post(BASE+"/login/token/v2", json={"provider_identifier":provider,"token":token},
                      headers={"User-Agent":"okhttp/5.3.2"}, timeout=(8,20))
    r.raise_for_status()
    session = r.json().get("session")
    if not session:
        raise ValueError("The server did not return a session.")
    return session


_refresh_locks = {}
_refresh_guard = threading.Lock()


def refresh_session(account=None):
    if account is None:
        account = g.account
    with _refresh_guard:
        lock = _refresh_locks.setdefault(account["id"],threading.Lock())
    with lock:
        with closing(connect()) as db:
            current = db.execute("SELECT session,provider,token FROM accounts WHERE id=?", (account["id"],)).fetchone()
        if not current:
            return False
        if current["session"] and current["session"] != account["session"]:
            account.update(dict(current))
            return True
        try:
            session = login(current["provider"], current["token"])
        except (requests.RequestException, ValueError):
            return False
        with closing(connect()) as db, db:
            changed = db.execute("UPDATE accounts SET session=? WHERE id=? AND token=? AND provider=? AND session=?",
                                 (session,account["id"],current["token"],current["provider"],current["session"]))
            if not changed.rowcount:
                return False
        account["session"] = session
        return True


def bind(app, disconnect):
    initialize()

    @app.before_request
    def current():
        if request.method == "POST" and request.headers.get("Origin") and urlsplit(request.headers["Origin"]).netloc != request.host:
            return jsonify(error="Cross-origin actions are not allowed."),403
        if request.endpoint == "static":
            return
        with closing(connect()) as db:
            row = db.execute("SELECT * FROM accounts WHERE active=1").fetchone()
        g.account = dict(row) if row else None
        if request.endpoint in ("index","list_accounts","add_account","select_account",None):
            return
        if not row:
            return jsonify(error="Select an account first."),409
        expected = request.headers.get("X-Blink-Account") or request.args.get("account")
        if expected and expected != row["id"]:
            return jsonify(error="The active account has changed.", account_changed=True),409
        actor = (request.view_args or {}).get("actor") or request.args.get("actor_id")
        if request.method == "POST":
            data = request.get_json(silent=True) if request.is_json else request.form
            actor = (data or {}).get("actor_id",actor)
        if actor is not None and str(actor) != row["id"]:
            return jsonify(error="This action belongs to another account."),409

    @app.get("/accounts")
    def list_accounts():
        with closing(connect()) as db:
            return jsonify([dict(r) for r in db.execute("SELECT "+PUBLIC+" FROM accounts ORDER BY username,id")])

    @app.post("/accounts")
    def add_account():
        data = request.get_json() or {}
        provider,token = (str(data.get(k) or "").strip() for k in ("provider_identifier","token"))
        if not provider or not token:
            return jsonify(error="Both credentials are required."),400
        try:
            session = login(provider,token)
            res = requests.post(BASE+"/map/markers",headers={"Authorization":session},timeout=(8,25))
            profile = (res.json().get("my_marker") or {}).get("account") or {} if res.ok else {}
            if not profile.get("id"):
                res = requests.get(BASE+"/account/profile",headers={"Authorization":session},timeout=(8,20))
                res.raise_for_status()
                data = res.json()
                profile = data.get("account") or data
            if not profile.get("id"):
                return jsonify(error="Could not identify the owner of these credentials."),422
        except (requests.RequestException,ValueError):
            return jsonify(error="Could not verify the credentials with Blink."),422
        with closing(connect()) as db, db:
            db.execute("""INSERT INTO accounts(id,username,avatar,provider,token,session) VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET username=excluded.username,avatar=excluded.avatar,
                provider=excluded.provider,token=excluded.token,session=excluded.session""",
                (str(profile["id"]),profile.get("username") or profile.get("name") or str(profile["id"]),
                 profile.get("avatar_thumbnail") or profile.get("avatar") or "",provider,token,session))
        return jsonify(id=str(profile["id"]))

    @app.post("/accounts/<int:uid>/select")
    def select_account(uid):
        uid = str(uid)
        with closing(connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM accounts WHERE id=?", (uid,)).fetchone():
                abort(404)
            db.execute("UPDATE accounts SET active=0 WHERE active=1 AND id<>?",(uid,))
            db.execute("UPDATE accounts SET active=1 WHERE id=?",(uid,))
        disconnect(uid)
        return jsonify(active=uid)
