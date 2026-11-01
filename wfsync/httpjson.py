"""Minimal JSON-over-HTTP helper on the standard library, with retry on rate limits."""

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import IO, Any

# An error code is a word; free text (`"have 12.5, need 20"`) could carry portfolio figures. A bare
# number counts only from the field the caller names as its API's error code (`errorCode` for
# Bitvavo, `code` for Bitget), never from any other.
SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,39}")
NUMERIC_CODE = re.compile(r"\d{1,5}")
MAX_RETRY_AFTER_S = 300
# How a market data endpoint reports an unknown pair or period. Any other error says nothing about
# the price.
NO_DATA = frozenset({400, 404})


class HttpError(Exception):
    """A failed request: a non-2xx status, or a status the caller assigns (Bitget's error in a 200,
    500 for a missing session cookie). Carries the status and a short reason without figures."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"HTTP {status}: {reason}")
        self.status = status
        self.reason = reason


def now() -> datetime:
    return datetime.now(UTC)


def retry_delay(headers: Message | Mapping[str, str] | None, default: float) -> float:
    """Seconds to wait after a 429: `Retry-After` (seconds or a date) if usable, capped."""
    value = (headers.get("Retry-After") or "").strip() if headers else ""
    if value.isascii() and value.isdigit():
        return min(int(value), MAX_RETRY_AFTER_S)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return default
    return min(max(int((when - now()).total_seconds()), 0), MAX_RETRY_AFTER_S)


def number_text(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def request_json(opener: urllib.request.OpenerDirector, method: str, url: str, body: object = None,
                 headers: Mapping[str, str] | None = None, timeout: float = 60, retries: int = 5, backoff: float = 15.0,
                 numeric_code: str = "errorCode", sign: Callable[[], dict[str, str]] | None = None) -> Any:
    """`sign` returns authentication headers. It runs per attempt: an exchange refuses a retry that
    carries the timestamp of the first one."""
    data = json.dumps(body, default=number_text).encode() if body is not None else None
    base = {"Accept": "application/json"}
    if data is not None:
        base["Content-Type"] = "application/json"
    base.update(headers or {})
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers=base | (sign() if sign else {}))
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw, parse_float=Decimal) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(retry_delay(e.headers, backoff))
                continue
            # The body may echo request data; keep only an error code if the API sends one.
            reason = "error"
            try:
                parsed = json.loads(e.read() or b"null")
                if isinstance(parsed, dict):
                    numeric = parsed.get(numeric_code)
                    code = parsed.get("errorCode") or parsed.get("code") or parsed.get("error")
                    if code is not None and (SAFE_CODE.fullmatch(str(code))
                                             or (code == numeric and NUMERIC_CODE.fullmatch(str(code)))):
                        reason = str(code)
            except (ValueError, OSError):
                pass
            raise HttpError(e.code, reason) from None
    raise HttpError(429, "rate limited")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib copies every header, `Authorization` included, to the target of a redirect, even
    to another host or to plain http. No API here redirects, so a 3xx ends as an HttpError."""

    def redirect_request(self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str, headers: Message,
                         newurl: str) -> None:
        return None


def default_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(NoRedirect)
