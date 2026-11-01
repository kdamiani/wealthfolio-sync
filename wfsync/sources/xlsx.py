"""Reading the uploaded Excel statements (Go & Grow, Debitum) with zipfile and ElementTree only.

Every part is bounded while it is read (at most `MAX_PART` unpacked bytes, whatever size the archive
declares, and only the stored and deflate methods), a document type declaration (the source of entity
expansion) and nesting deeper than `MAX_DEPTH` are refused before parsing, and every failure ends as a
`StatementError` naming the part of the file, never a figure from it.
"""

import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from typing import NoReturn, Self
from xml.parsers import expat

from .statement import StatementError, masked

MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
SHARED_STRINGS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings"
CELL = re.compile(r"([A-Z]{1,3})([0-9]+)")
# Thousands of rows stay below 4 MiB. Larger parts are refused unread; the limit also bounds the
# memory ElementTree needs, and the container has 256 MB.
MAX_PART = 4 * 1024 * 1024
# Parts nest about 7 levels (worksheet, sheetData, row, c, is, r, t); without a limit a few KB of
# unclosed tags fill the container's memory in expat and ElementTree.
MAX_DEPTH = 32
# bzip2 and lzma decompress a whole member at once, ignoring any read limit; Excel writes deflate.
METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)

Grid = dict[tuple[int, str], tuple[str, str]]


class Workbook:
    """The sheets of an .xlsx by name. `not_ours` starts every message ("File is not ...");
    `shared`: whether shared strings are read (a source whose files have none refuses them)."""

    def __init__(self, raw: bytes, not_ours: str, shared: bool = True) -> None:
        self.not_ours = not_ours
        self.shared = shared
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(raw))
        except (zipfile.BadZipFile, NotImplementedError, ValueError):  # NotImplementedError: a newer zip version
            raise StatementError(not_ours) from None
        rels = self.part("xl/_rels/workbook.xml.rels").findall(f"{PKG_REL}Relationship")
        self.targets: dict[str | None, list[tuple[str | None, str | None]]] = {}
        for r in rels:
            self.targets.setdefault(r.get("Id"), []).append((r.get("Type"), r.get("Target")))
        self.sheets: list[tuple[str | None, str | None]] = []
        for sheet in self.part("xl/workbook.xml").findall(f"{MAIN}sheets/{MAIN}sheet"):
            self.sheets.append((sheet.get("name"), sheet.get(f"{REL}id")))
        self.strings: list[str] | None = None

    def close(self) -> None:
        self.zf.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def part(self, name: str) -> ET.Element:
        try:
            info = self.zf.getinfo(name)
        except KeyError:
            raise StatementError(f"{self.not_ours} (part missing)") from None
        if info.file_size > MAX_PART:
            raise StatementError(f"{self.not_ours} (too large)")
        if info.compress_type not in METHODS:
            raise StatementError(f"{self.not_ours} (compression)")
        try:
            with self.zf.open(info) as f:
                # zipfile ends the member at its declared size and checks the CRC there, but without
                # a size it decompresses in blocks of up to 2 GB to get there: the bound keeps them small.
                data = f.read(MAX_PART + 1)
        except (zipfile.BadZipFile, NotImplementedError, OSError, EOFError, zlib.error, RuntimeError, ValueError):
            # RuntimeError: an encrypted entry; ValueError: broken offsets or entry names
            raise StatementError(f"{self.not_ours} (archive damaged)") from None
        self.no_doctype(data)
        try:
            return ET.fromstring(data)  # noqa: S314 - `no_doctype` ran on these bytes
        except ET.ParseError:
            raise StatementError(f"{self.not_ours} (XML)") from None

    def no_doctype(self, data: bytes) -> None:
        """Refuse document type declarations and deep nesting. Checked by expat itself, so the file's
        encoding (UTF-16 hides `<!DOCTYPE` from a byte search) does not matter."""
        def refuse(*_: object) -> NoReturn:
            raise StatementError(f"{self.not_ours} (content)")
        depth = [0]

        def start(*_: object) -> None:
            depth[0] += 1
            if depth[0] > MAX_DEPTH:
                refuse()

        def end(*_: object) -> None:
            depth[0] -= 1
        p = expat.ParserCreate()
        p.StartDoctypeDeclHandler = refuse
        p.EntityDeclHandler = refuse
        p.StartElementHandler = start
        p.EndElementHandler = end
        try:
            p.Parse(data, True)
        except (expat.ExpatError, ValueError):  # ValueError: a multi-byte encoding expat does not support
            raise StatementError(f"{self.not_ours} (XML)") from None

    def names(self) -> list[str | None]:
        return [name for name, _ in self.sheets]

    def path_of(self, rid: str | None, kind: str | None = None) -> str:
        targets = [t for typ, t in self.targets.get(rid, []) if kind is None or typ == kind]
        if len(targets) != 1 or not targets[0]:
            raise StatementError(f"{self.not_ours} (sheets)")
        target = targets[0] or ""
        return posixpath.normpath(target[1:] if target.startswith("/") else posixpath.join("xl", target))

    def shared_strings(self) -> list[str]:
        """The workbook's shared strings, read on the first cell that needs them."""
        if self.strings is None:
            rids = [rid for rid, ts in self.targets.items() if any(typ == SHARED_STRINGS for typ, _ in ts)]
            if len(rids) != 1:
                raise StatementError(f"{self.not_ours} (strings)")
            sst = self.part(self.path_of(rids[0], SHARED_STRINGS))
            # stripped once here: a copy per cell would let one long string fill the memory
            self.strings = ["".join(t.text or "" for t in si.iter(f"{MAIN}t")).strip()
                            for si in sst.findall(f"{MAIN}si")]
        return self.strings

    def grid(self, name: str) -> Grid:
        """{(row, column): ("s" | "n", text)} of the sheet `name`; strings stripped, an empty cell ("s", "")."""
        rids = [rid for n, rid in self.sheets if n == name]
        if len(rids) != 1:
            raise StatementError(f"{self.not_ours} (sheets)")
        sheet = self.part(self.path_of(rids[0]))
        grid: Grid = {}
        for c in sheet.iter(f"{MAIN}c"):
            m = CELL.fullmatch(c.get("r") or "")
            if not m:
                raise StatementError(f"{self.not_ours} (cell reference)")
            key = int(m.group(2)), m.group(1)
            if key in grid:
                raise StatementError(f"{self.not_ours} (cell reference)")
            kind = c.get("t", "n")
            v = c.find(f"{MAIN}v")
            value = (v.text or "").strip() if v is not None else ""
            if kind == "inlineStr":
                grid[key] = ("s", "".join(t.text or "" for t in c.iter(f"{MAIN}t")).strip())
            elif kind == "s" and self.shared:
                strings = self.shared_strings()
                if not (value.isascii() and value.isdigit()) or int(value) >= len(strings):
                    raise StatementError(f"{self.not_ours} (strings)")
                grid[key] = ("s", strings[int(value)])
            elif kind == "n":
                grid[key] = ("n", value) if value else ("s", "")  # a styled cell without a value is empty
            else:
                raise StatementError(f"Line {m.group(2)}: cell format \"{masked(kind)}\" is not supported yet")
        return grid


def text(grid: Grid, r: int, col: str) -> str | None:
    """The text of a cell, "" for an empty one, None for a number."""
    kind, value = grid.get((r, col), ("s", ""))
    return value if kind == "s" else None
