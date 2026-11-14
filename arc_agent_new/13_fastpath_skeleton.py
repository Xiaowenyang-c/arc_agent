"""Prebuilt skeleton for the fast path's template+delta generation.

The model writes only the DELTA (tables, seeds, route handlers); this module provides the
server, sessions, database helpers, HTML components and route dispatch, so the generated
code stays small and the boilerplate is known-good instead of regenerated on every run.

Delta contract (the model fills these module-level names):
    TABLES: list of (name, "CREATE TABLE IF NOT EXISTS ...") tuples
    SEEDS:  list of SQL strings, executed once at startup (must be idempotent)
    ROUTES: dict path -> (method, handler); paths may contain "<int:id>" segments
    NAV:    list of (label, path) for the global navigation
    handlers: functions taking (self, conn, user, params) and calling self.send_html(...)
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")
TABLES: list = []
SEEDS: list = []
ROUTES: dict = {}
NAV: list = []
LOGIN = {
    "table": "users",
    "login_column": "email",
    "login_field_label": "Email",
    "password_field_label": "Password",
    "shown_column": "display_name",
    "landing": "/",
    "region_name": "Log in",
}

SESSION_TTL = 30 * 24 * 3600


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def seq_next(conn, name: str = "app") -> int:
    """Strictly increasing per-name sequence (concurrency-safe via the SQLite connection)."""
    conn.execute("CREATE TABLE IF NOT EXISTS _seq(name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)")
    conn.execute("INSERT INTO _seq(name, value) VALUES(?, 1) "
                 "ON CONFLICT(name) DO UPDATE SET value = value + 1", (name,))
    row = conn.execute("SELECT value FROM _seq WHERE name = ?", (name,)).fetchone()
    return int(row["value"])


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 60_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 60_000).hex(), digest)


# ---------------------------------------------------------------- HTML components

def region(name: str, inner: str, heading: bool = True, tag: str = "section") -> str:
    head = f"<h2>{esc(name)}</h2>" if heading else ""
    return f'<{tag} role="region" aria-label="{esc(name)}">{head}{inner}</{tag}>'


def status(name: str, text: str, extra: str = "") -> str:
    return f'<div role="status" aria-label="{esc(name)}">{esc(text)}{extra}</div>'


def dialog(name: str, inner: str) -> str:
    return f'<div role="dialog" aria-label="{esc(name)}"><h2>{esc(name)}</h2>{inner}</div>'


def button(text: str, action: str, method: str = "post", hidden: dict | None = None,
           form_attrs: str = "", attrs: str = "", cls: str = "") -> str:
    hidden_html = "".join(f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">' for k, v in (hidden or {}).items())
    return (f'<form method="{method}" action="{esc(action)}" {form_attrs} class="inline">'
            f"{hidden_html}<button type=\"submit\" {attrs} class=\"{cls}\">{esc(text)}</button></form>")


def link(text: str, href: str, attrs: str = "", cls: str = "") -> str:
    return f'<a href="{esc(href)}" {attrs} class="{cls}">{esc(text)}</a>'


def field(name: str, kind: str = "text", value: str = "", placeholder: str | None = None,
          extra: str = "") -> str:
    placeholder = name if placeholder is None else placeholder
    if kind == "textarea":
        return (f'<label for="f_{esc(name)}">{esc(name)}</label>'
                f'<textarea id="f_{esc(name)}" name="{esc(name)}" placeholder="{esc(placeholder)}" {extra}>'
                f"{esc(value)}</textarea>")
    if kind == "select":
        return (f'<label for="f_{esc(name)}">{esc(name)}</label>'
                f'<select id="f_{esc(name)}" name="{esc(name)}" onchange="this.form.requestSubmit()" {extra}>'
                f"{value}</select>")
    return (f'<label for="f_{esc(name)}">{esc(name)}</label>'
            f'<input id="f_{esc(name)}" type="{kind}" name="{esc(name)}" value="{esc(value)}" '
            f'placeholder="{esc(placeholder)}" {extra}>')


def layout(title: str, nav: list, active: str, content: str, user=None, banner: str = "") -> str:
    user_area = ""
    if user is not None and isinstance(user, sqlite3.Row):
        shown = user["display_name"] if "display_name" in user.keys() else user["username"]
        user_area = button(shown, "/logout") + button("Log out", "/logout")
    else:
        user_area = '<a href="/login">Log in</a> <a href="/signup">Sign up</a>'
    nav_html = " ".join(
        link(label, path, attrs='aria-current="true"' if label == active else "")
        for label, path in nav)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;color:#1a1a1a}}header, nav{{display:flex;gap:12px;align-items:center;flex-wrap:wrap}}
header{{padding:8px 16px;background:#f4f4f4}}nav a{{text-decoration:none;color:#0a58ca}}
nav a[aria-current="true"]{{font-weight:700}}main{{padding:16px;max-width:1100px;margin:0 auto}}
.inline{{display:inline}}.status{{margin:8px 0;padding:8px;border:1px solid #ddd;border-radius:6px;background:#fafafa}}
button{{cursor:pointer}}section{{margin:14px 0}}ul{{list-style:none;padding-left:0}}li{{margin:4px 0}}
</style></head><body>
<header role="banner">{banner}<nav role="navigation" aria-label="Main navigation">{nav_html}</nav>{user_area}</header>
<main>{content}</main>
</body></html>"""


# ---------------------------------------------------------------- request handling

def _parse_path(path: str) -> tuple[str, dict]:
    for pattern in ROUTES:
        names = re.findall(r"<int:(\w+)>", pattern)
        regex = re.escape(pattern)
        for name in names:
            regex = regex.replace(re.escape(f"<int:{name}>"), r"(\d+)")
        match = re.fullmatch(regex, path)
        if match:
            return pattern, dict(zip(names, [int(v) for v in match.groups()]))
    return "", {}


class Handler(BaseHTTPRequestHandler):
    server_version = "fastpath/1.0"

    def log_message(self, *args):
        pass  # keep the server log quiet

    def _read_form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}

    def _read_query(self) -> dict:
        parsed = urllib.parse.urlparse(self.path)
        return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    def session_token(self) -> str | None:
        cookies = self.headers.get("Cookie", "")
        for part in cookies.split(";"):
            key, _, value = part.strip().partition("=")
            if key == "session":
                return value
        return None

    def current_user(self, conn):
        token = self.session_token()
        if not token:
            return None
        row = conn.execute("SELECT * FROM users WHERE session_token = ?", (token,)).fetchone()
        return row

    def send_html(self, body: str, code: int = 200, cookie: str | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, path: str, code: int = 303, cookie: str | None = None) -> None:
        self.send_response(code)
        self.send_header("Location", path)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        pattern, params = _parse_path(parsed.path)
        conn = db()
        try:
            if not pattern:
                self.send_html("<h1>Not found</h1><p>The page you asked for does not exist.</p>", 404)
                return
            handler = ROUTES[pattern].get(method)
            if isinstance(handler, str):  # delta registers by name so definition order never matters
                handler = globals().get(handler)
            if handler is None:
                self.send_html("<h1>Not found</h1>", 404)
                return
            user = self.current_user(conn)
            handler(self, conn, user, params)
            conn.commit()
        except Exception as error:  # never crash the server on a bad route
            try:
                conn.rollback()
            except Exception:
                pass
            self.send_html(f"<h1>Server error</h1><pre>{esc(repr(error))}</pre>", 500)
        finally:
            conn.close()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


# ---------------------------------------------------------------- built-in auth

def _login_page(self, conn, user, params):
    if user is not None:
        return self.redirect(LOGIN["landing"])
    inner = (f'<form method="post" action="/login">{field(LOGIN["login_field_label"])}'
             f'{field(LOGIN["password_field_label"], kind="password")}'
             f'<label><input type="checkbox" name="remember" value="1">Remember me</label>'
             f'<button type="submit">Log in</button></form>')
    self.send_html(layout(LOGIN["region_name"], NAV, "", region(LOGIN["region_name"], inner), None))


def _login_post(self, conn, user, params):
    form = self._read_form()
    login_value = form.get(LOGIN["login_field_label"], "")
    password = form.get(LOGIN["password_field_label"], "")
    table = LOGIN["table"]
    column = LOGIN["login_column"]
    row = conn.execute(f'SELECT * FROM {table} WHERE {column} = ?', (login_value,)).fetchone()
    if row is not None and row["password_hash"] and verify_password(password, row["password_hash"]):
        token = secrets.token_hex(16)
        conn.execute(f'UPDATE {table} SET session_token = ? WHERE id = ?', (token, row["id"]))
        self.redirect(LOGIN["landing"], cookie=f"session={token}; Path=/; HttpOnly; Max-Age={SESSION_TTL}")
        return
    inner = (f'<p>Wrong credentials.</p><form method="post" action="/login">'
             f'{field(LOGIN["login_field_label"])}{field(LOGIN["password_field_label"], kind="password")}'
             f'<button type="submit">Log in</button></form>')
    self.send_html(layout(LOGIN["region_name"], NAV, "", region(LOGIN["region_name"], inner), None))


def _logout(self, conn, user, params):
    token = self.session_token()
    if token:
        table = LOGIN["table"]
        conn.execute(f'UPDATE {table} SET session_token = NULL WHERE session_token = ?', (token,))
    self.redirect("/")


ROUTES.update({"/login": {"GET": _login_page, "POST": _login_post}, "/logout": {"POST": _logout}})


def serve() -> None:
    conn = db()
    try:
        for _name, ddl in TABLES:
            conn.execute(ddl)
        for statement in SEEDS:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "3000"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()
