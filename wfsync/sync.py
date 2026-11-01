"""One sync run: fetch every source, map, let Wealthfolio validate, import what is new.

Logs carry only states and error classes, never amounts, quantities or counts: portfolio
figures stay inside Wealthfolio. Counts live only in the status file for the /sync page.
"""

import fcntl
import json
import logging
import os
import time
import traceback
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import IO, Any, cast

from . import config, secrets
from .config import ConfigError
from .httpjson import HttpError
from .rows import GROUP_KEY
from .sources import bitget, bitvavo, debitum, goandgrow, monefit, statement, trading212
from .types import Client, Row, Source, SourceResult, StatementClient, UploadSource
from .wealthfolio import Wealthfolio, refs_of

log = logging.getLogger("wfsync")
STATE_DIR = os.environ.get("WFSYNC_STATE_DIR", "/state")
BATCH = 200
LOCK_ATTEMPTS, LOCK_RETRY_S = 10, 0.1


NIGHTLY_AT = (3, 30)  # local time of the container (TZ); the nightly loop and the page read it here
DEFAULT_WEALTHFOLIO_URL = "http://wealthfolio:8088"
TRANSPORT_FUNCTIONS = {"get", "pages", "call", "fields", "items"}
# Sources without an API: their statement is uploaded on /sync (`import_statement`), never fetched at night.
UPLOAD_SOURCES: dict[str, UploadSource] = {m.SOURCE_ID: m for m in (monefit, goandgrow, debitum)}
ApiSource = tuple[Source, str, Callable[[], Client]]
API_SOURCES: tuple[ApiSource, ...] = (
    (trading212, "SECURITIES",
     lambda: trading212.Trading212(secrets.read_secret("t212_key"), secrets.read_secret("t212_secret"))),
    (bitvavo, "CRYPTOCURRENCY",
     lambda: bitvavo.Bitvavo(secrets.read_secret("bitvavo_key"), secrets.read_secret("bitvavo_secret"))),
    (bitget, "CRYPTOCURRENCY",
     lambda: bitget.Bitget(secrets.read_secret("bitget_key"), secrets.read_secret("bitget_secret"),
                           secrets.read_secret("bitget_passphrase"))),
)
SOURCES_ENV = "WFSYNC_SOURCES"
SOURCE_IDS = tuple(s[0].SOURCE_ID for s in API_SOURCES) + tuple(UPLOAD_SOURCES)
NO_SOURCES = f"no source selected: set {SOURCES_ENV} to a comma-separated list of {', '.join(SOURCE_IDS)}"


def selected_sources() -> set[str]:
    """The SOURCE_IDs named in WFSYNC_SOURCES; unset or empty selects none. An unknown name is an
    error, not a name to skip: a typo would otherwise silently stop importing a source."""
    names = {n.strip() for n in os.environ.get(SOURCES_ENV, "").split(",")} - {""}
    unknown = sorted(names - set(SOURCE_IDS))
    if unknown:
        raise ConfigError(f"{SOURCES_ENV}: unknown source {', '.join(unknown)}; valid: {', '.join(SOURCE_IDS)}")
    return names


def configured_sources() -> list[ApiSource]:
    """(module, account type, client factory) for every selected API source. A selected source
    whose secret files are missing fails in its factory, so it shows as an error instead of
    silently dropping out."""
    selected = selected_sources()
    return [s for s in API_SOURCES if s[0].SOURCE_ID in selected]


def upload_sources() -> dict[str, UploadSource]:
    """The selected sources without an API, by SOURCE_ID."""
    selected = selected_sources()
    return {k: m for k, m in UPLOAD_SOURCES.items() if k in selected}


def wealthfolio() -> Wealthfolio:
    return Wealthfolio(os.environ.get("WFSYNC_WEALTHFOLIO_URL", DEFAULT_WEALTHFOLIO_URL), config.public_origin())


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def load_status() -> dict[str, Any]:
    try:
        with open(os.path.join(STATE_DIR, "status.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_status(status: dict[str, Any]) -> None:
    path = os.path.join(STATE_DIR, "status.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=1)
    os.replace(tmp, path)


def batches(rows: list[Row], group_of: dict[int, str | None]) -> Iterator[list[Row]]:
    """`rows` in lists of at most BATCH, never separating the rows of a group: Wealthfolio refuses
    a batch as a whole, so both legs of a trade must share its fate."""
    units: dict[object, list[Row]] = {}
    for r in rows:
        group = group_of[r["lineNumber"]]
        units.setdefault(("row", r["lineNumber"]) if group is None else group, []).append(r)
    chunk: list[Row] = []
    for unit in units.values():
        if chunk and len(chunk) + len(unit) > BATCH:
            yield chunk
            chunk = []
        chunk = chunk + unit
    if chunk:
        yield chunk


def sync_source(wf: Wealthfolio, module: Source, account_type: str, client: Client,
                dry_run: bool) -> SourceResult:
    data = client.fetch()
    # A dry run writes nothing to Wealthfolio, not even the account; without one there is nothing to compare.
    account_id = wf.ensure_account(module.SOURCE_ID, module.ACCOUNT_NAME, account_type, data["currency"],
                                   create=not dry_run)
    if account_id is None:
        return {"status": "ok", "new": None, "dry_run": True, "note": "account is created on the first real run"}
    existing = wf.activities(account_id)
    known = refs_of(existing)
    # API sources price only new records here, statements check their anchor.
    complete = getattr(client, "complete", None)
    if complete:
        complete(data, known)
    rows, skipped = module.map_activities(data, account_id)
    rows = [r for r in unique_rows(rows) if r["comment"] not in known]
    skipped = [s for s in skipped if s.ref not in known]
    resolve = getattr(module, "resolve_listings", None)
    manual = resolve(wf, rows, data, existing) if resolve else []
    rows.sort(key=lambda r: r["date"])
    group_of: dict[int, str | None] = {}
    for i, r in enumerate(rows, 1):
        r["lineNumber"] = i
        group_of[i] = r.pop(GROUP_KEY, None)
    checked = [r for chunk in batches(rows, group_of) for r in wf.check(chunk)]
    # Every row must come back exactly once: a row the response leaves out would be neither imported
    # nor reported.
    returned = [r.get("lineNumber") for r in checked]
    if sorted(returned, key=str) != sorted(group_of, key=str):
        raise RuntimeError("Wealthfolio check response does not match our line numbers")
    invalid = [r for r in checked if not r["isValid"]]
    valid = [r for r in checked if r["isValid"]]
    # Our reference differs, so this is a second transaction that looks the same to Wealthfolio.
    # Importing it would need `forceImport`, dropping it silently would lose it: it is reported.
    duplicates = [r for r in valid
                  if r.get("duplicateOfId") is not None or r.get("duplicateOfLineNumber") is not None]
    fresh = [r for r in valid if r not in duplicates]
    broken_groups = {group_of[r["lineNumber"]] for r in invalid + duplicates} - {None}
    held_back = [r for r in fresh if group_of[r["lineNumber"]] in broken_groups]
    fresh = [r for r in fresh if r not in held_back]
    error_fields = sorted({field for r in invalid for field in (r.get("errors") or {})})
    new, not_imported = 0, 0
    if dry_run:
        new = len(fresh)
    else:
        for chunk in batches(fresh, group_of):
            outcome = wf.import_rows(chunk)
            new += outcome["imported"]
            not_imported += outcome["skipped"]
    pending = sum(s.transient for s in skipped)
    return {
        "status": "partial" if invalid or duplicates or not_imported or pending else "ok",
        "new": new,
        "invalid": len(invalid),
        "duplicates": len(duplicates),
        "held_back": len(held_back),
        "not_imported": not_imported,
        "pending": pending,
        "invalid_fields": error_fields,
        "skipped": dict(Counter(s.reason for s in skipped)),
        "manual_pricing": manual,
        "dry_run": dry_run,
    }


def unique_rows(rows: list[Row]) -> list[Row]:
    """Rows with each reference once. A source can return a record twice within one fetch (a
    transaction arriving while Bitvavo's history is paged by offset shifts the pages): the same row
    again is dropped, a different row under the same reference stops the source."""
    seen: dict[str, Row] = {}
    for r in rows:
        prev = seen.setdefault(r["comment"], r)
        if prev is not r and prev != r:
            raise RuntimeError("two different rows share one source reference")
    return list(seen.values())


def error_text(e: BaseException) -> str:
    """A failure without figures: HTTP status and code, otherwise only the class, plus the function
    where it happened. Exception messages stay out: they can carry figures."""
    if isinstance(e, secrets.MissingSecret):
        return f"Credentials missing ({e})"
    if isinstance(e, statement.StatementError):
        return str(e)  # written for the page, carries no figures
    text = str(e) if isinstance(e, HttpError) else type(e).__name__
    # The innermost frame of our own code that does more than move bytes: an HTTP error always
    # passes through httpjson and the clients' get/pages/call, which would name the same spot.
    frames = [f for f in traceback.extract_tb(e.__traceback__)
              if f"{os.sep}wfsync{os.sep}" in f.filename and not f.filename.endswith("httpjson.py")
              and f.name not in TRANSPORT_FUNCTIONS]
    if frames:
        text += f" in {frames[-1].filename.rsplit(os.sep + 'wfsync' + os.sep, 1)[1]} {frames[-1].name}"
    return text


def sync_all(dry_run: bool) -> dict[str, SourceResult]:
    sources = configured_sources()
    if not sources:  # nothing to fetch: statements arrive by upload, if any source is selected at all
        if not upload_sources():
            log.error("%s", NO_SOURCES)
        return {}
    wf = wealthfolio()
    try:
        wf.login(secrets.read_secret("wealthfolio_password"))
    except (HttpError, OSError, secrets.MissingSecret) as e:
        log.error("wealthfolio login failed: %s", error_text(e))
        return {"wealthfolio": {"status": "error", "error": "Login: " + error_text(e)}}
    results: dict[str, SourceResult] = {}
    for module, account_type, factory in sources:
        try:
            results[module.SOURCE_ID] = sync_source(wf, module, account_type, factory(), dry_run)
            log.info("%s: %s", module.SOURCE_ID, results[module.SOURCE_ID]["status"])
        except Exception as e:  # noqa: BLE001 - one broken source must not stop the others
            results[module.SOURCE_ID] = {"status": "error", "error": error_text(e)}
            log.error("%s: %s", module.SOURCE_ID, error_text(e))
    return results


def lock_path() -> str:
    return os.path.join(STATE_DIR, "run.lock")


def is_running() -> bool:
    """True while a run holds the lock. The `running` flag alone can outlive a killed run."""
    try:
        with open(lock_path(), "a") as f:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
    except BlockingIOError:
        return True
    except OSError:
        return False


def acquire(lock: IO[str], attempts: int = LOCK_ATTEMPTS) -> bool:
    """Exclusive lock, retried briefly: the page's probe (`is_running`) holds a shared lock for
    an instant, a real run holds it for minutes."""
    for i in range(attempts):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if i + 1 < attempts:
                time.sleep(LOCK_RETRY_S)
    return False


def run(dry_run: bool = False, trigger: str = "manual") -> dict[str, Any] | None:
    # File lock, because `docker exec ... run` is a second process next to the server.
    lock = open(lock_path(), "a")  # noqa: SIM115 - held until the `finally` below
    if not acquire(lock):
        lock.close()
        log.info("sync already running, request ignored")
        return None
    status = load_status()
    try:
        status.update({"started": now(), "trigger": trigger, "running": True})
        save_status(status)
        try:
            results = sync_all(dry_run)
        except Exception as e:  # noqa: BLE001 - the run must end with a status, whatever broke
            log.error("sync failed: %s", error_text(e))
            results = {"wealthfolio": {"status": "error", "error": error_text(e)}}
        # An empty result is a failure, except where nothing can be fetched by design: with upload
        # sources alone a run has no work, and health checks on `ok` must not stay red for that.
        nothing_to_fetch = not results and not configured_sources() and bool(upload_sources())
        all_ok = all(r.get("status") == "ok" for r in results.values())
        status.update({"finished": now(), "results": results, "ok": all_ok and (bool(results) or nothing_to_fetch)})
        if status["ok"] and results and not dry_run:
            status["last_success"] = status["finished"]
        return status
    finally:
        status["running"] = False
        try:
            save_status(status)
        except OSError as e:
            log.error("status not saved: %s", type(e).__name__)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def import_statement(source_id: str, raw: bytes) -> SourceResult | None:
    """Import an uploaded statement of a selected upload source now, under the run lock. The
    result goes to `status["uploads"][source_id]`, apart from the nightly results: the nightly `ok`
    (and any health check on it) only covers the sources a run can fetch by itself.
    Returns None if a run holds the lock."""
    module = upload_sources()[source_id]
    lock = open(lock_path(), "a")  # noqa: SIM115 - held until the `finally` below
    if not acquire(lock):
        lock.close()
        log.info("sync already running, upload ignored")
        return None
    try:
        status = load_status()
        result: SourceResult = {"status": "error", "at": now()}
        try:
            client: StatementClient = module.Statement(raw)
            result["statement"] = statement.statement_range(client.fetch())
            wf = wealthfolio()
            wf.login(secrets.read_secret("wealthfolio_password"))
            if wf.ensure_account(module.SOURCE_ID, module.ACCOUNT_NAME, module.ACCOUNT_TYPE, module.CURRENCY,
                                 create=False) is None:
                client.complete(client.fetch(), set())  # refuse before the account is created, not after
            result.update(sync_source(wf, module, module.ACCOUNT_TYPE, client, dry_run=False))
        except Exception as e:  # noqa: BLE001 - the upload must end with a status, whatever broke
            result["status"], result["error"] = "error", error_text(e)
        log.info("%s upload: %s", source_id, result["status"])
        uploads = status.get("uploads")
        status["uploads"] = {**(uploads if isinstance(uploads, dict) else {}), source_id: result}
        try:
            save_status(status)
        except OSError as e:
            log.error("status not saved: %s", type(e).__name__)
        return result
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def backup(keep: int = 3) -> str:
    """Ask Wealthfolio for a consistent copy of its encrypted database and return its file name.
    Prunes only copies this command made (listed in /state/backups.json): Wealthfolio's own backup
    button writes to the same place with the same `reason`."""
    wf = wealthfolio()
    wf.login(secrets.read_secret("wealthfolio_password"))
    name = cast(str, wf.backup()["filename"])
    backups = wf.list_backups()
    made = next((b for b in backups if b.get("filename") == name), None)
    if not made or made.get("protection") != "encrypted":
        raise RuntimeError("backup missing or not encrypted")
    present = {b.get("filename") for b in backups}
    with open(own_backups_path() + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # two backup runs must not drop each other's names
        own = [n for n in load_own_backups() if n in present and n != name] + [name]
        save_own_backups(own)  # the new copy is tracked even if pruning fails later
        kept = own[-keep:]
        for old in own[:-keep]:
            try:
                wf.delete_backup(old)
            except (HttpError, OSError) as e:
                # The new copy is good either way; the old one stays listed for the next backup.
                log.error("old backup not deleted: %s", error_text(e))
                kept.insert(0, old)
        save_own_backups(kept)
    return name


def own_backups_path() -> str:
    return os.path.join(STATE_DIR, "backups.json")


def load_own_backups() -> list[str]:
    try:
        with open(own_backups_path(), encoding="utf-8") as f:
            names = json.load(f)
        return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []
    except (OSError, ValueError):
        return []


def save_own_backups(names: list[str]) -> None:
    tmp = own_backups_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(names, f)
    os.replace(tmp, own_backups_path())
