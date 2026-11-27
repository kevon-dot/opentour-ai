"""Minimal text-bearing PDF reader: page + passage citations in positional reading order.

Scope is deliberately narrow and honest. It reads classic cross-reference PDFs with simple
(single-byte) fonts and Flate or unfiltered content streams. Anything else is reported with a
specific state (object streams, composite fonts, other filters) instead of guessed. Encrypted
files report LOCKED. Image-only pages report NEEDS_RENDITION. Nothing is executed or fetched.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass

from opentour_cell.pilot.mixed_intake.flags import value_flags
from opentour_cell.pilot.mixed_intake.model import (
    Deadline,
    ExtractionState,
    Limits,
    NextActor,
    ReaderFailure,
    ReadResult,
    Span,
    finish,
)

PDF_REVISION = "opentour.reader.pdf-text@1.0.0"
_OBJ = re.compile(rb"(?<![0-9])(\d{1,7})\s+(\d{1,5})\s+obj\b")
_REF = rb"(\d+)\s+\d+\s+R"
_WS = b" \t\r\n\x0c\x00"
_DELIM = b"()<>[]/%"
_STD_ENCODINGS = {b"WinAnsiEncoding", b"MacRomanEncoding", b"StandardEncoding"}
_ESCAPES = {
    ord("n"): 10,
    ord("r"): 13,
    ord("t"): 9,
    ord("b"): 8,
    ord("f"): 12,
    ord("("): 40,
    ord(")"): 41,
    ord("\\"): 92,
}


@dataclass
class _Object:
    number: int
    head: bytes
    stream: bytes | None


def _balanced(text: bytes, start: int) -> bytes:
    """Return the `<< ... >>` dictionary text beginning at or after `start`."""
    begin = text.find(b"<<", start)
    if begin < 0:
        return b""
    depth, i = 0, begin
    while i < len(text) - 1:
        pair = text[i : i + 2]
        if pair == b"<<":
            depth += 1
            i += 2
        elif pair == b">>":
            depth -= 1
            i += 2
            if depth == 0:
                return text[begin:i]
        else:
            i += 1
    return text[begin:]


def _objects(data: bytes, deadline: Deadline) -> dict[int, _Object]:
    found: dict[int, _Object] = {}
    position = 0
    while match := _OBJ.search(data, position):
        deadline.check()
        number, body_start = int(match[1]), match.end()
        stream_at = data.find(b"stream", body_start)
        end_at = data.find(b"endobj", body_start)
        if end_at < 0:
            raise ReaderFailure(ExtractionState.CORRUPT, "unterminated_object", NextActor.OWNER)
        stream: bytes | None = None
        head_end = end_at
        if 0 <= stream_at < end_at:
            head = data[body_start:stream_at]
            raw_start = stream_at + len(b"stream")
            if data[raw_start : raw_start + 2] == b"\r\n":
                raw_start += 2
            elif data[raw_start : raw_start + 1] in (b"\n", b"\r"):
                raw_start += 1
            length = re.search(rb"/Length\s+(\d+)(?!\s+\d+\s+R)", head)
            raw_end = -1
            if length:
                candidate = raw_start + int(length[1])
                if data[candidate : candidate + 20].lstrip(_WS).startswith(b"endstream"):
                    raw_end = candidate
            if raw_end < 0:
                raw_end = data.find(b"endstream", raw_start)
                if raw_end < 0:
                    raise ReaderFailure(ExtractionState.CORRUPT, "bad_stream", NextActor.OWNER)
            stream = data[raw_start:raw_end]
            end_at = data.find(b"endobj", raw_end)
            if end_at < 0:
                raise ReaderFailure(ExtractionState.CORRUPT, "unterminated_object", NextActor.OWNER)
            head_end = stream_at
        found[number] = _Object(number, data[body_start:head_end], stream)
        position = end_at + len(b"endobj")
    return found


def _decode(obj: _Object, limits: Limits) -> bytes:
    raw = obj.stream or b""
    filters = re.findall(rb"/(\w+Decode|DCTDecode|JPXDecode|CCITTFaxDecode)", obj.head)
    if not filters:
        return raw
    if filters != [b"FlateDecode"]:
        raise ReaderFailure(ExtractionState.UNSUPPORTED, "pdf_stream_filter", NextActor.OWNER)
    inflater = zlib.decompressobj()
    try:
        out = inflater.decompress(raw, limits.max_object_bytes)
    except zlib.error as error:
        raise ReaderFailure(ExtractionState.CORRUPT, "bad_flate_stream", NextActor.OWNER) from error
    if inflater.unconsumed_tail:
        raise ReaderFailure(
            ExtractionState.OVERSIZED,
            "expanded_size_limit",
            NextActor.OWNER,
            limit="max_expanded_bytes",
        )
    return out


@dataclass
class _Fragment:
    x: float
    y: float
    text: str
    size: float
    font: bytes


def _tokens(data: bytes):  # type: ignore[no-untyped-def]
    i, n = 0, len(data)
    while i < n:
        c = data[i : i + 1]
        if c in _WS:
            i += 1
        elif c == b"%":
            while i < n and data[i] not in b"\r\n":
                i += 1
        elif c == b"(":
            depth, i, out = 1, i + 1, bytearray()
            while i < n and depth:
                b = data[i]
                if b == 0x5C and i + 1 < n:
                    nxt = data[i + 1]
                    if 0x30 <= nxt <= 0x37:
                        j = i + 1
                        while j < n and j < i + 4 and 0x30 <= data[j] <= 0x37:
                            j += 1
                        out.append(int(data[i + 1 : j], 8) & 0xFF)
                        i = j
                        continue
                    if nxt in (10, 13):
                        i += 2
                        continue
                    out.append(_ESCAPES.get(nxt, nxt))
                    i += 2
                    continue
                if b == 0x28:
                    depth += 1
                elif b == 0x29:
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                out.append(b)
                i += 1
            yield ("str", bytes(out))
        elif c == b"<" and data[i + 1 : i + 2] != b"<":
            end = data.find(b">", i)
            hexed = re.sub(rb"\s", b"", data[i + 1 : end])
            if len(hexed) % 2:
                hexed += b"0"
            yield ("str", bytes.fromhex(hexed.decode("ascii", "ignore")) if hexed else b"")
            i = end + 1
        elif c in (b"[", b"]"):
            yield (c.decode(), None)
            i += 1
        elif c in (b"<", b">"):
            i += 2
            yield ("op", b"<<" if c == b"<" else b">>")
        elif c == b"/":
            j = i + 1
            while j < n and data[j] not in _WS and data[j] not in _DELIM:
                j += 1
            yield ("name", data[i + 1 : j])
            i = j
        else:
            j = i
            while j < n and data[j] not in _WS and data[j] not in _DELIM:
                j += 1
            if j == i:
                i += 1
                continue
            word = data[i:j]
            try:
                yield ("num", float(word))
            except ValueError:
                yield ("op", word)
            i = j


def _show(raw: bytes) -> str:
    return raw.decode("cp1252", "replace")


def _fragments(content: bytes, deadline: Deadline) -> list[_Fragment]:
    out: list[_Fragment] = []
    stack: list[object] = []
    lx = ly = x = y = leading = size = 0.0
    font = b""
    array: list[object] | None = None

    def emit(text: str) -> None:
        if text.strip():
            out.append(_Fragment(x, y, text, size, font))

    for kind, value in _tokens(content):
        deadline.check()
        if kind == "[":
            array = []
            continue
        if kind == "]":
            if array is not None:
                stack.append(array)
            array = None
            continue
        if array is not None:
            array.append(value)
            continue
        if kind != "op":
            stack.append(value)
            continue
        op = value
        nums = [v for v in stack if isinstance(v, float)]
        if op == b"BT":
            lx = ly = x = y = 0.0
        elif op == b"Tf" and stack:
            size = nums[-1] if nums else size
            names = [v for v in stack if isinstance(v, bytes) and not isinstance(v, bool)]
            font = names[0] if names else font
        elif op in (b"Td", b"TD") and len(nums) >= 2:
            lx, ly = lx + nums[-2], ly + nums[-1]
            x, y = lx, ly
            if op == b"TD":
                leading = -nums[-1]
        elif op == b"Tm" and len(nums) >= 6:
            lx, ly = nums[-2], nums[-1]
            x, y = lx, ly
        elif op == b"TL" and nums:
            leading = nums[-1]
        elif op == b"T*":
            ly -= leading
            x, y = lx, ly
        elif op == b"Tj" and stack and isinstance(stack[-1], bytes):
            emit(_show(stack[-1]))
        elif op in (b"'", b'"') and stack and isinstance(stack[-1], bytes):
            ly -= leading
            x, y = lx, ly
            emit(_show(stack[-1]))
        elif op == b"TJ" and stack and isinstance(stack[-1], list):
            parts = []
            for item in stack[-1]:
                if isinstance(item, bytes):
                    parts.append(_show(item))
                elif isinstance(item, float) and item < -200:
                    parts.append(" ")
            emit("".join(parts))
        stack.clear()
    return out


def _font_ok(fonts: dict[bytes, _Object], name: bytes) -> bool:
    obj = fonts.get(name)
    if obj is None:
        return True
    head = obj.head
    if re.search(rb"/Subtype\s*/Type0", head):
        return False
    encoding = re.search(rb"/Encoding\s*/(\w+)", head)
    if re.search(rb"/Differences", head) or re.search(rb"/ToUnicode", head):
        return False
    if encoding and encoding[1] not in _STD_ENCODINGS:
        return False
    return True


def _lines(fragments: list[_Fragment]) -> list[list[_Fragment]]:
    rows: list[list[_Fragment]] = []
    for fragment in sorted(fragments, key=lambda f: (-f.y, f.x)):
        if rows and abs(rows[-1][0].y - fragment.y) <= 2.0:
            rows[-1].append(fragment)
        else:
            rows.append([fragment])
    merged: list[list[_Fragment]] = []
    for row in rows:
        row.sort(key=lambda f: f.x)
        cells: list[_Fragment] = []
        for fragment in row:
            if cells:
                prev = cells[-1]
                end = prev.x + len(prev.text) * prev.size * 0.5
                if fragment.x - end < fragment.size * 0.6:
                    joiner = "" if prev.text.endswith(" ") else " "
                    cells[-1] = _Fragment(
                        prev.x, prev.y, prev.text + joiner + fragment.text, prev.size, prev.font
                    )
                    continue
            cells.append(fragment)
        merged.append(cells)
    return merged


def read_pdf(data: bytes, limits: Limits, deadline: Deadline) -> ReadResult:
    """Return page/passage spans in reading order, or one specific recoverable state."""
    objects = _objects(data, deadline)
    if not objects:
        raise ReaderFailure(ExtractionState.CORRUPT, "no_pdf_objects", NextActor.OWNER)
    trailer = data[data.rfind(b"trailer") :] if b"trailer" in data else b""
    if re.search(rb"/Encrypt\b", trailer) or any(
        re.search(rb"/Encrypt\b", o.head) for o in objects.values()
    ):
        raise ReaderFailure(ExtractionState.LOCKED, "encrypted_pdf", NextActor.OWNER)
    catalog = next((o for o in objects.values() if re.search(rb"/Type\s*/Catalog", o.head)), None)
    if catalog is None:
        reason = "pdf_object_streams" if b"/ObjStm" in data else "no_catalog"
        state = ExtractionState.UNSUPPORTED if b"/ObjStm" in data else ExtractionState.CORRUPT
        raise ReaderFailure(state, reason, NextActor.OWNER)
    root = re.search(rb"/Pages\s+" + _REF, catalog.head)
    if root is None:
        raise ReaderFailure(ExtractionState.CORRUPT, "no_page_tree", NextActor.OWNER)

    pages: list[tuple[_Object, bytes]] = []
    seen: set[int] = set()

    def walk(number: int, inherited: bytes, depth: int) -> None:
        if number in seen or number not in objects:
            raise ReaderFailure(ExtractionState.CORRUPT, "bad_page_tree", NextActor.OWNER)
        if depth > limits.max_xml_depth:
            raise ReaderFailure(
                ExtractionState.OVER_DEPTH, "page_tree_depth", NextActor.OWNER, limit="max_depth"
            )
        seen.add(number)
        obj = objects[number]
        resources = _resources(obj.head, objects) or inherited
        if re.search(rb"/Type\s*/Pages\b", obj.head):
            kids = re.search(rb"/Kids\s*\[(.*?)\]", obj.head, re.S)
            for kid in re.findall(_REF, kids[1] if kids else b""):
                walk(int(kid), resources, depth + 1)
        else:
            pages.append((obj, resources))
            if len(pages) > limits.max_pages:
                raise ReaderFailure(
                    ExtractionState.OVERSIZED, "page_limit", NextActor.OWNER, limit="pages"
                )

    walk(int(root[1]), b"", 0)
    spans: list[Span] = []
    image_pages: list[int] = []
    reading_order = 0
    for page_number, (page, resources) in enumerate(pages, start=1):
        deadline.check()
        fonts = _named_refs(resources, b"Font", objects)
        images = [
            name
            for name, obj in _named_refs(resources, b"XObject", objects).items()
            if re.search(rb"/Subtype\s*/Image", obj.head)
        ]
        contents = re.search(rb"/Contents\s*(\[(.*?)\]|" + _REF + rb")", page.head, re.S)
        refs = re.findall(_REF, contents[0]) if contents else []
        stream = b"\n".join(_decode(objects[int(r)], limits) for r in refs if int(r) in objects)
        fragments = _fragments(stream, deadline)
        if not fragments:
            if images:
                image_pages.append(page_number)
            continue
        flagged_font = any(not _font_ok(fonts, f.font) for f in fragments)
        passages = _passages(_lines(fragments))
        for index, lines in enumerate(passages, start=1):
            reading_order += 1
            cells = [[c.text for c in line] for line in lines]
            table = len(cells) >= 2 and all(len(row) >= 2 for row in cells)
            flags: list[str] = []
            if flagged_font:
                flags.append("font_encoding_unverified")
            if table and len({len(row) for row in cells}) != 1:
                flags.append("column_layout_ambiguous")
            for row in cells:
                for cell in row:
                    flags.extend(value_flags(cell.strip()))
            spans.append(
                Span(
                    {
                        "kind": "pdf",
                        "page": page_number,
                        "passage": index,
                        "reading_order": reading_order,
                        "lines": [1, len(lines)],
                    },
                    "\n".join("\t".join(row) for row in cells),
                    {"cells": cells, "table": table, "header": cells[0] if table else None},
                    tuple(dict.fromkeys(flags)),
                )
            )
    if not spans:
        if image_pages:
            return ReadResult(
                ExtractionState.NEEDS_RENDITION,
                (),
                "image_only_pdf_needs_authorized_ocr_rendition",
                NextActor.OWNER,
            )
        raise ReaderFailure(ExtractionState.CORRUPT, "no_extractable_text", NextActor.OWNER)
    notes: list[str] = []
    if image_pages:
        notes.append("image_only_pages:" + ",".join(map(str, image_pages)))
        spans.append(
            Span(
                {"kind": "pdf", "pages": image_pages},
                "",
                None,
                ("pages_need_rendition",),
                False,
            )
        )
    return finish(spans, notes)


def _passages(lines: list[list[_Fragment]]) -> list[list[list[_Fragment]]]:
    passages: list[list[list[_Fragment]]] = []
    for line in lines:
        if passages:
            previous = passages[-1][-1]
            gap = previous[0].y - line[0].y
            if gap <= max(line[0].size, 1.0) * 2.0:
                passages[-1].append(line)
                continue
        passages.append([line])
    return passages


def _resources(head: bytes, objects: dict[int, _Object]) -> bytes:
    ref = re.search(rb"/Resources\s+" + _REF, head)
    if ref and int(ref[1]) in objects:
        return objects[int(ref[1])].head
    at = head.find(b"/Resources")
    return _balanced(head, at) if at >= 0 else b""


def _named_refs(resources: bytes, key: bytes, objects: dict[int, _Object]) -> dict[bytes, _Object]:
    at = resources.find(b"/" + key)
    if at < 0:
        return {}
    ref = re.match(rb"/" + key + rb"\s+" + _REF, resources[at:])
    table = (
        objects[int(ref[1])].head if ref and int(ref[1]) in objects else _balanced(resources, at)
    )
    return {
        name: objects[int(num)]
        for name, num in re.findall(rb"/([\w.+-]+)\s+" + _REF, table)
        if int(num) in objects
    }
