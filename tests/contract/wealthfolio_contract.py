#!/usr/bin/env python3
"""Checks the client against a running Wealthfolio with a throwaway database.

    WFSYNC_WEALTHFOLIO_URL=http://127.0.0.1:8088 WFSYNC_ORIGIN=https://wealthfolio.example.com \\
    WF_PASSWORD=... python3 -m tests.contract.wealthfolio_contract

It creates an account and imports rows: never point it at a Wealthfolio with data that matters.
"""

import os
import sys
import types
import uuid

from wfsync import sync
from wfsync.httpjson import HttpError
from wfsync.rows import row
from wfsync.wealthfolio import ImportRefused, Wealthfolio, refs_of

DEPOSIT_DAY = "2020-03-02T10:00:00Z"


def source(source_id, rows):
    return types.SimpleNamespace(
        SOURCE_ID=source_id, ACCOUNT_NAME="Contract " + source_id,
        map_activities=lambda data, account_id: ([dict(r, accountId=account_id) for r in rows], []))


def client():
    return types.SimpleNamespace(fetch=lambda: {"currency": "EUR"})


def deposit(ref, amount, day=DEPOSIT_DAY):
    return row("", day, "DEPOSIT", "EUR", ref, amount=amount)


def expect(name, actual, wanted):
    if actual != wanted:
        sys.exit(f"FAILED {name}: {actual!r} != {wanted!r}")
    print("ok", name)


def main():
    wf = Wealthfolio(os.environ["WFSYNC_WEALTHFOLIO_URL"], os.environ["WFSYNC_ORIGIN"])
    wf.login(os.environ["WF_PASSWORD"])
    source_id = "contract-" + uuid.uuid4().hex[:8]
    prefix = f"wfsync:{source_id}:"
    rows = [deposit(prefix + str(i), "10.123456789", f"2020-03-{i + 2:02d}T10:00:00Z") for i in range(3)]
    module = source(source_id, rows)

    expect("no account before the first run", wf.ensure_account(source_id, "x", "CASH", "EUR", create=False), None)
    first = sync.sync_source(wf, module, "CASH", client(), False)
    expect("first run", (first["status"], first["new"], first["duplicates"]), ("ok", 3, 0))

    account_id = wf.ensure_account(source_id, module.ACCOUNT_NAME, "CASH", "EUR", create=False)
    expect("search returns every row", refs_of(wf.activities(account_id)), {r["comment"] for r in rows})
    again = sync.sync_source(wf, module, "CASH", client(), False)
    expect("second run imports nothing", (again["status"], again["new"]), ("ok", 0))

    # Same day and amount under a new reference stays a separate transaction.
    twin = deposit(prefix + "twin", "10.123456789", rows[0]["date"])
    third = sync.sync_source(wf, source(source_id, [*rows, twin]), "CASH", client(), False)
    expect("twin is imported", (third["status"], third["duplicates"], third["new"]), ("ok", 0, 1))

    # The same reference twice in one batch is the duplicate Wealthfolio flags.
    in_batch = [dict(deposit(prefix + "same", "3", "2020-05-01T10:00:00Z"), accountId=account_id, lineNumber=i + 1)
                for i in range(2)]
    flagged = wf.check(in_batch)
    expect("duplicate within a batch", flagged[1].get("duplicateOfLineNumber"), 1)

    refused = [dict(deposit(prefix + "bad", "4"), accountId=account_id, lineNumber=1, activityType="NOT_A_TYPE")]
    try:
        outcome = wf.import_rows(refused)
    except (ImportRefused, HttpError):
        outcome = "refused"
    expect("invalid batch writes nothing", (outcome, prefix + "bad" in refs_of(wf.activities(account_id))),
           ("refused", False))
    print("contract holds")


if __name__ == "__main__":
    main()
