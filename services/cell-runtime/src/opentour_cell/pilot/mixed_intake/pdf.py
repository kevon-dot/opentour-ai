"""Minimal text-bearing PDF reader: page + passage citations in positional reading order.

Scope is deliberately narrow and honest. It reads classic cross-reference PDFs with simple
(single-byte) fonts and Flate or unfiltered content streams. Anything else is reported with a
specific state (object streams, composite fonts, other filters) instead of guessed. Encrypted
files report LOCKED. Image-only pages report NEEDS_RENDITION. Nothing is executed or fetched.

Fail-closed review routing: an unresolved or non-standard font encoding, rotated or skewed
text, invisible text and text inside Form XObjects each flag the page for review rather than
emitting a guess. Objects are scanned directly (the cross-reference table is not consulted).

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
# a small opaque image placed over text conceals it. Fragment extents come from glyph widths,
# Tj advance and Tc (see UNKNOWN_FONT_TIGHT_FACTOR below), so overlap is approximate.
IMAGE_REVIEW_PAGE_FRACTION = 0.25
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


# Text extents are estimated from per-glyph advance widths (1000 units per em) of the standard
# fonts, which are published data (Adobe core-14 AFM). An over-wide estimate makes text and an
# image placed side by side (letterheads, two-column layouts) look overlapping and flags them
# falsely; an under-wide one lets an image over the tail of wide glyphs through. Real advances
# keep both errors small. A font with no table here (embedded or non-standard fonts, Times
# bold and italic, composite fonts) falls back to GENEROUS widths for image overlap
# (Helvetica-Bold, wider than most body faces, so side-by-side layouts in such fonts may flag
# falsely) and to UNKNOWN_FONT_TIGHT_FACTOR x Helvetica for the page-edge and clip checks
# (so a long run of very wide glyphs in such a font can still escape those two checks).
# Known bounded gaps: Times-Bold "W" is 1000 units against Helvetica-Bold's 944, so an image over
# the tail of an all-wide-glyph run in Times-Bold can pass the fallback; characters outside
# printable ASCII use a per-table default (556 Helvetica, 500 Times, 600 Courier), which slightly
# under-estimates an em dash or ellipsis (1000 units); TJ kerning and Tw are ignored. Family
# names are matched exactly so wider or narrower faces (Arial-Black, ArialNarrow) never borrow
# the Helvetica table.
UNKNOWN_FONT_TIGHT_FACTOR = 0.8
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
_GLYPH_WIDTHS: dict[str, tuple[dict[str, int], int]] = {
    "helvetica": (dict(zip(_ASCII, _HELVETICA, strict=True)), 556),
    "helvetica-bold": (dict(zip(_ASCII, _HELVETICA_BOLD, strict=True)), 556),
    "times": (dict(zip(_ASCII, _TIMES_ROMAN, strict=True)), 500),
    "courier": ({}, 600),  # monospaced: every glyph, whatever its code, advances 600
}


def _width_table_for(font: _Object | None) -> str | None:
    """Name the width table of a simple standard-metric font, or None when there is none."""
    if font is None or not re.search(rb"/Subtype\s*/(Type1|TrueType|MMType1)\b", font.head):
        return None
    base = re.search(rb"/BaseFont\s*/([^\s/<>\[\]()]+)", font.head)
    if base is None:
        return None
    name = re.sub(r"^[A-Z]{6}\+", "", base[1].decode("latin-1")).lower().replace(",", "-")
    styled = any(word in name for word in ("bold", "italic", "oblique"))
    if name.startswith("courier"):
        return "courier"
    # Exact family names only: Arial-Black (wider), ArialNarrow, Helvetica-Light, -Condensed and
    # the like have different metrics and must take the generous fallback, never this table.
    if re.fullmatch(r"(helvetica|arial)-?(bold)?(oblique|italic)?(mt)?", name):
        return "helvetica-bold" if "bold" in name else "helvetica"
    if name in {"times-roman", "timesnewroman", "timesnewromanpsmt"} and not styled:
        return "times"
    return None


def _extent_units(text: str, table: str | None) -> tuple[float, float]:
    """(generous, tight) advance of `text` in 1/1000 em, from the real glyph widths."""

    def total(name: str) -> int:
        glyphs, default = _GLYPH_WIDTHS[name]
        return sum(glyphs.get(char, default) for char in text)

    if table is not None:
        known = float(total(table))
        return known, known
    return float(total("helvetica-bold")), UNKNOWN_FONT_TIGHT_FACTOR * total("helvetica")


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
    font_tables: dict[bytes, str | None] | None = None,
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
    # Anchor of the current positioning and the advance of the shows since it.
    adv_anchor: tuple[object, ...] = ()
    adv_wide = adv_tight = 0.0

    def emit(text: str) -> None:
        nonlocal adv_anchor, adv_wide, adv_tight
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
        glyph = size * scale_y
        # Several shows after one positioning operator advance along the line.
        anchor = (x, y, text_matrix, ctm)
        if anchor != adv_anchor:
            adv_anchor, adv_wide, adv_tight = anchor, 0.0, 0.0
        step = scale_x * gs.h_scale

        units_wide, units_tight = _extent_units(text, (font_tables or {}).get(gs.font))

        def run(units: float) -> float:
            return max(units / 1000.0 * size + len(text) * gs.char_spacing, 0.0) * step

        wide_end = px + adv_wide + run(units_wide)
        tight_end = px + adv_tight + run(units_tight)
        adv_wide += run(units_wide)
        adv_tight += run(units_tight)
        # The baseline origin and the glyph box must lie inside the page: an origin at the top
        # edge puts the whole glyph above it, and a long string can run off the right edge.
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
            or tight_end > page_box[2]
        ):
            marks.add("text_outside_page")
        # A clip can only exclude text that falls outside it (or whose region is unknown).
        if gs.clip_unknown or (
            gs.clip is not None
            and not (
                gs.clip[0] <= px
                and tight_end <= gs.clip[2]
                and gs.clip[1] <= py
                and py + 0.7 * glyph <= gs.clip[3]
            )
        ):
            marks.add("clip_region_present")
        text_boxes.append((px, py, wide_end, py + glyph))
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
        if any(_image_may_conceal(box, page_box, text_boxes) for box in image_boxes):
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
    unresolved_pages: list[int] = []
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
        tables = {name: _width_table_for(obj) for name, obj in fonts.items()}
        fragments, marks = _fragments(stream, deadline, forms, frozenset(images), page_box, tables)
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
