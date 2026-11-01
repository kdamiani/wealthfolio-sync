"""Settings from the environment. An invalid value stops the process with a clear message."""

import os
import re
import urllib.parse

ORIGIN_ENV = "WFSYNC_ORIGIN"
ALIASES_ENV = "WFSYNC_YAHOO_ALIASES"
# Yahoo appends the CoinMarketCap id when a ticker collides. Every alias is confirmed by price.
BUILTIN_YAHOO_ALIASES = {"POL": "POL28321"}
ALIAS = re.compile(r"([A-Za-z0-9._-]+)=([A-Za-z0-9._-]+)")
DEFAULT_PORTS = {"http": 80, "https": 443}


class ConfigError(ValueError):
    """The configuration cannot work; the message tells the operator what to change."""


def public_origin() -> str:
    """WFSYNC_ORIGIN, checked: the page compares it to the browser's `Origin` header character by
    character, so anything a browser would not send locks every POST out."""
    value = os.environ.get(ORIGIN_ENV, "")
    bad = ConfigError(f"{ORIGIN_ENV} must be a browser origin such as https://wealthfolio.example.com: "
                      "scheme, lowercase host and optional port, without path or trailing slash")
    parts = urllib.parse.urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        raise bad from None
    if (parts.scheme not in DEFAULT_PORTS or not parts.hostname or parts.username is not None
            or parts.path or parts.query or parts.fragment or value != value.lower()
            or "#" in value or "?" in value or port == DEFAULT_PORTS[parts.scheme]):
        raise bad
    return value


def yahoo_aliases() -> dict[str, str]:
    """The built-in aliases plus WFSYNC_YAHOO_ALIASES (`SYMBOL=YAHOO,SYMBOL=YAHOO`)."""
    value = os.environ.get(ALIASES_ENV, "").strip()
    aliases = dict(BUILTIN_YAHOO_ALIASES)
    for pair in value.split(",") if value else ():
        match = ALIAS.fullmatch(pair.strip())
        if not match:
            raise ConfigError(f"{ALIASES_ENV} must look like SYMBOL=YAHOO,SYMBOL=YAHOO")
        aliases[match.group(1)] = match.group(2)
    return aliases
