import io
import struct
import unittest
import zipfile
from unittest import mock

from tests.test_debitum import xlsx as debitum_xlsx
from wfsync.sources import debitum as d
from wfsync.sources import statement, xlsx


def rewrite(raw, name, data=None, method=None, declared=None):
    """`raw` with member `name` replaced: new `data`, another compression `method`, or a central
    directory that declares `declared` as its unpacked size (an attacker's own archive)."""
    src = zipfile.ZipFile(io.BytesIO(raw))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            body = src.read(info.filename)
            if info.filename == name:
                body = data if data is not None else body
                z.writestr(info.filename, body, compress_type=method or zipfile.ZIP_DEFLATED)
            else:
                z.writestr(info.filename, body)
    out = bytearray(buf.getvalue())
    if declared is not None:
        start = 0
        while (i := out.find(b"PK\x01\x02", start)) >= 0:
            n = struct.unpack("<H", out[i + 28:i + 30])[0]
            if out[i + 46:i + 46 + n].decode() == name:
                out[i + 24:i + 28] = struct.pack("<I", declared)
            start = i + 4
        i = 0
        while (i := out.find(b"PK\x03\x04", i)) >= 0:  # local header too
            n = struct.unpack("<H", out[i + 26:i + 28])[0]
            if out[i + 30:i + 30 + n].decode() == name:
                out[i + 22:i + 26] = struct.pack("<I", declared)
            i += 4
    return bytes(out)


SHEET = "xl/worksheets/sheet2.xml"


class Limits(unittest.TestCase):
    def test_unmodified_file_is_read(self):
        self.assertEqual(len(d.parse(rewrite(debitum_xlsx(), SHEET))[0]), 4)

    def test_understated_size_keeps_the_read_limit(self):
        # 8 MiB of spaces declared as 100 bytes: read at most MAX_PART + 1, then refused
        bomb = b"<worksheet>" + b" " * (8 * 1024 * 1024) + b"</worksheet>"
        raw = rewrite(debitum_xlsx(), SHEET, data=bomb, declared=100)
        with mock.patch.object(xlsx, "MAX_PART", 1024 * 1024), \
             self.assertRaisesRegex(statement.StatementError, r"\(too large\)|\(archive damaged\)"):
            d.parse(raw)
        # read() must have been bounded: the limit is passed to it
        with mock.patch.object(zipfile.ZipExtFile, "read", autospec=True,
                               side_effect=lambda self, n=-1: b"x" * 10) as read:
            with self.assertRaises(statement.StatementError):
                d.parse(debitum_xlsx())
            self.assertTrue(all(c.args[1] == xlsx.MAX_PART + 1 for c in read.call_args_list))

    def test_bzip2_and_lzma_are_refused_unread(self):
        for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            with self.assertRaisesRegex(statement.StatementError, r"\(compression\)"):
                d.parse(rewrite(debitum_xlsx(), SHEET, method=method))
        d.parse(rewrite(debitum_xlsx(), SHEET, method=zipfile.ZIP_STORED))  # stored is fine

    def test_deep_nesting_is_refused(self):
        deep = b"<a>" * (xlsx.MAX_DEPTH + 1) + b"</a>" * (xlsx.MAX_DEPTH + 1)
        with self.assertRaisesRegex(statement.StatementError, r"\(content\)"):
            d.parse(rewrite(debitum_xlsx(), SHEET, data=deep))
        ok = b"<a>" * xlsx.MAX_DEPTH + b"</a>" * xlsx.MAX_DEPTH  # at the limit: parsed, then not ours
        with self.assertRaisesRegex(statement.StatementError, r"\(unknown header\)"):
            d.parse(rewrite(debitum_xlsx(), SHEET, data=ok))

    def test_shared_strings_are_stripped_once(self):
        raw = debitum_xlsx()
        with xlsx.Workbook(raw, "x") as wb:
            grid = wb.grid("Detailed Balance")
            eur = [v for (r, c), (k, v) in grid.items() if c == "T" and r > 1 and v]
            self.assertTrue(len(eur) > 1 and all(v is eur[0] for v in eur))
