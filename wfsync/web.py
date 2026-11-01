"""The /sync page behind a reverse proxy, on the same host name as Wealthfolio.

Access requires a valid Wealthfolio browser session: the `wf_session` cookie of the request is
checked against Wealthfolio's `/auth/me`, so there is one login for both. State-changing requests
additionally require `Origin` to equal the public origin (CSRF guard on top of SameSite).
"""

import hashlib
import html
import json
import logging
import re
import threading
import time
from collections.abc import Mapping
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__, sync
from .types import Status, UploadSource
from .wealthfolio import SESSION_COOKIE, Wealthfolio

log = logging.getLogger("wfsync.web")
SESSION_CACHE_S = 60
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
# A Monefit statement has one short line per transaction, Go & Grow's and Debitum's .xlsx are
# compressed: 2 MiB hold decades of daily earnings.
MAX_UPLOAD = 2 * 1024 * 1024
UPLOAD_FIELD = "statement"

# Rows of the nightly table; upload sources show under their own card with `ACCOUNT_NAME`.
LABELS = {"trading212": "Trading 212", "bitvavo": "Bitvavo", "bitget": "Bitget", "wealthfolio": "Wealthfolio"}


class SessionChecker:
    def __init__(self, wf: Wealthfolio) -> None:
        self.wf = wf
        self.cache: dict[str, float] = {}
        self.lock = threading.Lock()

    def valid(self, cookie_value: str | None) -> bool:
        if not cookie_value:
            return False
        key = hashlib.sha256(cookie_value.encode()).hexdigest()
        with self.lock:
            hit = self.cache.get(key)
            if hit and hit > time.monotonic():
                return True
        ok = self.wf.session_valid(cookie_value)
        if ok:
            with self.lock:
                self.cache = {k: v for k, v in self.cache.items() if v > time.monotonic()}
                self.cache[key] = time.monotonic() + SESSION_CACHE_S
        return ok


SESSION_VALUE = re.compile(r"[A-Za-z0-9._~+/=-]{1,512}")


def session_cookie(header: str | None) -> str | None:
    """Value of `wf_session` in a Cookie header. Parsed by hand: `SimpleCookie` stops at the first
    cookie it cannot parse (a JSON value or a space from another app on the domain) and loses the
    ones after it. Only token characters pass on to Wealthfolio's /auth/me."""
    for part in (header or "").split(";"):
        name, sep, value = part.partition("=")
        value = value.strip().strip('"')
        if sep and name.strip() == SESSION_COOKIE and SESSION_VALUE.fullmatch(value):
            return value
    return None


def local_time(stamp: str | None) -> str:
    """`2020-06-15T01:30:00+00:00` -> `2020-06-15 03:30` in the container's time zone."""
    try:
        return datetime.fromisoformat(stamp or "").astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "-"


def upload_file(content_type: str | None, body: bytes, field: str = UPLOAD_FIELD) -> bytes | None:
    """Bytes of the form field `field` in a multipart/form-data body, or None."""
    if not content_type or not content_type.lower().startswith("multipart/form-data"):
        return None
    head = b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("latin-1", "replace") + b"\r\n\r\n"
    msg = BytesParser(EmailMessage, policy=policy.HTTP).parsebytes(head + body)
    if not msg.is_multipart():
        return None
    for part in msg.iter_parts():
        if part.get_param("name", header="content-disposition") == field:
            payload = part.get_payload(decode=True)
            return payload if isinstance(payload, bytes) else None
    return None


def details(res: Mapping[str, Any]) -> list[str]:
    detail: list[str] = []
    if res.get("note"):
        detail.append(res["note"])
    if res.get("new") is not None:
        detail.append(f"{res['new']} new transactions" + (" (dry run)" if res.get("dry_run") else ""))
    if res.get("invalid"):
        detail.append(f"{res['invalid']} rejected (fields: {', '.join(res.get('invalid_fields') or []) or '-'})")
    if res.get("duplicates"):
        detail.append(f"{res['duplicates']} not imported: Wealthfolio reports a duplicate")
    if res.get("held_back"):
        detail.append(f"{res['held_back']} held back (other leg not imported)")
    if res.get("not_imported"):
        detail.append(f"{res['not_imported']} skipped by Wealthfolio on import")
    if res.get("pending"):
        detail.append(f"{res['pending']} waiting for a price, retried on the next run")
    skipped = res.get("skipped")
    for reason, n in (skipped.items() if isinstance(skipped, dict) else ()):
        detail.append(f"{n} skipped: {reason}")
    if isinstance(res.get("manual_pricing"), list) and res["manual_pricing"]:
        detail.append("no market price, valued manually: " + ", ".join(map(str, res["manual_pricing"])))
    if res.get("error"):
        detail.append(res["error"])
    return detail


def upload_card(module: UploadSource, status: Status, running: bool) -> str:
    """Upload form for the statement of `module` and the result of its last upload."""
    e = html.escape
    uploads = status.get("uploads")
    last = uploads.get(module.SOURCE_ID) if isinstance(uploads, dict) else None
    if isinstance(last, dict) and last:
        st = str(last.get("status", "?"))
        period = f" ({e(last['statement'])})" if last.get("statement") else ""
        detail = "".join("<br>" + e(d) for d in details(last))
        state = (f"<p>Last statement{period}, imported {e(local_time(last.get('at')))}: "
                 f"<span class='st {e(st)}'>{e(st)}</span>{detail}</p>")
    else:
        state = "<p class='muted'>No statement imported yet.</p>"
    form = ("<button disabled>Running ...</button>" if running else
            f"<form method='post' action='/sync/{e(module.SOURCE_ID)}' enctype='multipart/form-data'>"
            f"<input type='file' name='{UPLOAD_FIELD}' accept='{e(module.UPLOAD_ACCEPT)}' required> "
            "<button type='submit'>Import statement</button></form>")
    return (f"<h2>{e(module.ACCOUNT_NAME)}</h2><p class='muted'>{e(module.UPLOAD_HELP)}; "
            "transactions already imported are skipped.</p>" + form + state)


def visible(status: Status) -> dict[str, Any]:
    """`status` without results of sources deselected since the run or upload that wrote them."""
    shown = {s[0].SOURCE_ID for s in sync.configured_sources()} | {"wealthfolio"}
    picked = dict(status)
    for key, keep in (("results", shown), ("uploads", set(sync.upload_sources()))):
        if isinstance(status.get(key), dict):
            picked[key] = {source: res for source, res in status[key].items() if source in keep}
    return picked


def render(status: Status) -> str:
    e = html.escape
    status = visible(status)
    running = bool(status.get("running"))
    api = sync.configured_sources()
    upload = sync.upload_sources()
    rows = []
    results = status.get("results")
    for source, res in (results.items() if isinstance(results, dict) else ()):
        if not isinstance(res, dict):
            res = {"status": "?"}
        st = str(res.get("status", "?"))
        detail = details(res)
        rows.append(f"<tr><td>{e(LABELS.get(source, str(source)))}</td><td class='st {e(st)}'>{e(st)}</td>"
                    f"<td>{'<br>'.join(e(d) for d in detail) or '-'}</td></tr>")
    table = "".join(rows) or "<tr><td colspan='3'>No run yet.</td></tr>"
    refresh = "<meta http-equiv='refresh' content='5'>" if running else ""
    button = ("<button disabled>Running ...</button>" if running else
              "<form method='post' action='/sync/run'><button type='submit'>Sync now</button></form>")
    uploads = "".join(f"<div class='card'>{upload_card(m, status, running)}</div>" for m in upload.values())
    if api:
        names = ", ".join(s[0].ACCOUNT_NAME for s in api)
        intro = (f"Fetches transactions from {e(names)} read-only and imports new ones into Wealthfolio. "
                 f"Runs automatically every night at {sync.NIGHTLY_AT[0]:02d}:{sync.NIGHTLY_AT[1]:02d}.")
        trigger = f" ({e(status['trigger'])})" if status.get("trigger") else ""
        run_cards = f"""<div class="card">{button}</div>
<div class="card"><table>{table}</table>
<p class="muted">Last start: {e(local_time(status.get('started')))}{trigger}
| End: {e(local_time(status.get('finished')))}
| Last success: {e(local_time(status.get('last_success')))}</p></div>"""
    elif upload:
        intro = "Imports uploaded account statements into Wealthfolio. No selected source is fetched automatically."
        run_cards = ""
    else:
        intro = "Imports transactions into Wealthfolio."
        run_cards = (f"<div class='card'><p>No source is selected. Set <code>{sync.SOURCES_ENV}</code> to a "
                     f"comma-separated list of {e(', '.join(sync.SOURCE_IDS))} and restart the container.</p></div>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{refresh}<title>Wealthfolio Sync</title>
<style>
:root{{--bg:#f6f7f9;--fg:#1d2330;--muted:#5b6475;--card:#fff;--line:#e2e5eb;--ok:#1a7f45;--warn:#a45d00;
--err:#b42318;--btn:#1d2330;--btnfg:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12151b;--fg:#e8eaef;--muted:#9aa3b2;--card:#1b1f27;--line:#2b313c;
--ok:#4cc38a;--warn:#f0a43a;--err:#ff6b5e;--btn:#e8eaef;--btnfg:#12151b}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:24px 16px}}h1{{font-size:22px;margin:0 0 4px}}
.muted{{color:var(--muted)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:16px 0}}
table{{width:100%;border-collapse:collapse}}td{{padding:8px 6px;border-top:1px solid var(--line);vertical-align:top}}
.st.ok{{color:var(--ok)}}.st.partial{{color:var(--warn)}}.st.error{{color:var(--err)}}
button{{background:var(--btn);color:var(--btnfg);border:0;border-radius:8px;padding:10px 16px;font-size:15px;
cursor:pointer}}
button[disabled]{{opacity:.6;cursor:default}}a{{color:inherit}}h2{{font-size:17px;margin:0 0 4px}}
input[type=file]{{max-width:100%;margin:0 8px 8px 0;font:inherit}}
</style></head><body><main>
<h1>Wealthfolio Sync</h1>
<p class="muted">{intro}</p>
{run_cards}
{uploads}
<p><a href="/">Back to Wealthfolio</a></p>
<p class="muted">wealthfolio-sync {e(__version__)}</p>
</main></body></html>"""


def make_handler(checker: SessionChecker, origin: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "wfsync"
        timeout = 30  # a client that stalls mid-request must not hold its thread forever

        def log_message(self, format: str, *args: Any) -> None:  # access log without query strings or cookies
            # `send_error` logs before a broken request line has set `command` and `path`.
            path = (getattr(self, "path", "") or "").split("?", 1)[0]
            command = getattr(self, "command", None) or "-"
            log.info("%s %s -> %s", command, path or "-", args[1] if len(args) > 1 else "")

        def log_error(self, format: str, *args: Any) -> None:
            # The standard message quotes the raw request line, query string included; the status
            # code still reaches the access log through `send_response`.
            pass

        def _session(self) -> str | None:
            return session_cookie(self.headers.get("Cookie"))

        def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
                  extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._send(200, b"ok")
            if path not in ("/sync", "/sync/", "/sync/status.json"):
                return self._send(404, b"not found")
            if not checker.valid(self._session()):
                return self._send(303, extra={"Location": "/"})
            status = sync.load_status()
            # A run killed mid-way leaves `running` set; only a held lock means a run is going on.
            status["running"] = bool(status.get("running")) and sync.is_running()
            if path.endswith(".json"):
                return self._send(200, json.dumps(visible(status)).encode(), "application/json")
            return self._send(200, render(status).encode(), "text/html; charset=utf-8")

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if self.headers.get("Origin") != origin:
                return self._send(403, b"origin")
            if not checker.valid(self._session()):
                return self._send(403, b"session")
            if path != "/sync/run":
                source_id = path.removeprefix("/sync/")
                if not path.startswith("/sync/") or source_id not in sync.upload_sources():
                    return self._send(404, b"not found")
                return self._upload(source_id)
            threading.Thread(target=sync.run, kwargs={"trigger": "button"}, daemon=True).start()
            time.sleep(0.5)  # let the run mark itself as running before the page reloads
            return self._send(303, extra={"Location": "/sync"})

        def _upload(self, source_id: str) -> None:
            # Imported in this request, so the page after the redirect already shows the result.
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if length < 0:
                self.close_connection = True
                return self._send(411, b"length required")
            if length > MAX_UPLOAD:
                self.close_connection = True  # the body stays unread, so the connection must not be reused
                return self._send(413, b"file too large")
            raw = upload_file(self.headers.get("Content-Type"), self.rfile.read(length))
            if not raw:
                return self._send(400, b"no file chosen")
            if sync.import_statement(source_id, raw) is None:
                return self._send(409, b"a sync is running, please upload again in a moment")
            return self._send(303, extra={"Location": "/sync"})

    return Handler


def serve(port: int) -> None:
    wf = sync.wealthfolio()
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(SessionChecker(wf), wf.origin))  # noqa: S104
    log.info("listening on %s", port)
    httpd.serve_forever()
