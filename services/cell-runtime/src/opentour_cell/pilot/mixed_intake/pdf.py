"""Minimal text-bearing PDF reader: page + passage citations in positional reading order.

Scope is deliberately narrow and honest. It reads classic cross-reference PDFs with simple
(single-byte) fonts and Flate or unfiltered content streams. Anything else is reported with a
specific state (object streams, composite fonts, other filters) instead of guessed. Encrypted
files report LOCKED. Image-only pages report NEEDS_RENDITION. Nothing is executed or fetched.

Fail-closed review routing: an unresolved or non-standard font encoding, rotated or skewed
text, invisible text and text inside Form XObjects each flag the page for review rather than
emitting a guess. Objects are scanned directly (the cross-reference table is not consulted; a
repeated object number keeps its last definition). Every dictionary the reader relies on is
parsed, not searched, and one that repeats a key or cannot be read goes to review.

This is not a renderer, and invisibility is flagged only where it can be seen in the content
stream without painting. Covered: render mode, effective size (Tf, Tm, cm, Tz, Ts), near-white
fill (g, rg, k, sc, scn), text origin or extent outside the page box, text outside a clip
region, and page-sized or text-overlapping image XObjects. NOT covered, and therefore able to
admit hidden text as ELIGIBLE:

- inline images (`BI ... ID ... EI`) are not detected at all, so a page-sized inline image
  over text is not flagged;
- vector overpaint is not detected, e.g. `1 g ... re f` painted over text after it;
- ExtGState transparency is not detected, e.g. `/GS gs` with zero alpha;
- stroke-only text (`1 Tr`) is judged by its fill colour, not its stroke colour.

Known cost, by design: white or reverse-out text on a dark band (banner and table headers) is
flagged `text_color_invisible`, because no background is tracked and visibility cannot be
established without it. Closing that needs painted-background tracking: the fill colour and
extent of every filled rectangle and image, composited under each text fragment.
"""

from __future__ import annotations

import math
import re
import unicodedata
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

PDF_REVISION = "opentour.reader.pdf-text@1.0.6"
# The three thresholds below are adjustable judgement values, not contract values: they trade
# review volume against missed concealment and may be retuned without a format change.
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
# An image whose placed bounding box covers at least this fraction of the visible page can hide
# a paragraph, so a page with such an image and text goes to review. Logos, stamps and
# signatures are typically under 10% of a page; a quarter page is large enough to cover several
# lines. Any image overlapping an extracted text fragment also flags, whatever its size, since
# a small opaque image placed over text conceals it. Fragment extents come from glyph advance
# widths, Tc, Tw and TJ adjustments (see the width sources below), bounded wide, never narrow.
IMAGE_REVIEW_PAGE_FRACTION = 0.25
_OBJ = re.compile(rb"(?<![0-9])(\d{1,7})\s+(\d{1,5})\s+obj\b")
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


# ------------------------------------------------------------------ object syntax
# Every dictionary the reader relies on (catalog, page tree, resources, fonts, font
# descriptors, /Widths, XObject and content-stream dictionaries) is parsed, never searched: a
# key inside a string, a comment or a nested dictionary is not that dictionary's key, and a
# `#xx`-escaped name is its decoded name. A dictionary that repeats a key is ambiguous (a viewer
# may take either value), so it is never read: the page goes to review as pdf_object_ambiguous.
# So does a head that is malformed, nested deeper than _MAX_OBJECT_DEPTH, longer than
# _MAX_HEAD_BYTES or holding a number that is not finite.
_MAX_OBJECT_DEPTH = 32
_MAX_HEAD_BYTES = 262_144
_REGULAR = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]*")
_HEX_PAIR = re.compile(rb"#([0-9A-Fa-f]{2})")


class _Name(str):
    """A PDF name with its `#xx` escapes decoded (latin-1 text)."""

    __slots__ = ()


@dataclass(frozen=True)
class _Ref:
    number: int
    generation: int


_NO_STREAM = _Object(0, b"", None)


class _Unreadable(Exception):
    """An object head that cannot be read unambiguously."""


def _number(word: bytes) -> int | float:
    if not _NUMBER.fullmatch(word):
        raise _Unreadable("not a value")
    if b"." not in word and len(word) <= 16:
        return int(word)
    try:
        value = float(word)
    except OverflowError as error:  # pragma: no cover - float() returns inf instead
        raise _Unreadable("number out of range") from error
    if not math.isfinite(value):
        raise _Unreadable("number out of range")
    return value


class _HeadParser:
    def __init__(self, data: bytes) -> None:
        self.data, self.at = data, 0

    def skip(self) -> None:
        data, n = self.data, len(self.data)
        while self.at < n:
            byte = data[self.at]
            if byte in _WS:
                self.at += 1
            elif byte == 0x25:  # % comment, to the end of the line
                while self.at < n and data[self.at] not in (10, 13):
                    self.at += 1
            else:
                return

    def word(self) -> bytes:
        match = _REGULAR.match(self.data, self.at)
        assert match is not None
        self.at = match.end()
        return match[0]

    def value(self, depth: int) -> object:
        if depth > _MAX_OBJECT_DEPTH:
            raise _Unreadable("nested too deep")
        self.skip()
        data = self.data
        if self.at >= len(data):
            raise _Unreadable("missing value")
        byte = data[self.at]
        if data.startswith(b"<<", self.at):
            return self.dictionary(depth)
        if byte == 0x5B:  # [
            self.at += 1
            items: list[object] = []
            while True:
                self.skip()
                if data.startswith(b"]", self.at):
                    self.at += 1
                    return items
                items.append(self.value(depth + 1))
        if byte == 0x28:  # (
            return self.literal()
        if byte == 0x3C:  # <
            end = data.find(b">", self.at)
            digits = (
                re.sub(rb"[\x00\t\n\x0c\r ]", b"", data[self.at + 1 : end]) if end > 0 else b"?"
            )
            if not re.fullmatch(rb"[0-9A-Fa-f]*", digits):
                raise _Unreadable("bad hex string")
            self.at = end + 1
            return bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode("ascii"))
        if byte == 0x2F:  # /
            return self.name()
        word = self.word()
        if word in (b"true", b"false"):
            return word == b"true"
        if word == b"null":
            return None
        number = _number(word)
        if isinstance(number, int) and number >= 0 and not word.startswith((b"+", b"-")):
            mark = self.at
            self.skip()
            generation = self.word()
            if generation.isdigit():
                self.skip()
                if self.word() == b"R":
                    return _Ref(number, int(generation))
            self.at = mark
        return number

    def dictionary(self, depth: int) -> dict[str, object]:
        self.at += 2
        out: dict[str, object] = {}
        while True:
            self.skip()
            if self.data.startswith(b">>", self.at):
                self.at += 2
                return out
            if not self.data.startswith(b"/", self.at):
                raise _Unreadable("dictionary key is not a name")
            key = self.name()
            value = self.value(depth + 1)
            if key in out:
                raise _Unreadable("duplicate dictionary key")
            out[key] = value

    def name(self) -> _Name:
        self.at += 1
        raw = self.word()
        if raw.count(b"#") != len(_HEX_PAIR.findall(raw)):
            raise _Unreadable("bad name escape")
        decoded = _HEX_PAIR.sub(lambda m: bytes([int(m[1], 16)]), raw)
        return _Name(decoded.decode("latin-1"))

    def literal(self) -> bytes:
        data, n = self.data, len(self.data)
        depth, at, out = 1, self.at + 1, bytearray()
        while at < n:
            byte = data[at]
            if byte == 0x5C and at + 1 < n:  # backslash escape
                nxt = data[at + 1]
                if 0x30 <= nxt <= 0x37:
                    end = at + 1
                    while end < n and end < at + 4 and 0x30 <= data[end] <= 0x37:
                        end += 1
                    out.append(int(data[at + 1 : end], 8) & 0xFF)
                    at = end
                    continue
                if nxt in (10, 13):
                    at += 3 if data[at + 1 : at + 3] == b"\r\n" else 2
                    continue
                out.append(_ESCAPES.get(nxt, nxt))
                at += 2
                continue
            if byte == 0x28:
                depth += 1
            elif byte == 0x29:
                depth -= 1
                if depth == 0:
                    self.at = at + 1
                    return bytes(out)
            out.append(byte)
            at += 1
        raise _Unreadable("unterminated string")


def _parse_head(head: bytes) -> object:
    """The single value an object head holds; raises _Unreadable rather than guess."""
    if len(head) > _MAX_HEAD_BYTES:
        raise _Unreadable("object head too long")
    parser = _HeadParser(head)
    value = parser.value(0)
    parser.skip()
    if parser.at != len(head):
        raise _Unreadable("trailing content")
    return value


class _Doc:
    """Parsed object heads, read on demand; remembers which could not be read unambiguously."""

    def __init__(self, objects: dict[int, _Object], deadline: Deadline) -> None:
        self.objects, self.deadline = objects, deadline
        self.values: dict[int, object] = {}
        self.bad: set[int] = set()
        self.touched: set[int] = set()

    def value(self, number: int) -> object:
        self.touched.add(number)
        if number not in self.values:
            self.deadline.check()
            obj = self.objects.get(number)
            try:
                self.values[number] = None if obj is None else _parse_head(obj.head)
            except _Unreadable:
                self.bad.add(number)
                self.values[number] = None
        return self.values[number]

    def resolve(self, value: object) -> object:
        seen: set[int] = set()
        while isinstance(value, _Ref):
            if value.number in seen or len(seen) >= 8:
                return None  # a reference cycle or chain resolves to nothing
            seen.add(value.number)
            value = self.value(value.number)
        return value

    def dict(self, value: object) -> dict[str, object] | None:
        resolved = self.resolve(value)
        return resolved if isinstance(resolved, dict) else None

    def ambiguous(self) -> bool:
        """Whether anything read since `touched` was last cleared was unreadable."""
        return bool(self.touched & self.bad)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_dict(head: bytes) -> dict[str, object] | None:
    """A standalone dictionary head (no references resolved), or None when unreadable."""
    try:
        value = _parse_head(head)
    except _Unreadable:
        return None
    return value if isinstance(value, dict) else None


def _decode(obj: _Object, head: dict[str, object] | None, doc: _Doc, limits: Limits) -> bytes:
    raw = obj.stream or b""
    if head is None:
        raise ReaderFailure(ExtractionState.UNSUPPORTED, "pdf_stream_filter", NextActor.OWNER)
    filters = doc.resolve(head.get("Filter"))
    names = filters if isinstance(filters, list) else [] if filters is None else [filters]
    if not names:
        return raw
    if [str(name) if isinstance(name, _Name) else None for name in names] != ["FlateDecode"]:
        raise ReaderFailure(ExtractionState.UNSUPPORTED, "pdf_stream_filter", NextActor.OWNER)
    params = doc.resolve(head.get("DecodeParms"))
    params = params[0] if isinstance(params, list) and params else params
    params = doc.dict(params) if params is not None else {}
    predictor = doc.resolve((params or {}).get("Predictor"))
    if params is None or (predictor is not None and predictor != 1):
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
            # Names match resource keys after #xx decoding, as the dictionaries are read.
            yield ("name", _HEX_PAIR.sub(lambda m: bytes([int(m[1], 16)]), data[i + 1 : j]))
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


# Text extents come from per-glyph widths (1000 units per em). Every extent check (page edge,
# clip, image overlap) asks whether a show's glyph boxes reach past a boundary, so all of them
# use one bound per font: too wide only sends a page to review, too narrow can admit text drawn
# off the page, outside a clip or under an image. The pen moves as a viewer moves it, by the
# declared advance plus Tc, Tw, Tz and TJ adjustments, leftward included. A viewer still draws
# each glyph at its natural size wherever the pen puts it, so each glyph's box is
# [pen, pen + max(declared, natural)], natural being the AFM width for an exact standard name
# and the source-3 bound otherwise (_Metrics.ink). Declared widths far below the drawn glyph
# also pile glyphs up even away from any edge: see glyph_overprint_suspected in _extent_units.
# Width sources, most authoritative first:
#
# 1. A simple font's own /FirstChar, /LastChar and /Widths, parsed and checked here. A viewer
#    positions glyphs from it whatever /BaseFont says, so a font that embeds a wide program and
#    names it /Helvetica is measured by its real widths. Codes outside the range take the
#    largest of /MissingWidth and sources 2 and 3. An array that is incomplete, mis-sized, past
#    the one-byte range, or holds a non-number, a reference or a negative width is never
#    trusted: the font takes source 2 or 3, with no glyph below the array's largest number.
#    Large entries are used as given, since they can only widen the extent.
# 2. No /Widths, no FontDescriptor and a name that is exactly a standard face: Helvetica and
#    Arial (regular, bold, oblique/italic, MT), Times-Roman, -Bold, -Italic and -BoldItalic and
#    Times New Roman in the same styles, Courier and Courier New in their styles. That face's
#    Adobe core-14 AFM widths. These are ground truth, not an estimate: a standard-14 face with
#    no font program of its own is drawn by the viewer from exactly these metrics, so a table
#    narrower than the fallback is not an under-estimate. This holds only for an exact name
#    with no FontDescriptor; everything else is source 3 or goes to review.
# 3. Anything else: another name (Arial-Black, Helvetica-Light, CourierPrime), a standard name
#    with a FontDescriptor of its own, an unresolved, Type0 or Type3 font. Those take
#    _PROPORTIONAL_MAX, the per-glyph maximum of Helvetica, Helvetica-Bold, Times-Roman,
#    Times-Bold, Times-Italic and Times-BoldItalic (the obliques share their upright widths),
#    never less than the named face's own widths. A name starting "courier" keeps at least
#    Courier's 600. No such bound is below where it was at opentour.reader.pdf-text@1.0.3.
#    A simple (Type1, TrueType, MMType1) font in this group with no valid /Widths also goes to
#    review as font_widths_unavailable (see _widths_unavailable): the PDF format requires
#    /Widths for it, and a viewer may draw it from its embedded program, so no assumed bound
#    is safe. Unresolved, Type0 and Type3 fonts already go to review as font_encoding_unverified.
#
# Outside printable ASCII, a letter with an accent takes the source-3 width of its unaccented
# letter, which is at least the accented glyph's AFM width in all six faces (the only accented
# glyphs wider than their base letter, Helvetica's i-grave/acute/circumflex/dieresis at 278
# against 222, are within it). In the source-3 bound that width is never below
# _NON_ASCII_FLOOR, the 556 that 1.0.3 measured every such code at. Anything else (dashes,
# ellipsis, symbols, unmapped codes) takes _WIDEST_GLYPH, the widest WinAnsi upper-half glyph in
# those six faces (of the glyphs the AFM data covers; it has no Euro, no-break space or soft
# hyphen).
#
# Residuals, measured against this table, not closed here:
# - A non-standard simple font with no valid /Widths is routed to review, not measured: of 75
#   TrueType/OpenType faces measured, 41 are wider than _PROPORTIONAL_MAX on an English pangram
#   (up to 1.21 times, the monospaced faces) and every one has at least one glyph wider.
# - Courier New without /Widths is measured at Courier's 600; its TrueType advance is
#   1229/2048 = 600.1 (measured on the metric-compatible Liberation Mono).
# - Glyph boxes run from the pen to the larger of the declared and natural advance. True ink
#   outside that box is not modelled: italic overhang past the advance, and outlines wider than
#   their AFM width or than _PROPORTIONAL_MAX.
# - Overprint is checked within one show and the shows that follow it from the same position.
#   Shows placed onto each other by separate positioning operators are not compared.
# - Type3 widths (glyph space) and Type0 widths (/W) are not read. Such text already goes to
#   review as font_encoding_unverified.
_WIDEST_GLYPH = 1000
_NON_ASCII_FLOOR = 556  # what 1.0.3 measured any code outside printable ASCII at
_ASCII = (
    " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~"
)
_HELVETICA = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015,
    667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778,
    722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333,
    556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556,
    333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)  # fmt: skip
_HELVETICA_BOLD = (
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975,
    722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667, 778,
    722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333,
    556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611,
    389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
)  # fmt: skip
_TIMES_ROMAN = (
    250, 333, 408, 500, 500, 833, 778, 180, 333, 333, 500, 564, 250, 333, 250, 278,
    500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 278, 278, 564, 564, 564, 444, 921,
    722, 667, 667, 722, 611, 556, 722, 722, 333, 389, 722, 611, 889, 722, 722, 556, 722,
    667, 556, 611, 722, 722, 944, 722, 722, 611, 333, 278, 333, 469, 500, 333,
    444, 500, 444, 500, 444, 333, 500, 500, 278, 278, 500, 278, 778, 500, 500, 500, 500,
    333, 389, 278, 500, 500, 722, 500, 500, 444, 480, 200, 480, 541,
)  # fmt: skip
_TIMES_BOLD = (
    250, 333, 555, 500, 500, 1000, 833, 278, 333, 333, 500, 570, 250, 333, 250, 278,
    500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 333, 333, 570, 570, 570, 500, 930,
    722, 667, 722, 722, 667, 611, 778, 778, 389, 500, 778, 667, 944, 722, 778, 611, 778,
    722, 556, 667, 722, 722, 1000, 722, 722, 667, 333, 278, 333, 581, 500, 333,
    500, 556, 444, 556, 444, 333, 500, 556, 278, 333, 556, 278, 833, 556, 500, 556, 556,
    444, 389, 333, 556, 500, 722, 500, 500, 444, 394, 220, 394, 520,
)  # fmt: skip
_TIMES_ITALIC = (
    250, 333, 420, 500, 500, 833, 778, 214, 333, 333, 500, 675, 250, 333, 250, 278,
    500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 333, 333, 675, 675, 675, 500, 920,
    611, 611, 667, 722, 611, 611, 722, 722, 333, 444, 667, 556, 833, 667, 722, 611, 722,
    611, 500, 556, 722, 611, 833, 611, 556, 556, 389, 278, 389, 422, 500, 333,
    500, 500, 444, 500, 444, 278, 500, 500, 278, 278, 444, 278, 722, 500, 500, 500, 500,
    389, 389, 278, 500, 444, 667, 444, 444, 389, 400, 275, 400, 541,
)  # fmt: skip
_TIMES_BOLD_ITALIC = (
    250, 389, 555, 500, 500, 833, 778, 278, 333, 333, 500, 570, 250, 333, 250, 278,
    500, 500, 500, 500, 500, 500, 500, 500, 500, 500, 333, 333, 570, 570, 570, 500, 832,
    667, 667, 667, 722, 667, 667, 722, 778, 389, 500, 667, 611, 889, 722, 722, 611, 722,
    667, 556, 611, 722, 667, 889, 667, 611, 611, 333, 278, 333, 570, 500, 333,
    500, 500, 444, 500, 444, 333, 500, 556, 278, 278, 500, 278, 778, 556, 500, 500, 500,
    389, 389, 278, 556, 444, 667, 500, 444, 389, 348, 220, 348, 570,
)  # fmt: skip
_PROPORTIONAL_MAX = (
    278, 389, 555, 556, 556, 1000, 833, 278, 333, 333, 500, 675, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 675, 675, 675, 611, 1015,
    722, 722, 722, 722, 667, 667, 778, 778, 389, 556, 778, 667, 944, 722, 778, 667, 778,
    722, 667, 667, 722, 722, 1000, 722, 722, 667, 389, 278, 389, 584, 556, 333,
    556, 611, 556, 611, 556, 333, 611, 611, 278, 333, 556, 278, 889, 611, 611, 611, 611,
    444, 556, 333, 611, 556, 778, 556, 556, 500, 480, 280, 480, 584,
)  # fmt: skip
_GLYPH_WIDTHS: dict[str | None, dict[str, int]] = {
    "helvetica": dict(zip(_ASCII, _HELVETICA, strict=True)),
    "helvetica-bold": dict(zip(_ASCII, _HELVETICA_BOLD, strict=True)),
    "times": dict(zip(_ASCII, _TIMES_ROMAN, strict=True)),
    "times-bold": dict(zip(_ASCII, _TIMES_BOLD, strict=True)),
    "times-italic": dict(zip(_ASCII, _TIMES_ITALIC, strict=True)),
    "times-bolditalic": dict(zip(_ASCII, _TIMES_BOLD_ITALIC, strict=True)),
    "courier": {},  # monospaced: every glyph, whatever its code, advances 600
    None: dict(zip(_ASCII, _PROPORTIONAL_MAX, strict=True)),
}


def _char_width(table: str | None, char: str) -> int:
    if table == "courier":
        return 600
    if char in _GLYPH_WIDTHS[table]:
        return _GLYPH_WIDTHS[table][char]
    base = unicodedata.normalize("NFD", char)[:1]
    width = _GLYPH_WIDTHS[None][base] if base != char and base in _GLYPH_WIDTHS[None] else None
    if width is None:
        return _WIDEST_GLYPH
    # The generous bound keeps the 1.0.3 floor: 556 for every code outside printable ASCII.
    return max(width, _NON_ASCII_FLOOR) if table is None else width


# Advance width of every one-byte code, decoded as this reader decodes it (cp1252).
_ADVANCES: dict[str | None, tuple[float, ...]] = {
    table: tuple(
        float(_char_width(table, bytes([code]).decode("cp1252", "replace"))) for code in range(256)
    )
    for table in _GLYPH_WIDTHS
}
# Printable, non-space glyphs: the ones whose piling up hides what the extracted text says.
_PRINTABLE = tuple(
    (char := bytes([code]).decode("cp1252", "replace")).isprintable()
    and not char.isspace()
    and char != "\ufffd"
    for code in range(256)
)
_SIMPLE_SUBTYPES = frozenset({"Type1", "TrueType", "MMType1"})
# Overprint thresholds (see glyph_overprint_suspected in _extent_units), in 1/1000 em.
OVERPRINT_ADVANCE_UNITS = 100.0
OVERPRINT_STANDARD_RATIO = 0.5
OVERPRINT_RUN = 3


@dataclass(frozen=True)
class _Metrics:
    """Per-code widths (1000 units per em) of one font, as this reader bounds them."""

    advance: tuple[float, ...]  # how far the pen moves: the declared width, or the bound
    ink: tuple[float, ...]  # the advance box drawn: never narrower than the natural glyph
    natural: tuple[float, ...]  # the glyph's own width: AFM for an exact name, else the bound
    exact: bool  # an exact standard name with no font program of its own
    declared: bool  # advances come from a valid /Widths


def _uniform(advances: tuple[float, ...], exact: bool) -> _Metrics:
    return _Metrics(advances, advances, advances, exact, False)


_FALLBACK_METRICS = _uniform(_ADVANCES[None], False)


def _base_name(font: dict[str, object] | None) -> str:
    """The simple font's /BaseFont, lower-cased, without a subset tag ("" when there is none)."""
    if not font or font.get("Subtype") not in _SIMPLE_SUBTYPES:
        return ""
    base = font.get("BaseFont")
    if not isinstance(base, _Name):
        return ""
    return re.sub(r"^[A-Z]{6}\+", "", str(base)).lower().replace(",", "-")


def _width_table_for(font: dict[str, object] | None) -> str | None:
    """Name the width table of a simple standard-metric font, or None when there is none."""
    name = _base_name(font)
    # Exact family names only: CourierPrime, Arial-Black (wider), ArialNarrow, Helvetica-Light,
    # -Condensed and the like have different metrics and must never borrow these tables.
    if re.fullmatch(r"courier(-bold|-oblique|-boldoblique)?", name) or re.fullmatch(
        r"couriernew(ps)?(-bold|-italic|-bolditalic)?(mt)?", name
    ):
        return "courier"
    if re.fullmatch(r"(helvetica|arial)-?(bold)?(oblique|italic)?(mt)?", name):
        return "helvetica-bold" if "bold" in name else "helvetica"
    if re.fullmatch(r"times-(roman|bold|italic|bolditalic)", name) or re.fullmatch(
        r"timesnewroman(ps)?(-bold|-italic|-bolditalic)?(mt)?", name
    ):
        style = ("bold" if "bold" in name else "") + ("italic" if "italic" in name else "")
        return f"times-{style}" if style else "times"
    return None


def _declared_widths(font: dict[str, object], doc: _Doc) -> tuple[int, list[float]] | float | None:
    """(FirstChar, widths) when well formed; the largest number seen when not; None if absent."""
    if not any(key in font for key in ("FirstChar", "LastChar", "Widths")):
        return None
    array = doc.resolve(font.get("Widths"))
    numbers = [float(v) for v in array if _is_number(v)] if isinstance(array, list) else []
    floor = max([0.0, *numbers])
    first, last = doc.resolve(font.get("FirstChar")), doc.resolve(font.get("LastChar"))
    if not isinstance(array, list) or not all(_is_number(v) for v in array):
        return floor
    if type(first) is not int or type(last) is not int:
        return floor
    widths = [float(v) for v in array]
    if not (0 <= first <= last <= 255 and len(widths) == last - first + 1):
        return floor
    if any(width < 0 for width in widths):
        return floor
    return first, widths


def _exact(font: dict[str, object] | None) -> str | None:
    """The AFM table of an exact standard name with no font program of its own, or None."""
    table = _width_table_for(font)
    return (
        table if font is not None and table is not None and "FontDescriptor" not in font else None
    )


def _font_metrics(font: dict[str, object] | None, doc: _Doc | None) -> _Metrics:
    """How far each code moves the pen, and how far its drawn glyph can reach."""
    doc = doc if doc is not None else _Doc({}, Deadline(60.0))
    generous = _ADVANCES[None]
    if not font or font.get("Subtype") not in _SIMPLE_SUBTYPES:
        return _uniform(generous, False)
    exact = _exact(font)
    if exact is not None:
        natural = _ADVANCES[exact]
    else:  # no standard name, or a standard name over a font program of its own
        table = _width_table_for(font)
        # A Courier-like name (CourierPrime, Courier-Condensed) keeps at least Courier's 600.
        family = table or ("courier" if _base_name(font).startswith("courier") else None)
        natural = tuple(map(max, _ADVANCES[family], generous))
    declared = _declared_widths(font, doc)
    if declared is None:
        return _uniform(natural, exact is not None)
    if isinstance(declared, float):  # malformed: never trusted, and never below its entries
        return _uniform(tuple(max(width, declared) for width in natural), exact is not None)
    start, widths = declared
    descriptor = doc.dict(font.get("FontDescriptor")) or {}
    missing = doc.resolve(descriptor.get("MissingWidth"))
    fill = max(float(missing), 0.0) if _is_number(missing) else 0.0  # type: ignore[arg-type]
    advance = tuple(
        widths[code - start]
        if start <= code < start + len(widths)
        else max(natural[code], generous[code], fill)
        for code in range(256)
    )
    ink = tuple(map(max, advance, natural))
    return _Metrics(advance, ink, natural, exact is not None, True)


def _widths_unavailable(font: dict[str, object] | None, doc: _Doc) -> bool:
    """A simple font whose advances only a guess can supply: it goes to review.

    Outside an exact standard name with no font program of its own, the PDF format requires a
    valid /Widths, and a viewer may otherwise draw from the embedded program, whose glyphs can
    be wider than any bound this reader assumes.
    """
    if not font or font.get("Subtype") not in _SIMPLE_SUBTYPES:
        return False  # unresolved, Type0 and Type3 fonts already flag font_encoding_unverified
    if _exact(font) is not None:
        return False
    return not isinstance(_declared_widths(font, doc), tuple)


def _extent_units(
    pieces: list[bytes | float],
    metrics: _Metrics,
    size: float,
    char_spacing: float = 0.0,
    word_spacing: float = 0.0,
) -> tuple[float, float, float, bool]:
    """(leftmost, rightmost, final pen, overprint) of a show's glyph boxes, in text space.

    `pieces` are the shown strings and, for TJ, the numeric adjustments between them. The pen
    moves exactly as a viewer moves it (PDF 9.4.4): by the declared advance, Tc, Tw and TJ
    adjustments, leftward included. Each glyph's box spans [pen, pen + max(declared, natural)],
    because a viewer draws the glyph at its natural size wherever /Widths puts the pen.

    Overprint (glyph_overprint_suspected) is a pile of printable glyphs a human sees as a blob
    while the extracted text reads cleanly: (i) any printable non-space glyph declared under
    OVERPRINT_ADVANCE_UNITS; (ii) for an exact standard name only, OVERPRINT_RUN consecutive
    printable glyphs declared under OVERPRINT_STANDARD_RATIO of their AFM width (a
    non-standard face has no natural width to compare with, and condensed faces are real);
    (iii) OVERPRINT_RUN consecutive printable glyphs each starting less than
    OVERPRINT_ADVANCE_UNITS after the previous one, whatever moved them (Tc, Tw, TJ).
    """
    pen = left = right = 0.0
    overprint = False
    squeezed = stacked = 0
    previous: float | None = None  # where the last printable glyph started, when it was the last
    em = size / 1000.0
    for piece in pieces:
        if not isinstance(piece, bytes):
            pen -= piece * em
            left, right = min(left, pen), max(right, pen)
            continue
        for code in piece:
            advance = metrics.advance[code]
            printable = _PRINTABLE[code]
            if printable:
                if metrics.declared and advance < OVERPRINT_ADVANCE_UNITS:
                    overprint = True
                low = metrics.exact and advance < OVERPRINT_STANDARD_RATIO * metrics.natural[code]
                squeezed = squeezed + 1 if metrics.declared and low else 0
                close = (
                    previous is not None
                    and size > 0
                    and pen - previous < OVERPRINT_ADVANCE_UNITS * em
                )
                stacked = stacked + 1 if close else 1
                if squeezed >= OVERPRINT_RUN or stacked >= OVERPRINT_RUN:
                    overprint = True
                previous = pen
            else:
                squeezed, stacked, previous = 0, 0, None
            left, right = min(left, pen), max(right, pen + metrics.ink[code] * em)
            pen += advance * em + char_spacing + (word_spacing if code == 32 else 0.0)
        left, right = min(left, pen), max(right, pen)
    return left, right, pen, overprint


def _relative_luminance(red: float, green: float, blue: float) -> float:
    def linear(channel: float) -> float:
        channel = min(max(channel, 0.0), 1.0)
        return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4

    return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)


def _cmyk_to_rgb(cyan: float, magenta: float, yellow: float, black: float) -> tuple[float, ...]:
    """Approximate SWOP-like CMYK to sRGB (the polynomial fit pdf.js uses for DeviceCMYK).

    The naive `(1 - c)(1 - k)` makes pure cyan #00FFFF (contrast about 1.25 against white),
    while printed process cyan is about #00AEEF (contrast about 2.3), so print-origin cyan
    headings would be wrongly flagged.
    """
    c, m, y, k = (min(max(v, 0.0), 1.0) for v in (cyan, magenta, yellow, black))
    red = (
        255
        + c
        * (
            -4.387332384609988 * c
            + 54.48615194189176 * m
            + 18.82290502165302 * y
            + 212.25662451639585 * k
            - 285.2331026137004
        )
        + m
        * (
            1.7149763477362134 * m
            - 5.6096736904047315 * y
            - 17.873870861415444 * k
            - 5.497006427196366
        )
        + y * (-2.5217340131683033 * y - 21.248923337353073 * k + 17.5119270841813)
        + k * (-21.86122147463605 * k - 189.48180835922747)
    )
    green = (
        255
        + c
        * (
            8.841041422036149 * c
            + 60.118027045597366 * m
            + 6.871425592049007 * y
            + 31.159100130055922 * k
            - 79.2970844816548
        )
        + m
        * (
            -15.310361306967817 * m
            + 17.575251261109482 * y
            + 131.35250912493976 * k
            - 190.9453302588951
        )
        + y * (4.444339102852739 * y + 9.8632861493405 * k - 24.86741582555878)
        + k * (-20.737325471181034 * k - 187.80453709719578)
    )
    blue = (
        255
        + c
        * (
            0.8842522430003296 * c
            + 8.078677503112928 * m
            + 30.89978309703729 * y
            - 0.23883238689178934 * k
            - 14.183576799673286
        )
        + m
        * (
            10.49593273432072 * m
            + 63.02378494754052 * y
            + 50.606957656360734 * k
            - 112.23884253719248
        )
        + y * (0.03296041114873217 * y + 115.60384449646641 * k - 193.58209356861505)
        + k * (-22.33816807309886 * k - 180.12613974708367)
    )
    return tuple(min(max(v, 0.0), 255.0) / 255.0 for v in (red, green, blue))


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
        red, green, blue = _cmyk_to_rgb(*components)
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
    char_spacing: float = 0.0
    word_spacing: float = 0.0
    # Device-space clip rectangle (intersection of every rectangular clip so far), or None.
    clip: tuple[float, float, float, float] | None = None
    clip_unknown: bool = False  # a clip whose region could not be determined


_PAINT = frozenset({b"n", b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"})
_PATH_BUILD = frozenset({b"m", b"l", b"c", b"v", b"y"})


def _image_may_conceal(
    image: tuple[float, float, float, float],
    page_box: tuple[float, float, float, float] | None,
    texts: list[tuple[float, float, float, float]],
) -> bool:
    """Large images, and images over extracted text, may hide it; small clear ones may not."""
    if page_box is None:
        return True  # nothing to measure against: fail closed
    page_area = (page_box[2] - page_box[0]) * (page_box[3] - page_box[1])
    left, bottom = max(image[0], page_box[0]), max(image[1], page_box[1])
    right, top = min(image[2], page_box[2]), min(image[3], page_box[3])
    covered = max(right - left, 0.0) * max(top - bottom, 0.0)
    if page_area <= 0 or covered / page_area >= IMAGE_REVIEW_PAGE_FRACTION:
        return True
    return any(
        text[0] < image[2] and text[2] > image[0] and text[1] < image[3] and text[3] > image[1]
        for text in texts
    )


def _fragments(
    content: bytes,
    deadline: Deadline,
    forms: frozenset[bytes] = frozenset(),
    images: frozenset[bytes] = frozenset(),
    page_box: tuple[float, float, float, float] | None = None,
    font_metrics: dict[bytes, _Metrics] | None = None,
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
    image_boxes: list[tuple[float, float, float, float]] = []
    text_boxes: list[tuple[float, float, float, float]] = []
    # Anchor of the current positioning and the pen advance of the shows since it.
    adv_anchor: tuple[object, ...] = ()
    adv_pen = 0.0

    def emit(text: str, pieces: list[bytes | float]) -> None:
        nonlocal adv_anchor, adv_pen
        m = _concat(text_matrix, gs.ctm)
        scale_x, scale_y = math.hypot(m[0], m[1]), math.hypot(m[2], m[3])
        size = abs(gs.size)
        ctm = gs.ctm
        px = ctm[0] * x + ctm[2] * y + ctm[4]
        py = ctm[1] * x + ctm[3] * y + ctm[5]
        # Several shows after one positioning operator advance along the line, whitespace-only
        # shows included.
        anchor = (x, y, text_matrix, ctm)
        if anchor != adv_anchor:
            adv_anchor, adv_pen = anchor, 0.0
        step = scale_x * gs.h_scale
        metrics = (font_metrics or {}).get(gs.font, _FALLBACK_METRICS)
        left, right, pen, overprint = _extent_units(
            pieces, metrics, size, gs.char_spacing, gs.word_spacing
        )
        left_end = px + (adv_pen + left) * step
        right_end = px + (adv_pen + right) * step
        adv_pen += pen
        if not text.strip():
            return
        if overprint:
            marks.add("glyph_overprint_suspected")
        if gs.render_mode in (3.0, 7.0):
            marks.add("hidden_text_present")
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
        raised = y + gs.rise * text_matrix[3]
        rx = ctm[0] * x + ctm[2] * raised + ctm[4]
        ry = ctm[1] * x + ctm[3] * raised + ctm[5]
        glyph = size * scale_y
        # The baseline origin and the glyph box must lie inside the page: an origin at the top
        # edge puts the whole glyph above it, and a long string can run off either side.
        if (
            page_box is None
            or any(
                not (
                    page_box[0] <= cx
                    and cx <= page_box[2]
                    and page_box[1] <= cy
                    and cy + glyph <= page_box[3]
                )
                for cx, cy in ((px, py), (rx, ry))
            )
            or left_end < page_box[0]
            or right_end > page_box[2]
        ):
            marks.add("text_outside_page")
        # A clip can only exclude text that falls outside it (or whose region is unknown).
        if gs.clip_unknown or (
            gs.clip is not None
            and not (
                gs.clip[0] <= left_end
                and right_end <= gs.clip[2]
                and gs.clip[1] <= py
                and py + 0.7 * glyph <= gs.clip[3]
            )
        ):
            marks.add("clip_region_present")
        text_boxes.append((left_end, py, right_end, py + glyph))
        out.append(_Fragment(px, py, text, gs.size * abs(ctm[3] or 1.0), gs.font))

    def clip_to_path() -> None:
        """Intersect the clip with the current path when it is a single axis-aligned rect."""
        nonlocal gs
        a, b, c, d, e, f = gs.ctm
        if len(rects) != 1 or path_other or b != 0 or c != 0:
            gs = replace(gs, clip_unknown=True)
            return
        rx, ry, rw, rh = rects[0]
        xs = sorted((a * rx + e, a * (rx + rw) + e))
        ys = sorted((d * ry + f, d * (ry + rh) + f))
        box = (xs[0], ys[0], xs[1], ys[1])
        if gs.clip is not None:
            box = (
                max(box[0], gs.clip[0]),
                max(box[1], gs.clip[1]),
                min(box[2], gs.clip[2]),
                min(box[3], gs.clip[3]),
            )
        gs = replace(gs, clip=box)

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
        if kind == "num" and not math.isfinite(value):
            marks.add("content_operand_non_finite")  # a viewer cannot place it; neither can we
            value = 0.0
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
        elif op == b"Tc" and nums:
            gs = replace(gs, char_spacing=nums[-1])
        elif op == b"Tw" and nums:
            gs = replace(gs, word_spacing=nums[-1])
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
            clip_to_path()
        elif op in _PAINT:
            rects.clear()
            path_other = False
        elif op == b"Do" and stack and isinstance(stack[-1], bytes):
            if stack[-1] in forms:
                marks.add("form_xobject_text_unread")
            elif stack[-1] in images:
                a, b, c, d, e, f = gs.ctm
                xs = (e, e + a, e + c, e + a + c)  # corners of the unit square under the CTM
                ys = (f, f + b, f + d, f + b + d)
                image_boxes.append((min(xs), min(ys), max(xs), max(ys)))
        elif op == b"TL" and nums:
            gs = replace(gs, leading=nums[-1])
        elif op == b"T*":
            ly -= gs.leading
            x, y = lx, ly
        elif op == b"Tj" and stack and isinstance(stack[-1], bytes):
            emit(_show(stack[-1]), [stack[-1]])
        elif op in (b"'", b'"') and stack and isinstance(stack[-1], bytes):
            if op == b'"' and len(nums) >= 2:  # aw ac string ": word, then character spacing
                gs = replace(gs, word_spacing=nums[-2], char_spacing=nums[-1])
            ly -= gs.leading
            x, y = lx, ly
            emit(_show(stack[-1]), [stack[-1]])
        elif op == b"TJ" and stack and isinstance(stack[-1], list):
            parts = []
            pieces: list[bytes | float] = []
            for item in stack[-1]:
                if isinstance(item, bytes):
                    parts.append(_show(item))
                    pieces.append(item)
                elif isinstance(item, float):
                    pieces.append(item)
                    if item < -200:
                        parts.append(" ")
            emit("".join(parts), pieces)
        stack.clear()
    if out:
        if any(_image_may_conceal(box, page_box, text_boxes) for box in image_boxes):
            late.add("image_text_overlay_unverified")
        marks |= late
    return out, marks


_STANDARD_TEXT_FONTS = frozenset(
    {
        "Courier", "Courier-Bold", "Courier-BoldOblique", "Courier-Oblique",
        "Helvetica", "Helvetica-Bold", "Helvetica-BoldOblique", "Helvetica-Oblique",
        "Times-Roman", "Times-Bold", "Times-BoldItalic", "Times-Italic",
    }
)  # fmt: skip


def _font_ok(fonts: dict[bytes, dict[str, object]], name: bytes, text: str) -> bool:
    """Only a font whose byte-to-character mapping is provably standard is trusted."""
    font = fonts.get(name)
    if font is None:
        return False  # unresolved or unreadable font resource: the encoding cannot be known
    subtype = font.get("Subtype")
    if subtype not in _SIMPLE_SUBTYPES:
        return False  # Type0, Type3 and anything unrecognised
    if "Differences" in font or "ToUnicode" in font:
        return False
    encoding = font.get("Encoding")
    if encoding is not None and not isinstance(encoding, _Name):
        return False  # inline or indirect encoding dictionary, or a value of the wrong type
    if encoding == "WinAnsiEncoding":
        return all(ord(char) >= 32 and char not in "\x7f�" for char in text)
    standard_subset = all(32 <= ord(char) <= 126 and char not in "'`" for char in text)
    if encoding is not None:
        # This reader decodes with WinAnsi. Only shared ASCII glyphs may be trusted
        # when a different supported font encoding is declared.
        return encoding in {"MacRomanEncoding", "StandardEncoding"} and standard_subset
    # Default font encodings are only known for the unembedded standard text fonts.
    return (
        standard_subset
        and subtype == "Type1"
        and "FontDescriptor" not in font
        and font.get("BaseFont") in _STANDARD_TEXT_FONTS
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
    doc = _Doc(objects, deadline)
    catalog: dict[str, object] | None = None
    for candidate in (o for o in objects.values() if re.search(rb"/Type\s*/Catalog", o.head)):
        value = doc.value(candidate.number)
        if candidate.number in doc.bad:
            raise ReaderFailure(ExtractionState.CORRUPT, "ambiguous_page_tree", NextActor.OWNER)
        if isinstance(value, dict) and value.get("Type") == "Catalog":
            catalog = value
            break
    if catalog is None:
        reason = "pdf_object_streams" if b"/ObjStm" in data else "no_catalog"
        state = ExtractionState.UNSUPPORTED if b"/ObjStm" in data else ExtractionState.CORRUPT
        raise ReaderFailure(state, reason, NextActor.OWNER)
    root = catalog.get("Pages")
    if not isinstance(root, _Ref):
        raise ReaderFailure(ExtractionState.CORRUPT, "no_page_tree", NextActor.OWNER)

    pages: list[
        tuple[dict[str, object], dict[str, object], tuple[float, float, float, float] | None, bool]
    ] = []
    seen: set[int] = set()

    def walk(
        number: int, inherited: dict[str, object], boxes: _Boxes, bad: bool, depth: int
    ) -> None:
        if number in seen or number not in objects:
            raise ReaderFailure(ExtractionState.CORRUPT, "bad_page_tree", NextActor.OWNER)
        if depth > limits.max_xml_depth:
            raise ReaderFailure(
                ExtractionState.OVER_DEPTH, "page_tree_depth", NextActor.OWNER, limit="max_depth"
            )
        seen.add(number)
        doc.touched = set()
        node = doc.value(number)
        if number in doc.bad or not isinstance(node, dict):
            raise ReaderFailure(ExtractionState.CORRUPT, "ambiguous_page_tree", NextActor.OWNER)
        resources = (doc.dict(node["Resources"]) or {}) if "Resources" in node else inherited
        boxes = _Boxes(
            _box(node.get("MediaBox"), doc) or boxes.media,
            _box(node.get("CropBox"), doc) or boxes.crop,
        )
        bad = bad or doc.ambiguous()
        if node.get("Type") == "Pages":
            kids = doc.resolve(node.get("Kids"))
            for kid in kids if isinstance(kids, list) else []:
                if not isinstance(kid, _Ref):
                    raise ReaderFailure(ExtractionState.CORRUPT, "bad_page_tree", NextActor.OWNER)
                walk(kid.number, resources, boxes, bad, depth + 1)
        else:
            pages.append((node, resources, boxes.visible(), bad))
            if len(pages) > limits.max_pages:
                raise ReaderFailure(
                    ExtractionState.OVERSIZED, "page_limit", NextActor.OWNER, limit="pages"
                )

    walk(root.number, {}, _Boxes(None, None), False, 0)
    spans: list[Span] = []
    image_pages: list[int] = []
    unresolved_pages: list[int] = []
    reading_order = 0
    for page_number, (page, resources, page_box, page_bad) in enumerate(pages, start=1):
        deadline.check()
        doc.touched = set()
        fonts = _named(resources, "Font", doc)
        xobjects = {
            name: doc.value(ref.number)
            for name, ref in (doc.dict(resources.get("XObject")) or {}).items()
            if isinstance(ref, _Ref)
        }
        images = frozenset(
            name.encode("latin-1")
            for name, head in xobjects.items()
            if isinstance(head, dict) and head.get("Subtype") == "Image"
        )
        forms = frozenset(
            name.encode("latin-1")
            for name, head in xobjects.items()
            if isinstance(head, dict) and head.get("Subtype") == "Form"
        )
        contents = page.get("Contents")
        if isinstance(contents, _Ref) and objects.get(contents.number, _NO_STREAM).stream is None:
            target = doc.value(contents.number)  # an indirect array of content streams
            contents = target if isinstance(target, list) else contents
        refs = contents if isinstance(contents, list) else [] if contents is None else [contents]
        readable = [
            ref
            for ref in refs
            if isinstance(ref, _Ref)
            and ref.number in objects
            and objects[ref.number].stream is not None
            and isinstance(doc.value(ref.number), dict)
        ]
        stream = b"\n".join(
            _decode(objects[ref.number], doc.dict(ref), doc, limits) for ref in readable
        )
        metrics = {name: _font_metrics(font, doc) for name, font in fonts.items()}
        fragments, marks = _fragments(stream, deadline, forms, images, page_box, metrics)
        if len(readable) != len(refs):
            # A referenced content stream that cannot be resolved means the page was not read.
            marks.add("content_stream_unresolved")
        if not fragments:
            if "content_stream_unresolved" in marks:
                unresolved_pages.append(page_number)
            if images or marks:
                image_pages.append(page_number)
            continue
        flagged_font = any(not _font_ok(fonts, f.font, f.text) for f in fragments)
        if any(_widths_unavailable(fonts.get(f.font), doc) for f in fragments):
            marks.add("font_widths_unavailable")
        if page_bad or doc.ambiguous():
            marks.add("pdf_object_ambiguous")
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
                "pdf_content_stream_unresolved"
                if unresolved_pages
                else "image_only_pdf_needs_authorized_ocr_rendition",
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


def _box(value: object, doc: _Doc) -> tuple[float, float, float, float] | None:
    """A rectangle such as `/MediaBox [0 0 612 792]`, inline or indirect; None if malformed."""
    box = doc.resolve(value)
    if not isinstance(box, list) or len(box) != 4 or not all(_is_number(v) for v in box):
        return None
    x0, y0, x1, y1 = (float(v) for v in box)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _named(resources: dict[str, object], key: str, doc: _Doc) -> dict[bytes, dict[str, object]]:
    """A resource category (/Font) as name -> dictionary; unreadable entries are left out."""
    out: dict[bytes, dict[str, object]] = {}
    for name, value in (doc.dict(resources.get(key)) or {}).items():
        entry = doc.dict(value)
        if entry is not None:
            out[name.encode("latin-1")] = entry
    return out
