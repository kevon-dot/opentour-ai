"""Minimal text-bearing PDF reader: page + passage citations in positional reading order.

Scope is deliberately narrow and honest. It reads classic cross-reference PDFs with simple
(single-byte) fonts and Flate or unfiltered content streams. Anything else is reported with a
specific state (object streams, composite fonts, other filters) instead of guessed. Encrypted
files report LOCKED. Image-only pages report NEEDS_RENDITION. Nothing is executed or fetched.

Fail-closed review routing: an unresolved or non-standard font encoding, rotated or skewed
text, invisible text and text inside Form XObjects each flag the page for review rather than
emitting a guess. Objects are scanned directly (the cross-reference table is not consulted).
"""

from __future__ import annotations

import math
import re
import zlib
from dataclasses import dataclass, replace

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

PDF_REVISION = "opentour.reader.pdf-text@1.0.3"
# Effective text size, in points, below which text is treated as not human-readable. Real
# fine print is 4 pt or more; below 2 pt (about 0.7 mm) glyphs are specks at any reading size.
# Applied to the effective em height (Tf x Tm x cm) and em width (also x Tz).
MIN_VISIBLE_TEXT_POINTS = 2.0
# A text rise (Ts) beyond this many em is not a super/subscript but a displacement.
MAX_TEXT_RISE_EM = 2.0
# Fill colours whose contrast ratio against white paper is below this are treated as invisible.
# 1.3 is roughly a grey level above 0.89 (about #E3E3E3) and is far below the 3:1 legibility
# floor, so coloured headings (red, navy, mid grey, brand colours) are never flagged.
MIN_TEXT_CONTRAST_AGAINST_WHITE = 1.3
_OBJ = re.compile(rb"(?<![0-9])(\d{1,7})\s+(\d{1,5})\s+obj\b")
_REF = rb"(\d+)\s+\d+\s+R"
_NUMBER = re.compile(rb"[+-]?(\d+\.?\d*|\.\d+)")
_WS = b" \t\r\n\x0c\x00"
_DELIM = b"()<>[]/%"
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
        end_at = data.find(b"endobj", body_start)
        if end_at < 0:
            raise ReaderFailure(ExtractionState.CORRUPT, "unterminated_object", NextActor.OWNER)
        # Bounded to this object so many small objects cannot make the scan quadratic.
        stream_at = data.find(b"stream", body_start, end_at)
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
    if filters != [b"FlateDecode"] or b"/Crypt" in obj.head:
        raise ReaderFailure(ExtractionState.UNSUPPORTED, "pdf_stream_filter", NextActor.OWNER)
    if b"/Predictor" in obj.head:
        # PNG/TIFF predictors would need un-predicting; reading without it would emit garbage.
        raise ReaderFailure(ExtractionState.UNSUPPORTED, "pdf_stream_predictor", NextActor.OWNER)
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
            hexed = re.sub(rb"\s", b"", data[i + 1 : end]) if end >= 0 else b"?"
            if not re.fullmatch(rb"[0-9A-Fa-f]*", hexed):
                raise ReaderFailure(ExtractionState.CORRUPT, "bad_hex_string", NextActor.OWNER)
            if len(hexed) % 2:
                hexed += b"0"
            yield ("str", bytes.fromhex(hexed.decode("ascii")))
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
            if _NUMBER.fullmatch(word):
                yield ("num", float(word))
            else:
                yield ("op", word)
            i = j


def _show(raw: bytes) -> str:
    return raw.decode("cp1252", "replace")


_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _concat(m: tuple[float, ...], n: tuple[float, ...]) -> tuple[float, ...]:
    """PDF matrix product m x n (row-vector convention)."""
    return (
        m[0] * n[0] + m[1] * n[2],
        m[0] * n[1] + m[1] * n[3],
        m[2] * n[0] + m[3] * n[2],
        m[2] * n[1] + m[3] * n[3],
        m[4] * n[0] + m[5] * n[2] + n[4],
        m[4] * n[1] + m[5] * n[3] + n[5],
    )


def _relative_luminance(red: float, green: float, blue: float) -> float:
    def linear(channel: float) -> float:
        channel = min(max(channel, 0.0), 1.0)
        return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4

    return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)


def _invisible_on_white(components: list[float]) -> bool | None:
    """True when a fill colour is near-invisible on white paper; None when not understood.

    The colour space is inferred from the operand count (1 grey, 3 RGB, 4 CMYK), which is exact
    for the device spaces and a conservative guess for the rest.
    """
    if len(components) == 1:
        red = green = blue = components[0]
    elif len(components) == 3:
        red, green, blue = components
    elif len(components) == 4:
        cyan, magenta, yellow, black = (min(max(c, 0.0), 1.0) for c in components)
        red, green, blue = ((1.0 - c) * (1.0 - black) for c in (cyan, magenta, yellow))
    else:
        return None
    contrast = 1.05 / (_relative_luminance(red, green, blue) + 0.05)
    return contrast < MIN_TEXT_CONTRAST_AGAINST_WHITE


@dataclass(frozen=True)
class _Graphics:
    """The graphics-state values that `q` saves and `Q` restores, text state included."""

    ctm: tuple[float, ...] = _IDENTITY
    render_mode: float = 0.0
    size: float = 0.0
    font: bytes = b""
    leading: float = 0.0
    h_scale: float = 1.0
    rise: float = 0.0
    fill_invisible: bool = False


_PAINT = frozenset({b"n", b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"})
_PATH_BUILD = frozenset({b"m", b"l", b"c", b"v", b"y"})


def _fragments(
    content: bytes,
    deadline: Deadline,
    forms: frozenset[bytes] = frozenset(),
    images: frozenset[bytes] = frozenset(),
    page_box: tuple[float, float, float, float] | None = None,
) -> tuple[list[_Fragment], set[str]]:
    """Return positioned fragments and the review flags the content stream earned.

    Positions are mapped through the current transformation matrix (translation and scale are
    honoured). Rotation or skew, invisible text and text living in Form XObjects cannot be put
    in reading order by this reader, so they are flagged for review instead of guessed. This is
    not a renderer: invisibility by size, paint, placement, clipping and overpainting is
    flagged conservatively, never proven absent.
    """
    out: list[_Fragment] = []
    marks: set[str] = set()
    late: set[str] = set()  # page-level flags that only matter when text was actually read
    stack: list[object] = []
    lx = ly = x = y = 0.0
    array: list[object] | None = None
    gs = _Graphics()
    saved: list[_Graphics] = []
    text_matrix: tuple[float, ...] = _IDENTITY
    rects: list[tuple[float, float, float, float]] = []
    path_other = False
    image_drawn = False

    def emit(text: str) -> None:
        if not text.strip():
            return
        if gs.render_mode in (3.0, 7.0):
            marks.add("hidden_text_present")
        m = _concat(text_matrix, gs.ctm)
        scale_x, scale_y = math.hypot(m[0], m[1]), math.hypot(m[2], m[3])
        size = abs(gs.size)
        if (
            gs.size <= 0
            or gs.h_scale <= 0
            or size * scale_y < MIN_VISIBLE_TEXT_POINTS
            or size * scale_x * gs.h_scale < MIN_VISIBLE_TEXT_POINTS
            or abs(gs.rise) > MAX_TEXT_RISE_EM * size
        ):
            marks.add("text_size_invisible")
        if gs.fill_invisible:
            marks.add("text_color_invisible")
        ctm = gs.ctm
        px = ctm[0] * x + ctm[2] * y + ctm[4]
        py = ctm[1] * x + ctm[3] * y + ctm[5]
        raised = y + gs.rise * text_matrix[3]
        rx = ctm[0] * x + ctm[2] * raised + ctm[4]
        ry = ctm[1] * x + ctm[3] * raised + ctm[5]
        # One em of slack: a baseline a hair outside the edge can still show an overhanging glyph.
        slack = size * scale_y
        if page_box is None or any(
            not (
                page_box[0] - slack <= cx <= page_box[2] + slack
                and page_box[1] - slack <= cy <= page_box[3] + slack
            )
            for cx, cy in ((px, py), (rx, ry))
        ):
            marks.add("text_outside_page")
        out.append(_Fragment(px, py, text, gs.size * abs(ctm[3] or 1.0), gs.font))

    def clip_is_noop() -> bool:
        """Only a single axis-aligned rectangle covering the whole page box is a no-op."""
        if page_box is None or len(rects) != 1 or path_other:
            return False
        a, b, c, d, e, f = gs.ctm
        if b != 0 or c != 0:
            return False
        rx, ry, rw, rh = rects[0]
        xs = sorted((a * rx + e, a * (rx + rw) + e))
        ys = sorted((d * ry + f, d * (ry + rh) + f))
        return (
            xs[0] <= page_box[0] + 0.5
            and xs[1] >= page_box[2] - 0.5
            and ys[0] <= page_box[1] + 0.5
            and ys[1] >= page_box[3] - 0.5
        )

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
            text_matrix = _IDENTITY
        elif op == b"Tf" and stack:
            names = [v for v in stack if isinstance(v, bytes) and not isinstance(v, bool)]
            gs = replace(
                gs, size=nums[-1] if nums else gs.size, font=names[0] if names else gs.font
            )
        elif op in (b"Td", b"TD") and len(nums) >= 2:
            lx, ly = lx + nums[-2], ly + nums[-1]
            x, y = lx, ly
            if op == b"TD":
                gs = replace(gs, leading=-nums[-1])
        elif op == b"Tm" and len(nums) >= 6:
            if nums[-5] != 0 or nums[-4] != 0 or nums[-6] <= 0 or nums[-3] <= 0:
                marks.add("layout_transform_unverified")
            text_matrix = tuple(nums[-6:])
            lx, ly = nums[-2], nums[-1]
            x, y = lx, ly
        elif op == b"cm" and len(nums) >= 6:
            gs = replace(gs, ctm=_concat(tuple(nums[-6:]), gs.ctm))
            ctm = gs.ctm
            if ctm[1] != 0 or ctm[2] != 0 or ctm[0] <= 0 or ctm[3] <= 0:
                marks.add("layout_transform_unverified")
        elif op == b"q":
            saved.append(gs)
        elif op == b"Q":
            gs = saved.pop() if saved else replace(gs, ctm=_IDENTITY)
        elif op == b"Tr" and nums:
            gs = replace(gs, render_mode=nums[-1])
        elif op == b"Tz" and nums:
            gs = replace(gs, h_scale=nums[-1] / 100.0)
        elif op == b"Ts" and nums:
            gs = replace(gs, rise=nums[-1])
        elif op in (b"g", b"rg", b"k") and nums:
            wanted = {b"g": 1, b"rg": 3, b"k": 4}[op]
            if len(nums) >= wanted:
                gs = replace(gs, fill_invisible=bool(_invisible_on_white(nums[-wanted:])))
        elif op in (b"sc", b"scn") and nums:
            verdict = _invisible_on_white(nums)
            if verdict is not None:
                gs = replace(gs, fill_invisible=verdict)
        elif op == b"cs":
            gs = replace(gs, fill_invisible=False)  # every initial colour is black or darker
        elif op == b"re" and len(nums) >= 4:
            rects.append((nums[-4], nums[-3], nums[-2], nums[-1]))
        elif op in _PATH_BUILD:
            path_other = True
        elif op in (b"W", b"W*"):
            if not clip_is_noop():
                late.add("clip_region_present")
        elif op in _PAINT:
            rects.clear()
            path_other = False
        elif op == b"Do" and stack and isinstance(stack[-1], bytes):
            if stack[-1] in forms:
                marks.add("form_xobject_text_unread")
            elif stack[-1] in images:
                image_drawn = True
        elif op == b"TL" and nums:
            gs = replace(gs, leading=nums[-1])
        elif op == b"T*":
            ly -= gs.leading
            x, y = lx, ly
        elif op == b"Tj" and stack and isinstance(stack[-1], bytes):
            emit(_show(stack[-1]))
        elif op in (b"'", b'"') and stack and isinstance(stack[-1], bytes):
            ly -= gs.leading
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
    if out:
        if image_drawn:
            late.add("image_text_overlay_unverified")
        marks |= late
    return out, marks


def _font_ok(fonts: dict[bytes, _Object], name: bytes, text: str) -> bool:
    """Only a font whose byte-to-character mapping is provably standard is trusted."""
    obj = fonts.get(name)
    if obj is None:
        return False  # unresolved font resource: the encoding cannot be known
    head = obj.head
    if re.search(rb"/Subtype\s*/Type1\b", head) is None and not re.search(
        rb"/Subtype\s*/(TrueType|MMType1)\b", head
    ):
        return False  # Type0, Type3 and anything unrecognised
    if re.search(rb"/Differences", head) or re.search(rb"/ToUnicode", head):
        return False
    if re.search(rb"/Encoding\s*(?:<<|\d+\s+\d+\s+R)", head):
        return False  # inline or indirect encoding dictionary
    encoding = re.search(rb"/Encoding\s*/(\w+)", head)
    if encoding and encoding[1] == b"WinAnsiEncoding":
        return all(ord(char) >= 32 and char not in "\x7f\ufffd" for char in text)
    standard_subset = all(32 <= ord(char) <= 126 and char not in "'`" for char in text)
    if encoding:
        # This reader decodes with WinAnsi. Only shared ASCII glyphs may be trusted
        # when a different supported font encoding is declared.
        return encoding[1] in {b"MacRomanEncoding", b"StandardEncoding"} and standard_subset
    # Default font encodings are only known for the unembedded standard text fonts.
    return (
        standard_subset
        and re.search(rb"/Subtype\s*/Type1\b", head) is not None
        and b"/FontDescriptor" not in head
        and re.search(
            rb"/BaseFont\s*/(?:Courier(?:-Bold(?:Oblique)?|-Oblique)?"
            rb"|Helvetica(?:-Bold(?:Oblique)?|-Oblique)?|Times-(?:Roman|Bold(?:Italic)?|Italic))"
            rb"(?=[\x00\x09\x0a\x0c\x0d ()<>\[\]{}/%]|$)",
            head,
        )
        is not None
    )


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

    pages: list[tuple[_Object, bytes, tuple[float, float, float, float] | None]] = []
    seen: set[int] = set()

    def walk(number: int, inherited: bytes, inherited_boxes: _Boxes, depth: int) -> None:
        if number in seen or number not in objects:
            raise ReaderFailure(ExtractionState.CORRUPT, "bad_page_tree", NextActor.OWNER)
        if depth > limits.max_xml_depth:
            raise ReaderFailure(
                ExtractionState.OVER_DEPTH, "page_tree_depth", NextActor.OWNER, limit="max_depth"
            )
        seen.add(number)
        obj = objects[number]
        resources = _resources(obj.head, objects) or inherited
        boxes = _Boxes(
            _box(obj.head, b"MediaBox", objects) or inherited_boxes.media,
            _box(obj.head, b"CropBox", objects) or inherited_boxes.crop,
        )
        if re.search(rb"/Type\s*/Pages\b", obj.head):
            kids = re.search(rb"/Kids\s*\[(.*?)\]", obj.head, re.S)
            for kid in re.findall(_REF, kids[1] if kids else b""):
                walk(int(kid), resources, boxes, depth + 1)
        else:
            pages.append((obj, resources, boxes.visible()))
            if len(pages) > limits.max_pages:
                raise ReaderFailure(
                    ExtractionState.OVERSIZED, "page_limit", NextActor.OWNER, limit="pages"
                )

    walk(int(root[1]), b"", _Boxes(None, None), 0)
    spans: list[Span] = []
    image_pages: list[int] = []
    reading_order = 0
    for page_number, (page, resources, page_box) in enumerate(pages, start=1):
        deadline.check()
        fonts = _named_refs(resources, b"Font", objects)
        images = [
            name
            for name, obj in _named_refs(resources, b"XObject", objects).items()
            if re.search(rb"/Subtype\s*/Image", obj.head)
        ]
        contents = re.search(rb"/Contents\s*(\[(.*?)\]|" + _REF + rb")", page.head, re.S)
        refs = re.findall(_REF, contents[0]) if contents else []
        readable = [r for r in refs if int(r) in objects and objects[int(r)].stream is not None]
        stream = b"\n".join(_decode(objects[int(r)], limits) for r in readable)
        forms = frozenset(
            name
            for name, obj in _named_refs(resources, b"XObject", objects).items()
            if re.search(rb"/Subtype\s*/Form", obj.head)
        )
        fragments, marks = _fragments(stream, deadline, forms, frozenset(images), page_box)
        if len(readable) != len(refs):
            # A referenced content stream that cannot be resolved means the page was not read.
            marks.add("content_stream_unresolved")
        if not fragments:
            if images or marks:
                image_pages.append(page_number)
            continue
        flagged_font = any(not _font_ok(fonts, f.font, f.text) for f in fragments)
        passages = _passages(_lines(fragments))
        for index, lines in enumerate(passages, start=1):
            reading_order += 1
            cells = [[c.text for c in line] for line in lines]
            table = len(cells) >= 2 and all(len(row) >= 2 for row in cells)
            flags: list[str] = sorted(marks)
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


@dataclass(frozen=True)
class _Boxes:
    media: tuple[float, float, float, float] | None
    crop: tuple[float, float, float, float] | None

    def visible(self) -> tuple[float, float, float, float] | None:
        """The page area a viewer can see; None when the MediaBox cannot be read."""
        if self.media is None:
            return None
        if self.crop is None:
            return self.media
        return (
            max(self.media[0], self.crop[0]),
            max(self.media[1], self.crop[1]),
            min(self.media[2], self.crop[2]),
            min(self.media[3], self.crop[3]),
        )


def _box(
    head: bytes, key: bytes, objects: dict[int, _Object]
) -> tuple[float, float, float, float] | None:
    """Parse an inline or singly-indirect rectangle such as `/MediaBox [0 0 612 792]`."""
    match = re.search(rb"/" + key + rb"\s*(\[[^\]]*\]|" + _REF + rb")", head)
    if match is None:
        return None
    body = match[1]
    if not body.startswith(b"["):
        target = objects.get(int(match[2]))
        body = target.head if target is not None else b""
    numbers = re.findall(rb"[+-]?(?:\d+\.?\d*|\.\d+)", body)
    if len(numbers) != 4 or not body.strip().startswith(b"["):
        return None
    x0, y0, x1, y1 = (float(n) for n in numbers)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


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
