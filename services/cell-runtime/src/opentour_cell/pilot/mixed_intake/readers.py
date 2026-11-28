"""Deterministic, standard-library fixture readers. No model, OCR or network is ever needed.

Embedded links, formulas, macros and instructions are treated strictly as data: nothing here
evaluates a formula, follows a relationship target, opens an embedded object or runs a macro.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from xml.etree import ElementTree
from xml.parsers import expat

from opentour_cell.pilot.mixed_intake import pdf
from opentour_cell.pilot.mixed_intake.flags import value_flags
from opentour_cell.pilot.mixed_intake.model import (
    Deadline,
    ExtractionState,
    Limits,
    NextActor,
    ReaderFailure,
    ReadResult,
    Span,
    failure_result,
    finish,
)

READER_REVISIONS = {
    "text": "opentour.reader.text@1.0.0",
    "markdown": "opentour.reader.text@1.0.0",
    "csv": "opentour.reader.csv@1.0.0",
    "jsonl": "opentour.reader.jsonl@1.0.0",
    "docx": "opentour.reader.docx@1.0.1",
    "xlsx": "opentour.reader.xlsx@1.0.2",
    "pdf": pdf.PDF_REVISION,
    "ocr-text": "opentour.reader.ocr-rendition@1.0.0",
    "native-markdown": "opentour.reader.text@1.0.0",
}
TEXT_EXTENSIONS = {"md": "markdown", "markdown": "markdown", "txt": "text"}
ZIP_MAGIC = b"PK\x03\x04"
CFB_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def extension(name: str | None) -> str:
    if not name or "." not in name:
        return ""
    return name.rsplit(".", 1)[1].lower()


def sniff(data: bytes, name: str | None) -> str:
    """Classify by content first; the name is only a hint inside the text family."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(CFB_MAGIC):
        return "cfb"
    if data.startswith(ZIP_MAGIC) or data.startswith(b"PK\x05\x06"):
        return "zip"
    ext = extension(name)
    if ext in TEXT_EXTENSIONS or ext in {"csv", "jsonl", "ndjson"}:
        return {"ndjson": "jsonl"}.get(ext, TEXT_EXTENSIONS.get(ext, ext))
    return "binary"


def decode_utf8(data: bytes) -> str:
    if b"\x00" in data:
        raise ReaderFailure(ExtractionState.CORRUPT, "not_utf8_text", NextActor.OWNER)
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ReaderFailure(ExtractionState.CORRUPT, "invalid_utf8", NextActor.OWNER) from error


# --------------------------------------------------------------------------- text / markdown


def read_text(data: bytes, limits: Limits, deadline: Deadline) -> ReadResult:
    """UTF-8 text or Markdown. One span per blank-line separated block, cited by line range."""
    text = decode_utf8(data)
    spans: list[Span] = []
    lines = text.splitlines()
    if len(lines) > limits.max_rows:
        raise ReaderFailure(ExtractionState.OVERSIZED, "line_limit", NextActor.OWNER, limit="rows")
    start: int | None = None
    block: list[str] = []
    for number, line in enumerate([*lines, ""], start=1):
        deadline.check()
        if line.strip():
            if start is None:
                start = number
            block.append(line)
        elif start is not None:
            spans.append(
                Span({"kind": "lines", "start": start, "end": number - 1}, "\n".join(block))
            )
            start, block = None, []
    return finish(spans, [])


# --------------------------------------------------------------------------- csv / jsonl


def read_csv(data: bytes, limits: Limits, deadline: Deadline) -> ReadResult:
    """Header + row citations. Every cell stays the exact string that was in the file."""
    text = decode_utf8(data)
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        return ReadResult(ExtractionState.CORRUPT, reason="empty_csv", next_actor=NextActor.OWNER)
    except csv.Error as error:
        raise ReaderFailure(ExtractionState.CORRUPT, "csv_syntax", NextActor.OWNER) from error
    header_flags = ("duplicate_header",) if len(set(header)) != len(header) else ()
    spans: list[Span] = []
    try:
        for row_number, row in enumerate(reader, start=1):
            deadline.check()
            if row_number > limits.max_rows:
                raise ReaderFailure(
                    ExtractionState.OVERSIZED, "row_limit", NextActor.OWNER, limit="rows"
                )
            flags = list(header_flags)
            if len(row) != len(header):
                flags.append("ragged_row")
            cells = dict(zip(header, row, strict=False)) if not header_flags else None
            for cell in row:
                flags.extend(value_flags(cell))
            spans.append(
                Span(
                    {"kind": "csv", "row": row_number, "header": header},
                    "; ".join(f"{h}={v}" for h, v in zip(header, row, strict=False)),
                    {"cells": cells, "row": row},
                    tuple(dict.fromkeys(flags)),
                )
            )
    except csv.Error as error:
        raise ReaderFailure(ExtractionState.CORRUPT, "csv_syntax", NextActor.OWNER) from error
    return finish(spans, [])


class _Raw(str):
    """A JSON number kept as its literal token."""


def _depth(value: Any, limit: int) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            raise ReaderFailure(
                ExtractionState.OVER_DEPTH, "json_depth", NextActor.OWNER, limit="max_json_depth"
            )
        children = (
            item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
        )
        stack.extend((child, depth + 1) for child in children)


def _flatten(value: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key in value:
            yield from _flatten(value[key], f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _flatten(item, f"{path}[{index}]")
    elif value is None:
        yield path, "null"
    elif isinstance(value, bool):
        yield path, "true" if value else "false"
    else:
        yield path, str(value)


def read_jsonl(data: bytes, limits: Limits, deadline: Deadline) -> ReadResult:
    """One JSON object per line, cited by row and path. Number tokens are kept literally."""
    text = decode_utf8(data)
    spans: list[Span] = []
    for row, line in enumerate(text.splitlines(), start=1):
        deadline.check()
        if not line.strip():
            continue
        if len(spans) >= limits.max_rows:
            raise ReaderFailure(
                ExtractionState.OVERSIZED, "row_limit", NextActor.OWNER, limit="rows"
            )
        try:
            value = json.loads(line, parse_float=_Raw, parse_int=_Raw)
        except RecursionError as error:
            raise ReaderFailure(
                ExtractionState.OVER_DEPTH, "json_depth", NextActor.OWNER, limit="max_json_depth"
            ) from error
        except ValueError as error:
            raise ReaderFailure(ExtractionState.CORRUPT, "jsonl_syntax", NextActor.OWNER) from error
        _depth(value, limits.max_json_depth)
        flat = dict(_flatten(value))
        flags: list[str] = []
        for item in flat.values():
            flags.extend(value_flags(item))
        spans.append(
            Span(
                {"kind": "jsonl", "row": row, "paths": sorted(flat)},
                "; ".join(f"{k}={v}" for k, v in flat.items()),
                {"values": flat},
                tuple(dict.fromkeys(flags)),
            )
        )
    return finish(spans, [])


# --------------------------------------------------------------------------- OOXML packages


def _nested_depth(data: bytes, depth: int, limits: Limits, budget: list[int]) -> int:
    """Deepest nesting of archives inside archives, bounded without extracting to disk."""
    if not data.startswith(ZIP_MAGIC):
        return depth - 1
    if depth > limits.max_archive_depth:
        return depth
    deepest = depth
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as nested:
            for info in nested.infolist():
                if info.is_dir() or info.file_size == 0:
                    continue
                head = nested.open(info).read(4)
                if head == ZIP_MAGIC:
                    budget[0] -= info.file_size
                    if budget[0] < 0:
                        raise ReaderFailure(
                            ExtractionState.OVERSIZED,
                            "expanded_size_limit",
                            NextActor.OWNER,
                            limit="max_expanded_bytes",
                        )
                    inner = nested.open(info).read(limits.max_object_bytes + 1)
                    deepest = max(deepest, _nested_depth(inner, depth + 1, limits, budget))
    except zipfile.BadZipFile:
        return depth
    return deepest


class Package:
    """A size-, entry-, path- and depth-guarded view of an OOXML zip."""

    def __init__(self, data: bytes, limits: Limits, deadline: Deadline):
        self.limits, self.deadline = limits, deadline
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
            infos = self.zip.infolist()
        except (zipfile.BadZipFile, OSError, ValueError) as error:
            raise ReaderFailure(ExtractionState.CORRUPT, "bad_archive", NextActor.OWNER) from error
        if len(infos) > limits.max_archive_entries:
            raise ReaderFailure(
                ExtractionState.OVERSIZED,
                "archive_entry_limit",
                NextActor.OWNER,
                limit="max_archive_entries",
            )
        total = 0
        for info in infos:
            name = info.filename
            parts = name.replace("\\", "/").split("/")
            if name.startswith(("/", "\\")) or ".." in parts or ":" in parts[0]:
                raise ReaderFailure(ExtractionState.CORRUPT, "unsafe_archive_path", NextActor.OWNER)
            if info.flag_bits & 0x1:
                raise ReaderFailure(ExtractionState.LOCKED, "encrypted_archive", NextActor.OWNER)
            total += info.file_size
            if info.file_size > limits.max_object_bytes or total > limits.max_expanded_bytes:
                raise ReaderFailure(
                    ExtractionState.OVERSIZED,
                    "expanded_size_limit",
                    NextActor.OWNER,
                    limit="max_expanded_bytes",
                )
            if info.compress_size and info.file_size / info.compress_size > 1000:
                raise ReaderFailure(
                    ExtractionState.OVERSIZED,
                    "compression_ratio_limit",
                    NextActor.OWNER,
                    limit="max_expanded_bytes",
                )
        self.names = {info.filename for info in infos}
        budget = [limits.max_expanded_bytes]
        if _nested_depth(data, 1, limits, budget) > limits.max_archive_depth:
            raise ReaderFailure(
                ExtractionState.OVER_DEPTH,
                "archive_depth",
                NextActor.OWNER,
                limit="max_archive_depth",
            )
        self.notes: list[str] = []
        if any(name.lower().endswith("vbaproject.bin") for name in self.names):
            self.notes.append("macros_present_not_executed")
        if any(
            name.lower().startswith(("word/embeddings/", "xl/embeddings/")) for name in self.names
        ):
            self.notes.append("embedded_objects_not_opened")

    def read(self, name: str) -> bytes:
        if name not in self.names:
            raise ReaderFailure(ExtractionState.CORRUPT, f"missing_part:{name}", NextActor.OWNER)
        self.deadline.check()
        with self.zip.open(name) as handle:
            payload = handle.read(self.limits.max_object_bytes + 1)
        if len(payload) > self.limits.max_object_bytes:
            raise ReaderFailure(
                ExtractionState.OVERSIZED,
                "expanded_size_limit",
                NextActor.OWNER,
                limit="max_expanded_bytes",
            )
        return payload

    def xml(self, name: str) -> ElementTree.Element:
        return parse_xml(self.read(name), self.limits)


def parse_xml(payload: bytes, limits: Limits) -> ElementTree.Element:
    """Parse XML with entity/DTD declarations refused and nesting bounded."""
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ReaderFailure(ExtractionState.CORRUPT, "xml_dtd_refused", NextActor.OWNER)

    def refuse(*_args: object) -> None:
        raise ReaderFailure(ExtractionState.CORRUPT, "xml_dtd_refused", NextActor.OWNER)

    # The byte scan above only sees ASCII-compatible encodings (a UTF-16 payload would slip
    # past it), so the expat parser itself also refuses any DTD or entity declaration, whatever
    # the encoding. Depth is bounded while parsing, before a deep tree can be built.
    builder = ElementTree.TreeBuilder()
    depth = 0

    def qname(name: str) -> str:
        uri, _, local = name.rpartition(" ")
        return f"{{{uri}}}{local}" if uri else name

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal depth
        depth += 1
        if depth > limits.max_xml_depth:
            raise ReaderFailure(
                ExtractionState.OVER_DEPTH, "xml_depth", NextActor.OWNER, limit="max_xml_depth"
            )
        builder.start(qname(name), {qname(k): v for k, v in attrs.items()})

    def end(name: str) -> None:
        nonlocal depth
        depth -= 1
        builder.end(qname(name))

    parser = expat.ParserCreate(namespace_separator=" ")
    parser.buffer_text = True
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(payload, True)
        return builder.close()
    except (expat.ExpatError, RecursionError) as error:
        raise ReaderFailure(ExtractionState.CORRUPT, "bad_xml", NextActor.OWNER) from error


def _is_encrypted_container(data: bytes) -> bool:
    return data.startswith(CFB_MAGIC) and (
        "EncryptedPackage".encode("utf-16-le") in data or b"EncryptionInfo" in data
    )


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    out: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{W}t" and node.text:
            out.append(node.text)
        elif node.tag == f"{W}tab":
            out.append("\t")
        elif node.tag in (f"{W}br", f"{W}cr"):
            out.append("\n")
    return "".join(out)


def read_docx(data: bytes, limits: Limits, deadline: Deadline) -> ReadResult:
    """Paragraph and table-cell coordinates. Field codes and hyperlink targets are never used."""
    package = Package(data, limits, deadline)
    if "word/document.xml" not in package.names:
        raise ReaderFailure(ExtractionState.UNSUPPORTED, "not_wordprocessing", NextActor.OWNER)
    body = package.xml("word/document.xml").find(f"{W}body")
    if body is None:
        raise ReaderFailure(ExtractionState.CORRUPT, "no_document_body", NextActor.OWNER)
    spans: list[Span] = []
    paragraph_index = table_index = 0
    for block in body:
        deadline.check()
        if block.tag == f"{W}p":
            paragraph_index += 1
            text = _paragraph_text(block)
            if not text.strip():
                continue
            flags: tuple[str, ...] = (
                ("tracked_changes",) if block.find(f".//{W}del") is not None else ()
            )
            flags += tuple(flag for flag in value_flags(text.strip()))
            spans.append(Span({"kind": "docx", "paragraph": paragraph_index}, text, None, flags))
        elif block.tag == f"{W}tbl":
            table_index += 1
            rows = block.findall(f"{W}tr")
            if len(rows) > limits.max_rows:
                raise ReaderFailure(
                    ExtractionState.OVERSIZED, "row_limit", NextActor.OWNER, limit="rows"
                )
            header: list[str] = []
            for row_number, row in enumerate(rows, start=1):
                cells = row.findall(f"{W}tc")
                texts = ["\n".join(_paragraph_text(p) for p in c.findall(f"{W}p")) for c in cells]
                if row_number == 1:
                    header = texts
                for col, (cell, text) in enumerate(zip(cells, texts, strict=True), start=1):
                    flags_list: list[str] = []
                    props = cell.find(f"{W}tcPr")
                    if props is not None and (
                        props.find(f"{W}gridSpan") is not None
                        or props.find(f"{W}vMerge") is not None
                    ):
                        flags_list.append("merged_cell")
                    flags_list += value_flags(text.strip())
                    spans.append(
                        Span(
                            {
                                "kind": "docx",
                                "table": table_index,
                                "row": row_number,
                                "col": col,
                                "header": header[col - 1] if col - 1 < len(header) else None,
                            },
                            text,
                            None,
                            tuple(dict.fromkeys(flags_list)),
                        )
                    )
    result = finish(spans, package.notes)
    return result


def _column_number(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref)
    assert letters is not None
    number = 0
    for char in letters[0]:
        number = number * 26 + ord(char) - 64
    return number


_BUILTIN_DATE_FORMATS = set(range(14, 23)) | {27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 45, 46, 47}


def _is_date_format(code: str) -> bool:
    cleaned = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code.lower())
    return bool(re.search(r"[ymdhs]", cleaned))


def _serial_to_iso(serial: float, date1904: bool) -> str:
    base = datetime(1904, 1, 1, tzinfo=UTC) if date1904 else datetime(1899, 12, 30, tzinfo=UTC)
    moment = base + timedelta(days=serial)
    if serial == int(serial):
        return moment.date().isoformat()
    return moment.replace(tzinfo=None).isoformat(timespec="seconds")


def _shared_strings(package: Package) -> list[str]:
    if "xl/sharedStrings.xml" not in package.names:
        return []
    root = package.xml("xl/sharedStrings.xml")
    return ["".join(t.text or "" for t in si.iter(f"{S}t")) for si in root.findall(f"{S}si")]


def _date_styles(package: Package) -> set[int]:
    if "xl/styles.xml" not in package.names:
        return set()
    root = package.xml("xl/styles.xml")
    custom = {
        int(fmt.get("numFmtId", "-1")): fmt.get("formatCode", "") for fmt in root.iter(f"{S}numFmt")
    }
    cell_xfs = root.find(f"{S}cellXfs")
    styles: set[int] = set()
    if cell_xfs is not None:
        for index, xf in enumerate(cell_xfs.findall(f"{S}xf")):
            fmt_id = int(xf.get("numFmtId", "0"))
            if fmt_id in _BUILTIN_DATE_FORMATS or (
                fmt_id in custom and _is_date_format(custom[fmt_id])
            ):
                styles.add(index)
    return styles


def read_xlsx(
    data: bytes, limits: Limits, deadline: Deadline, *, attested_formulas: bool = False
) -> ReadResult:
    """Worksheet + cell citations of literal values. Formulas are never evaluated.

    A formula cell is unavailable unless the owner attested its cached snapshot value.
    """
    package = Package(data, limits, deadline)
    if "xl/workbook.xml" not in package.names:
        raise ReaderFailure(ExtractionState.UNSUPPORTED, "not_spreadsheet", NextActor.OWNER)
    workbook = package.xml("xl/workbook.xml")
    date1904 = (props := workbook.find(f"{S}workbookPr")) is not None and props.get(
        "date1904", "0"
    ) in ("1", "true")
    rels = {
        rel.get("Id"): rel.get("Target", "")
        for rel in package.xml("xl/_rels/workbook.xml.rels").findall(f"{PKG_REL}Relationship")
    }
    strings = _shared_strings(package)
    date_styles = _date_styles(package)
    spans: list[Span] = []
    sheets = workbook.find(f"{S}sheets")
    if sheets is None:
        raise ReaderFailure(ExtractionState.CORRUPT, "no_sheets", NextActor.OWNER)
    rows_seen = 0
    for sheet in sheets.findall(f"{S}sheet"):
        name = sheet.get("name", "")
        target = rels.get(sheet.get(f"{R}id"), "")
        path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
        for row in package.xml(path).iter(f"{S}row"):
            deadline.check()
            rows_seen += 1
            if rows_seen > limits.max_rows:
                raise ReaderFailure(
                    ExtractionState.OVERSIZED, "row_limit", NextActor.OWNER, limit="rows"
                )
            for cell in sorted(row.findall(f"{S}c"), key=lambda c: _column_number(c.get("r", "A"))):
                ref = cell.get("r", "")
                kind = cell.get("t", "n")
                raw_node = cell.find(f"{S}v")
                raw = raw_node.text if raw_node is not None and raw_node.text is not None else None
                locator = {"kind": "xlsx", "sheet": name, "cell": ref}
                if cell.find(f"{S}f") is not None:
                    if attested_formulas and raw is not None:
                        spans.append(
                            Span(locator, raw, {"value": raw, "snapshot": "owner_attested"}, ())
                        )
                    else:
                        spans.append(
                            Span(locator, "", {"formula": True}, ("formula_unavailable",), False)
                        )
                    continue
                if kind == "inlineStr":
                    text = "".join(t.text or "" for t in cell.iter(f"{S}t"))
                    spans.append(Span(locator, text, {"value": text, "type": "string"}, ()))
                    continue
                if raw is None:
                    continue
                if kind == "s":
                    index = int(raw)
                    if not 0 <= index < len(strings):
                        # A negative index would silently select the wrong string from the end.
                        raise ReaderFailure(
                            ExtractionState.CORRUPT, "malformed_content", NextActor.OWNER
                        )
                    value = strings[index]
                    spans.append(
                        Span(
                            locator,
                            value,
                            {"value": value, "type": "string"},
                            value_flags(value),
                        )
                    )
                elif kind == "b":
                    spans.append(
                        Span(locator, "true" if raw == "1" else "false", {"value": raw}, ())
                    )
                elif kind == "e":
                    spans.append(Span(locator, raw, {"value": raw, "type": "error"}, ()))
                elif kind == "str":
                    spans.append(Span(locator, raw, {"value": raw, "type": "string"}, ()))
                else:
                    info: dict[str, Any] = {"value": raw, "type": "number"}
                    text = raw
                    if int(cell.get("s", "0")) in date_styles:
                        iso = _serial_to_iso(float(raw), date1904)
                        info.update(type="date", iso=iso, serial=raw)
                        text = iso
                    spans.append(Span(locator, text, info, ()))
    return finish(spans, package.notes)


# --------------------------------------------------------------------------- dispatch


def read_object(
    data: bytes,
    name: str | None,
    limits: Limits,
    deadline: Deadline,
    *,
    attested_formulas: bool = False,
) -> tuple[str, ReadResult]:
    """Return (format, result). Never raises for a classifiable object-level failure."""
    kind = "unknown"
    try:
        if len(data) > limits.max_object_bytes:
            raise ReaderFailure(
                ExtractionState.OVERSIZED, "object_byte_limit", NextActor.OWNER, limit="bytes"
            )
        kind = sniff(data, name)
        if kind == "cfb":
            if _is_encrypted_container(data):
                raise ReaderFailure(ExtractionState.LOCKED, "encrypted_file", NextActor.OWNER)
            raise ReaderFailure(
                ExtractionState.UNSUPPORTED, "legacy_binary_office", NextActor.OWNER
            )
        if kind == "binary":
            raise ReaderFailure(ExtractionState.UNSUPPORTED, "unsupported_type", NextActor.OWNER)
        if kind == "pdf":
            return kind, pdf.read_pdf(data, limits, deadline)
        if kind == "zip":
            return _read_ooxml(data, limits, deadline, attested_formulas)
        if kind in ("text", "markdown"):
            return kind, read_text(data, limits, deadline)
        if kind == "csv":
            return kind, read_csv(data, limits, deadline)
        return kind, read_jsonl(data, limits, deadline)
    except ReaderFailure as error:
        return kind, failure_result(error)
    except Exception:  # noqa: BLE001
        # Attacker-shaped content must never abort a scan: any parser surprise (bad index,
        # non-numeric cell, overflow, unexpected structure) is one recoverable CORRUPT object.
        return kind, failure_result(
            ReaderFailure(ExtractionState.CORRUPT, "malformed_content", NextActor.OWNER)
        )


def _read_ooxml(
    data: bytes, limits: Limits, deadline: Deadline, attested_formulas: bool
) -> tuple[str, ReadResult]:
    try:
        package = Package(data, limits, deadline)
    except ReaderFailure as error:
        return "zip", failure_result(error)
    if "word/document.xml" in package.names:
        return "docx", read_docx(data, limits, deadline)
    if "xl/workbook.xml" in package.names:
        return "xlsx", read_xlsx(data, limits, deadline, attested_formulas=attested_formulas)
    raise ReaderFailure(ExtractionState.UNSUPPORTED, "unsupported_archive", NextActor.OWNER)
