"""Data forms shared across the package."""

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, NotRequired, Protocol, Required, TypedDict

Json = dict[str, Any]


class Row(TypedDict, total=False):
    """A Wealthfolio `ActivityImport`, as built here and as the import check returns it."""

    date: Required[str]
    symbol: Required[str]
    activityType: Required[str]
    currency: Required[str]
    accountId: Required[str]
    comment: Required[str]
    isDraft: Required[bool]
    isValid: Required[bool]
    isin: str
    symbolName: str
    quantity: str
    unitPrice: str
    amount: str
    fee: str
    fxRate: str
    subtype: str
    instrumentType: str
    isExternal: bool
    exchangeMic: str
    quoteCcy: str
    quoteMode: str
    lineNumber: int
    _group: str
    errors: dict[str, Any]
    duplicateOfId: str | None
    duplicateOfLineNumber: int | None


class SourceResult(TypedDict, total=False):
    """Outcome of one source in `status.json`; the page reads it through `web.details`."""

    status: Required[str]
    new: int | None
    invalid: int
    duplicates: int
    held_back: int
    not_imported: int
    pending: int
    invalid_fields: list[str]
    skipped: dict[str, int]
    manual_pricing: list[str]
    dry_run: bool
    note: str
    error: str
    at: str
    statement: str


class ImportOutcome(TypedDict):
    imported: int
    skipped: int


class Record(TypedDict):
    """A statement row that becomes an activity."""

    line: int
    date: str
    kind: str
    amount: Decimal
    change: Decimal
    ref: NotRequired[str]
    fee: NotRequired[Decimal]
    balance: NotRequired[Decimal]
    txid: NotRequired[str]
    type: NotRequired[str]
    credited: NotRequired[Decimal]


class StatementData(TypedDict):
    currency: str
    records: list[Record]
    days: tuple[str, str]
    from_start: bool


class MarketData(Protocol):
    """The part of the Wealthfolio client that chooses listings."""

    def search(self, query: str) -> list[Json]: ...
    def quote(self, symbol: str, exchange_mic: str | None = None) -> Json: ...


class Client(Protocol):
    """Where a source gets its records from. May also offer `complete(data, known)`, which runs
    once the imported references are known."""

    def fetch(self) -> Any: ...


class StatementClient(Client, Protocol):
    def complete(self, data: Any, known: set[str]) -> None: ...


class Source(Protocol):
    """A module in `wfsync.sources`. May also offer `resolve_listings(wf, rows, data, existing)`."""

    @property
    def SOURCE_ID(self) -> str: ...
    @property
    def ACCOUNT_NAME(self) -> str: ...
    def map_activities(self, data: Any, account_id: str, /) -> tuple[list[Row], list[Any]]: ...


class UploadSource(Source, Protocol):
    @property
    def ACCOUNT_TYPE(self) -> str: ...
    @property
    def CURRENCY(self) -> str: ...
    @property
    def UPLOAD_ACCEPT(self) -> str: ...
    @property
    def UPLOAD_HELP(self) -> str: ...
    def Statement(self, raw: bytes, /) -> StatementClient: ...


Status = Mapping[str, Any]
