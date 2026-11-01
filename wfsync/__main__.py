"""Entry point: `python -m wfsync serve | run [--dry-run] | backup`."""

import argparse
import logging
import threading
import time
from datetime import date, datetime, timedelta

from . import config, sync, web

NIGHTLY_WINDOW_MIN = 60  # a run missed by more than this (container down) waits for the next night

EXIT_BUSY = 75  # EX_TEMPFAIL: another run holds the lock, not a failure
EXIT_CONFIG = 78  # EX_CONFIG: a setting cannot work

log = logging.getLogger("wfsync")


def due(now: datetime, last_day: date | None) -> bool:
    """Nightly run due? Wall-clock check each minute instead of one long sleep, so DST changes and a
    late clock after boot do not shift or repeat the run."""
    start = now.replace(hour=sync.NIGHTLY_AT[0], minute=sync.NIGHTLY_AT[1], second=0, microsecond=0)
    return last_day != now.date() and start <= now < start + timedelta(minutes=NIGHTLY_WINDOW_MIN)


def nightly_loop() -> None:
    last_day: date | None = None
    while True:
        now = datetime.now()
        if due(now, last_day):
            last_day = now.date()
            try:
                sync.run(trigger="nightly")
            except Exception as e:  # noqa: BLE001 - the loop must survive any failure
                log.error("nightly run failed: %s", type(e).__name__)
        time.sleep(60)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="wfsync")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    run_p = sub.add_parser("run")
    run_p.add_argument("--dry-run", action="store_true")
    sub.add_parser("backup")
    args = parser.parse_args()
    try:
        config.public_origin()
        config.yahoo_aliases()
        selected = sync.selected_sources() if args.cmd != "backup" else set()  # `backup` touches no source
    except config.ConfigError as e:
        log.error("%s", e)
        raise SystemExit(EXIT_CONFIG) from None
    if args.cmd == "serve":
        if not selected:
            log.warning("%s", sync.NO_SOURCES)
        threading.Thread(target=nightly_loop, daemon=True).start()
        web.serve(8090)
    elif args.cmd == "run":
        status = sync.run(dry_run=args.dry_run, trigger="console")
        if status is None:
            raise SystemExit(EXIT_BUSY)
        raise SystemExit(0 if status.get("ok") else 1)
    elif args.cmd == "backup":
        print(sync.backup())


if __name__ == "__main__":
    main()
