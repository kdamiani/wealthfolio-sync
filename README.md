# wealthfolio-sync

A self-hosted importer for [Wealthfolio](https://github.com/wealthfolio/wealthfolio), focused on European platforms.

It fetches transactions read-only from Trading 212, Bitvavo and Bitget (spot), takes uploaded account statements from
Monefit SmartSaver (CSV), Go & Grow (Excel) and Debitum (Excel), and adds what is new to your own Wealthfolio instance.
Your data stays on your server: wealthfolio-sync only sends read-only queries to the platforms you select. Wealthfolio
itself makes its own outbound requests, such as market data and its update check.

Tested with Wealthfolio 3.11.0. It uses Wealthfolio's internal API, so a new Wealthfolio version can break it: after
upgrading, run `python -m wfsync run --dry-run`, or with statement sources only, upload a statement you imported
before and check that nothing new appears. A dry run without API sources does not reach Wealthfolio.

This is an independent project. It is not affiliated with or endorsed by Wealthfolio or any of the platforms named
here. It is not financial advice. Use it at your own risk and check what it imports.

Python 3.13 or newer, standard library only, no dependencies.

## Quick start

You need Docker with Compose, `openssl`, the [`argon2`](https://github.com/P-H-C/phc-winner-argon2) command line tool,
a host name for Wealthfolio, and a TLS certificate for it. Clone this repository; the files are in
[`examples/`](examples/): Wealthfolio, wealthfolio-sync and nginx on one origin.

1. Prepare the settings:

   ```sh
   cd examples
   install -m 600 .env.example .env
   openssl rand -base64 32                                       # WF_SECRET_KEY
   printf '%s' 'your-password' | argon2 "$(openssl rand -base64 16)" -id -e   # WF_AUTH_PASSWORD_HASH
   ```

   Enter both in `.env`, keeping the single quotes around the hash, and set `WFSYNC_ORIGIN` and `WFSYNC_SOURCES`.

2. Write one file per secret (see [Secret files](#secret-files)), all of them before handing the directory to
   uid 10001. To add one later, you need `sudo`.

   ```sh
   mkdir -m 700 secrets
   printf '%s' 'your-password' > secrets/wealthfolio_password
   # ... one file per secret of the sources you selected ...
   chmod 400 secrets/*
   sudo chown -R 10001:10001 secrets
   ```

3. Put the certificate into `certs/fullchain.pem` and `certs/privkey.pem`. For a local trial, use a self-signed
   certificate for the host name in `WFSYNC_ORIGIN` and point that name to the machine in `/etc/hosts`:

   ```sh
   mkdir certs
   openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj '/CN=wealthfolio.example.com' \
     -addext 'subjectAltName=DNS:wealthfolio.example.com' -keyout certs/privkey.pem -out certs/fullchain.pem
   ```

   The proxy runs as uid 101 and has to read the key, so run this for any certificate and again after each renewal:

   ```sh
   sudo chown 101:101 certs/privkey.pem
   ```

4. Start it. The build runs the tests and fails if one is red. A released image can replace the build:

   ```sh
   gh attestation verify oci://ghcr.io/<owner>/<repository>:<version> --repo <owner>/<repository> \
     --signer-workflow <owner>/<repository>/.github/workflows/release.yml
   ```

   That checks that the image was built by this repository's release workflow.

   ```sh
   docker compose up -d --build
   ```

5. Log in to Wealthfolio at your origin, then open `/sync` on the same origin. The page shows the selected sources and
   their last result, starts a run and takes the statement uploads. Accounts are created on the first import.

## Commands

- `python -m wfsync serve`: the page on port 8090 and the nightly run at 03:30 in the container's time zone (`TZ`).
  This is the image's default command. Statement sources do not run at night.
- `python -m wfsync run [--dry-run]`: one run by hand. A dry run writes nothing to Wealthfolio, not even the account;
  `status.json` is still updated.
- `python -m wfsync backup`: asks Wealthfolio for an encrypted copy of its database and prints the file name on stdout.
  On a new volume the copy lies under `profiles/<id>/backups/`. The command keeps three of its own copies (listed in
  `/state/backups.json`) and never deletes copies from Wealthfolio's own backup button or older ones. The Wealthfolio database has to be encrypted, as in the example
  (`WF_DB_REQUIRE_ENCRYPTION`).

With the example, run them in the container: `docker compose exec wealthfolio-sync python -m wfsync run --dry-run`.

| Exit code of `run` | Meaning |
| --- | --- |
| 0 | All selected API sources are `ok`, or only statement sources are selected, so there was nothing to fetch |
| 1 | A source is `error` or `partial`, or no source is selected |
| 75 | Another run is active |
| 78 | A setting is invalid (see [Environment variables](#environment-variables)). `serve` stops with the same code, and under a restart policy Docker keeps starting it, so check `docker compose logs wealthfolio-sync`. `backup` ends the same way, except that it ignores `WFSYNC_SOURCES` |

| Status of a source | Meaning | Last success |
| --- | --- | --- |
| `ok` | Everything importable arrived. Skipped records stay listed on the page, on every run | Updated |
| `partial` | Rows are missing: rejected by Wealthfolio, reported as a duplicate, skipped on import, held back with the other leg of a trade, or, for seven days from their date, waiting for a price | Kept |
| `error` | The source stopped; what was imported before stays | Kept |

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `WFSYNC_ORIGIN` | required | Public origin of Wealthfolio, such as `https://wealthfolio.example.com`: scheme, lowercase host and a port only if it is not the default, no path, no trailing slash. Sent to Wealthfolio's API and required on `POST /sync/*` |
| `WFSYNC_SOURCES` | empty | Comma-separated: `trading212`, `bitvavo`, `bitget`, `monefit`, `goandgrow`, `debitum` |
| `WFSYNC_WEALTHFOLIO_URL` | `http://wealthfolio:8088` | Wealthfolio as seen from this container |
| `WFSYNC_YAHOO_ALIASES` | empty | `SYMBOL=YAHOO,SYMBOL=YAHOO`: Yahoo tickers for coins whose symbol collides with another listing, such as `ABC=ABC12345`. Added to the built-in `POL=POL28321`, and each one still has to match the exchange's price |
| `WFSYNC_SECRETS_DIR` | `/run/secrets` | Directory of the secret files |
| `WFSYNC_STATE_DIR` | `/state` | Writable directory for `status.json`, `run.lock`, `backups.json` and their lock and temporary files |
| `TZ` | container default | Time zone of the nightly run and the page |

Only selected sources run, appear on the page and accept uploads. With `WFSYNC_SOURCES` unset or empty no source
runs, the page says so, and a run does not count as successful. An unknown name stops `run` and `serve` and lists the
valid names; `backup` ignores the selection. An invalid `WFSYNC_ORIGIN` or `WFSYNC_YAHOO_ALIASES` stops every command.
A selected source without its secret files shows as an error.

Messages of `serve` and `run` go to stderr, per source its status and, on errors, the HTTP status with a short error
code or the error class, plus the function it occurred in, never the error text itself. If `backup` fails, it ends with
exit code 1 and Python's traceback (file names and HTTP status, no portfolio values).

## Secret files

Every secret is one file in `WFSYNC_SECRETS_DIR`, mounted read-only.

| Used by | Files |
| --- | --- |
| Always | `wealthfolio_password`: the password behind `WF_AUTH_PASSWORD_HASH` |
| `trading212` | `t212_key`, `t212_secret` |
| `bitvavo` | `bitvavo_key`, `bitvavo_secret` |
| `bitget` | `bitget_key`, `bitget_secret`, `bitget_passphrase` |
| `monefit`, `goandgrow`, `debitum` | none |

Create the API keys with read permissions only, without trading or withdrawal rights, and restrict them to your
server's IP address where the platform allows it.

If a file is missing or empty, the source (or, for `wealthfolio_password`, the Wealthfolio row) shows the error
`Credentials missing` on `/sync`, and the run does not count as successful. With statement sources only, a missing
`wealthfolio_password` shows at the upload instead, since a run has nothing to do.

## Reverse proxy

The page must live on the same origin as Wealthfolio, because it uses Wealthfolio's login:

- Wealthfolio sets its cookie `wf_session` with `Path=/api`, so the browser would not send it to `/sync`. The proxy
  rewrites the path, in nginx with `proxy_cookie_path /api /;`.
- `/sync` goes to port 8090 of wealthfolio-sync, everything else to Wealthfolio.
- With nginx, the `/sync` location sets `client_body_buffer_size 3m` and `client_max_body_size 3m`, so that a statement
  never reaches a temporary file on disk and a larger body is refused with 413 before it is read.
- The proxy forwards `Host` and `X-Forwarded-Proto: https`, which Wealthfolio needs to mark its cookie `Secure`.
- The nginx example sends HSTS, limits requests and connections on `/sync` and requests on the login, and keeps other
  request bodies on a tmpfs.
- The nginx example resolves the containers per request through Docker's DNS, so a stopped wealthfolio-sync does not
  keep nginx from starting, and a recreated container is found again.

## Security model

- `/sync` needs a valid Wealthfolio session (`wf_session`, checked against `/auth/me`), so there is one login for both.
  `POST /sync/run` and the uploads (`POST /sync/monefit`, `/sync/goandgrow`, `/sync/debitum`) also need the matching
  `Origin`. `/healthz` is public to the container network and reveals nothing about the accounts. A valid session is
  cached, so after logging out `/sync` stays open for up to 60 s.
- A statement may be at most 2 MiB. It is processed in memory and never stored; it would otherwise lie unencrypted
  next to Wealthfolio's encrypted database. On disk only its result stays in `status.json` under `uploads`: period,
  time, counts and error message.
- Logs and the status file contain no amounts, quantities or account numbers. API error messages are reduced to the
  HTTP status and a short error code. Counts are stored only in `status.json`, for the page.
- No HTTP client follows redirects, since urllib would pass the `Authorization` header to any target; a 3xx response
  ends as an error. A request to the page is closed when a single socket operation stalls for 30 s.
- The container runs read-only, without capabilities, as uid 10001, with the secrets mounted read-only. In the example
  only the proxy publishes ports, and Wealthfolio, wealthfolio-sync and the proxy meet on an internal network.

## Limits

Anything that cannot be mapped with certainty is skipped or stops the import, rather than being imported wrongly.

| Source | Skipped | Stops the import |
| --- | --- | --- |
| Trading 212 | GBX (pence) instruments with their buys, dividends and splits; transactions with an unexpected sign | |
| Bitvavo | Trades with a fee in the coin or a third currency; crypto transfers with a fee | |
| Bitget | Groups of an unsupported form: partial fills, other pairs, fees on the other side | A broken balance chain |
| Monefit | | Vault transactions, card fees, non-zero fees, unknown labels, a broken balance chain, changed history |
| Go & Grow | | Any other payment type, a broken balance chain, changed history. Only the German export is supported |
| Debitum | | Loyalty points (DEB), vouchers, exchange trades, donations, most reversals, a wrong sign, a failed check against the summary, changed history |

When Bitvavo or Bitget has no price for a coin, a record younger than seven days waits and the source stays `partial`.
An older one counts as skipped at once, also on the first import of an old history, so the source can be `ok` while the
record is missing. Check the skipped records on the page after the first run. Every run tries them again. If a price
lookup fails with anything but 400 or 404, the source ends with an error.

Some mappings follow the platform's documentation or type catalogue and are unconfirmed: Bitvavo sells, crypto
withdrawals and staking; Monefit withdrawals and earnings; Go & Grow returns and withdrawals; Debitum's types other than
deposits, subscriptions, investments, taxes and repayments. The header of each file in `wfsync/sources/` says what is
confirmed. The label of Go & Grow interest comes from the descriptions of its former API, and zero returns are missing
from the export.

Bitget futures are out of scope: transfers to and from them count as external transfers.

## Reconciliation

### Duplicates

Duplicates are prevented by the source reference in the `comment` field (`wfsync:t212:...`, `wfsync:bitvavo:...`,
`wfsync:bitget:spot:<id>`). If Wealthfolio's own check still reports a new row as a duplicate of something else, the row
is not imported: the source turns `partial`, the page counts the row, and the other leg of a trade is held back.

### Trading 212

The venue comes from the metadata (`workingScheduleId` to exchange to MIC). Wealthfolio's search supplies the listing,
preferably on Trading 212's venue, pinned with a Yahoo suffix if the search finds nothing there. What cannot be
assigned with certainty is priced manually (`quoteMode` MANUAL) rather than wrongly.

The listing is chosen in the currency of the instrument, even for a dividend paid in EUR on a US stock, so that buys
and dividends of one ISIN share one asset. Later runs find the asset by its ISIN, which Wealthfolio checks before
symbol and exchange on import, as long as exactly one asset carries it.

### Bitvavo

History starts at `fromDate=0`. Incoming coins without a price are valued at the hourly price (candles). Price pairs
`<COIN>-EUR` only count if their quote matches Bitvavo's price. A coin that already has an asset in the account keeps
its pricing without a new check, since coins have no ISIN.

### Bitget (spot only)

The source is the tax record endpoint `/api/v2/tax/spot-record` in 30-day windows from 2018 on. A full page halves the
window, because Bitget sorts a page ascending and `idLessThan` does not page further.

The running balances give the spot holdings and are the guard: if the chain `previous + amount - |fee|` breaks for a
coin, the source ends with an error instead of importing an incomplete history.

USDT is a coin of its own in EUR (`USDT-EUR`). A buy against USDT is imported as a USDT sell and a coin buy of equal EUR
value at Bitget's hourly USDT/EUR price. An exchange from EUR to USDT uses the rate actually paid. A fiat buy belongs to
the next USDT credit up to 15 minutes later and is imported under the reference of its EUR record; a credit already
imported on its own is never assigned afterwards.

Both legs of a trade share an import batch, and if Wealthfolio rejects one, the other is held back. The assignment uses
the row's own `lineNumber`. A check response that omits a sent row, names one twice or carries an unknown `lineNumber`
stops the source, and so do two different rows under one reference in one fetch, while the same row twice is imported
once.

### Monefit SmartSaver (no API)

Download the account statement from SmartSaver for the whole period since the account was opened, in German or
English. The account is `Monefit SmartSaver` (type `CASH`).

Deposits and withdrawals are `DEPOSIT` and `WITHDRAWAL`, earnings received are `INTEREST` (income side), bonuses are
`CREDIT`/`BONUS` with `isExternal=false`, so a gain rather than new capital, as in Monefit's own total earnings.

The file has no transaction ids. The reference `wfsync:monefit:<kind>:<date>:<hash>` depends on date, kind, amount, fee and
the total wealth afterwards. Total wealth is the guard: each row must equal the previous one plus the amount in the
direction of its kind, otherwise nothing is imported.

An empty account takes only a statement that begins at zero; otherwise the first row of the statement must already be
imported. Every imported transaction from its days (for a statement from zero, also before them) must appear in it,
otherwise Monefit changed the history and the import stops.

### Go & Grow (API closed since 2025-09-30)

Download the account statement for all goals as Excel (.xlsx), starting before the account was opened and ending
today. The account is `Go & Grow` (type `CASH`, EUR).

`SEPA-Banküberweisung` under `Eingänge` is a `DEPOSIT`, under `Ausgänge` a `WITHDRAWAL`, `Go & Grow Zinsen` under
`Eingänge` is `INTEREST`.

The file is read with `zipfile` and ElementTree through `wfsync/sources/xlsx.py`. Its limits: at most 4 MiB unpacked per
part, stored or deflate only, depth at most 32, no document type declaration (checked by expat itself, so in any
encoding). The Go & Grow reader refuses shared strings because its files have none.

The reference `wfsync:goandgrow:<kind>:<date>:<hash>` covers date, kind, amount and balance afterwards. The balance
chain starts at `Zu Beginn` in the summary and ends at `Ergebnis`. Anchor and range checks work as for Monefit, over the
period of the file (`wfsync/sources/statement.py`).

### Debitum (no investor API)

Download the balance statement for the whole period and all transactions as XLSX; Debitum sends the file by email. The
account is `Debitum` (type `CASH`, EUR).

The account value is Debitum's total balance (cash plus invested capital): subscriptions, investments and principal
repayments are moves inside the account and are not imported. `DEPOSIT` and `WITHDRAWAL` are capital in and out,
interest, late and loyalty interest are `INTEREST`, cashback, promotion and referral bonuses are `CREDIT`/`BONUS` with
`isExternal=false` as for Monefit, taxes are `TAX`, fees are `FEE`.

`REPAYMENT` is missing from the app's type catalogue and is split: the interest, late-interest and bonus parts are
`INTEREST`, the principal stays a move, and the parts must add up to `Total Turnover`.

The types come from the app's catalogue (51 types plus `REPAYMENT`). Each has an expected sign of `Total Turnover`. A
wrong one stops the import. Debitum credits interest and taxes (8 to 9 decimals) truncated to the cent, and that amount
is imported. Shares below one cent are dropped.

`Balance on` in the overview is only the free cash: an executed investment lowers it by what `Closing Investments`
gains, a subscription does not. The guards: opening balance plus `Opening Investments` plus imported rows must equal
closing balance plus `Closing Investments`, deposits must equal their `DEPOSITS`, withdrawals by amount their
`WITHDRAWALS` (the sign there is ignored), and the `TOTAL` row every column sum (up to 1e-9, since Debitum sums in
floating point). So a kind that changes the account value but is taken for a move stops the import, and vice versa.

The reference `wfsync:debitum:<kind>:<date>:<hash>` covers transaction id, kind and normalised amount; an amount
changed afterwards stops as a contradiction. Anchor and range checks work as for Monefit. The file is read like Go &
Grow's through `wfsync/sources/xlsx.py` (shared strings, several sheets, same limits).

## Development

```sh
python3 -m unittest discover -s tests -t .
ruff check .
mypy
```

The Docker image only builds when the tests pass. `tests/contract/wealthfolio_contract.py` checks the client against a
running Wealthfolio with a throwaway database; CI runs it against the image pinned in `examples/docker-compose.yml`.

Bug reports and pull requests are welcome. Keep the runtime free of dependencies, and never put real statements or API
responses into an issue or a test. Security problems go through [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
