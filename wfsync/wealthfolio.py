"""Client for the Wealthfolio server API (session login, accounts, activity import, backups)."""

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from typing import Any, cast

from .httpjson import HttpError, default_opener, request_json
from .types import ImportOutcome, Json, Row

SESSION_COOKIE = "wf_session"
# Accounts created by this tool carry this provider tag; providerAccountId names the source.
PROVIDER = "wfsync"


class UnexpectedResponse(RuntimeError):
    """Wealthfolio returned a form this client does not know. Stops the source: read leniently,
    a changed form looks like an empty account, and the whole history would be imported again."""


class ImportRefused(RuntimeError):
    """Wealthfolio imported nothing from a batch."""


def fields(item: object, *names: str) -> Json:
    """`item`, which must be an object with all `names`."""
    if not isinstance(item, dict) or any(n not in item for n in names):
        raise UnexpectedResponse(type(item).__name__)
    return cast(Json, item)


def items(value: object, *names: str) -> list[Json]:
    """`value`, which must be a list of objects with all `names`."""
    if not isinstance(value, list):
        raise UnexpectedResponse(type(value).__name__)
    return [fields(v, *names) for v in value]


def refs_of(activities: Iterable[Json], prefix: str = "wfsync:") -> set[str]:
    return {a["comment"] for a in activities if (a["comment"] or "").startswith(prefix)}


class Wealthfolio:
    def __init__(self, base_url: str, origin: str, opener: urllib.request.OpenerDirector | None = None) -> None:
        self.base = base_url.rstrip("/") + "/api/v1"
        self.origin = origin
        self.opener = opener or default_opener()
        self.session: str | None = None

    # The server sets the session cookie with the Secure flag; inside the internal Docker
    # network we speak plain HTTP, so the cookie is carried by hand instead of a cookie jar.
    def _headers(self) -> dict[str, str]:
        h = {"Origin": self.origin}
        if self.session:
            h["Cookie"] = f"{SESSION_COOKIE}={self.session}"
        return h

    def login(self, password: str) -> None:
        req = urllib.request.Request(
            self.base + "/auth/login",
            data=json.dumps({"password": password}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Origin": self.origin},
        )
        try:
            with self.opener.open(req, timeout=30) as resp:
                cookies = resp.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as e:
            raise HttpError(e.code, "login failed") from None
        for c in cookies:
            name, _, rest = c.partition("=")
            if name.strip() == SESSION_COOKIE:
                self.session = rest.split(";", 1)[0]
                return
        raise HttpError(500, "no session cookie")

    def call(self, method: str, path: str, body: object = None, timeout: float = 300) -> Any:
        return request_json(self.opener, method, self.base + path, body, self._headers(), timeout=timeout, retries=1)

    def session_valid(self, session_cookie_value: str) -> bool:
        """True if the given browser session belongs to a logged-in user (used by the /sync page)."""
        req = urllib.request.Request(
            self.base + "/auth/me",
            headers={"Cookie": f"{SESSION_COOKIE}={session_cookie_value}", "Origin": self.origin},
        )
        try:
            with self.opener.open(req, timeout=10) as resp:
                # Only Wealthfolio's own JSON response counts: an SPA fallback page or a redirect after
                # an update must not turn every cookie into a valid session.
                return bool(resp.status == 200 and resp.geturl() == req.full_url
                            and (resp.headers.get("Content-Type") or "").startswith("application/json"))
        except (OSError, ValueError, http.client.HTTPException):  # any failure denies
            return False

    def ensure_account(self, source_id: str, name: str, account_type: str, currency: str,
                       create: bool = True) -> str | None:
        for acc in items(self.call("GET", "/accounts"), "id", "provider", "providerAccountId"):
            if acc["provider"] == PROVIDER and acc["providerAccountId"] == source_id:
                return cast(str, acc["id"])
        if not create:
            return None
        created = self.call(
            "POST",
            "/accounts",
            {
                "name": name,
                "accountType": account_type,
                "currency": currency,
                "isDefault": False,
                "isActive": True,
                "provider": PROVIDER,
                "providerAccountId": source_id,
                "trackingMode": "TRANSACTIONS",
            },
        )
        return cast(str, fields(created, "id")["id"])

    def activities(self, account_id: str, page_size: int = 500) -> list[Json]:
        """All activities of the account. Pages (0-based) until a page brings nothing unseen, so a
        server cap on `pageSize` cannot end it early: a missed reference means a duplicate import."""
        out: list[Json] = []
        seen: set[Any] = set()
        page = 0
        while True:
            res = fields(self.call("POST", "/activities/search",
                                   {"page": page, "pageSize": page_size, "accountIdFilter": account_id}),
                         "data", "meta")
            total = fields(res["meta"], "totalRowCount")["totalRowCount"]
            fresh = [a for a in items(res["data"], "id", "comment") if a["id"] not in seen]
            if not fresh:
                if len(out) < total:
                    raise UnexpectedResponse("search ended early")
                return out
            seen.update(a["id"] for a in fresh)
            out.extend(fresh)
            page += 1

    def search(self, query: str) -> list[Json]:
        path = "/market-data/search?query=" + urllib.parse.quote(query)
        return cast(list[Json], self.call("GET", path, timeout=60) or [])

    def quote(self, symbol: str, exchange_mic: str | None = None) -> Json:
        """Latest market quote Wealthfolio finds (`currency`, `price`); an unknown symbol returns
        neither. A failed call raises: a listing choice must not depend on a
        flaky night, so the source fails and the next run retries."""
        query = {"symbol": symbol}
        if exchange_mic:
            query["exchangeMic"] = exchange_mic
        path = "/market-data/resolve-currency?" + urllib.parse.urlencode(query)
        return cast(Json, self.call("GET", path, timeout=60) or {})

    def check(self, rows: list[Row]) -> list[Row]:
        res = self.call("POST", "/activities/import/check", {"activities": rows})
        return cast(list[Row], items(res, "isValid", "lineNumber"))

    def import_rows(self, rows: list[Row]) -> ImportOutcome:
        """Import one batch; returns how many rows Wealthfolio `imported` and `skipped`. A batch
        with a validation error imports nothing and raises, without Wealthfolio's message: it
        can quote a row."""
        res = fields(self.call("POST", "/activities/import", {"activities": rows}), "activities", "summary")
        summary = fields(res["summary"], "total", "imported", "skipped", "success")
        lines = [a["lineNumber"] for a in items(res["activities"], "lineNumber")]
        if sorted(lines, key=str) != sorted((r["lineNumber"] for r in rows), key=str):
            raise UnexpectedResponse("import response lists other rows")
        if not summary["success"]:
            raise ImportRefused("batch refused")
        if summary["total"] != len(rows) or summary["imported"] + summary["skipped"] != summary["total"]:
            raise UnexpectedResponse("import summary does not add up")
        return {"imported": summary["imported"], "skipped": summary["skipped"]}

    def backup(self) -> Json:
        return cast(Json, self.call("POST", "/utilities/database/backup"))

    def list_backups(self) -> list[Json]:
        return cast(list[Json], self.call("GET", "/utilities/database/backups") or [])

    def delete_backup(self, filename: str) -> None:
        self.call("DELETE", "/utilities/database/backups/" + urllib.parse.quote(filename, safe=""))
