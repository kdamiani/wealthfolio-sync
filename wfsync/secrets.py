"""Secrets come from files mounted read-only into the container, one value per file."""

import os

SECRETS_DIR = os.environ.get("WFSYNC_SECRETS_DIR", "/run/secrets")


class MissingSecret(RuntimeError):
    """A secret file is absent or empty; the message names the file, never a value."""


def read_secret(name: str) -> str:
    try:
        with open(os.path.join(SECRETS_DIR, name), encoding="utf-8") as f:
            value = f.read().strip()
    except FileNotFoundError:
        raise MissingSecret(name) from None
    if not value:
        raise MissingSecret(name)
    return value
