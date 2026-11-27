"""Generated sentinel fixture for OPE-531: twelve invented leaf entries plus a generated MCP.

Everything here is invented. Names use the `generated-` marker, hosts use `.invalid`. The MCP
server below is a generated connector transport; it is NOT an installed customer connector and
proves no native integration coverage.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Any

from opentour_cell.pilot.mixed_intake.adapters import AdapterError, Capabilities

Json = dict[str, Any]
TENANT = "generated-tenant-a"
CONNECTION = "generated-connection-1"
ROOT = "generated-root"
AS_OF = "2026-01-15T00:00:00Z"
RESTRICTED_NAME = "generated-restricted-name-sentinel"
OUTSIDE_TARGET = "generated-outside-grant-target"

MARKDOWN_POLICY = (
    b"# Refund policy (generated)\n"
    b"\n"
    b"Refunds up to 500 are approved by role-a.\n"
    b"Refunds above 500 need a second approver.\n"
    b"\n"
    b"Case identifiers keep leading zeroes, for example 00417.\n"
)
NATIVE_EXPORT_MARKDOWN = (
    b"# Native SOP export (generated)\n\nEscalate disputes within 02 business days.\n"
)
CSV_DECISIONS = (
    b"case_id,decided_at,decision,amount\n"
    b"00417,2026-01-10T09:30:00+00:00,approve,0450\n"
    b"00418,2026-01-11T14:05:00+00:00,escalate,1200\n"
)
JSONL_OUTCOMES = (
    b'{"case_id":"00417","verified_at":"2026-01-12T00:00:00Z","outcome":{"label":"upheld"}}\n'
    b'{"case_id":"00418","verified_at":"2026-01-13T00:00:00Z","outcome":{"label":"reversed"}}\n'
)
ENCRYPTED = (
    bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 24 + "EncryptedPackage".encode("utf-16-le")
)


def unsupported_bytes() -> bytes:
    out, block = b"", b"generated-unsupported-seed"
    while len(out) < 256:
        block = hashlib.sha256(block).digest()
        out += block
    return b"\x7fGEN" + out


def _zip(parts: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, payload in parts.items():
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            archive.writestr(info, payload)
    return buffer.getvalue()


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/'
    b'package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>'
)


def _p(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def build_docx() -> bytes:
    rows = [("Step", "Owner", "Limit"), ("01", "role-a", "0500"), ("02", "role-b", "0750")]
    table = (
        "<w:tbl>"
        + "".join(
            "<w:tr>" + "".join(f"<w:tc>{_p(cell)}</w:tc>" for cell in row) + "</w:tr>"
            for row in rows
        )
        + "</w:tbl>"
    )
    link = (
        '<w:p><w:hyperlink r:id="rId9"><w:r><w:t>see linked note</w:t></w:r></w:hyperlink>'
        '<w:r><w:instrText> HYPERLINK "https://example.invalid/x" </w:instrText></w:r></w:p>'
    )
    body = (
        _p("Procedure P-17 (generated)")
        + _p("Step 01 applies when the case id is 00417.")
        + link
        + table
    )
    document = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W_NS}" '
        f'xmlns:r="{R_NS}"><w:body>{body}</w:body></w:document>'
    ).encode()
    rels = (
        b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
        b'2006/relationships"><Relationship Id="rId9" TargetMode="External" '
        b'Target="https://example.invalid/x"/></Relationships>'
    )
    return _zip(
        {
            "[Content_Types].xml": CONTENT_TYPES,
            "word/document.xml": document,
            "word/_rels/document.xml.rels": rels,
        }
    )


S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def build_xlsx() -> bytes:
    strings = ["Case", "Amount", "Opened", "Doubled", "00417"]
    shared = (
        f'<?xml version="1.0"?><sst xmlns="{S_NS}">'
        + "".join(f"<si><t>{s}</t></si>" for s in strings)
        + "</sst>"
    ).encode()
    sheet = (
        f'<?xml version="1.0"?><worksheet xmlns="{S_NS}"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
        '<c r="C1" t="s"><v>2</v></c><c r="D1" t="s"><v>3</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>4</v></c><c r="B2"><v>1250</v></c>'
        '<c r="C2" s="1"><v>46037</v></c><c r="D2"><f>B2*2</f><v>2500</v></c></row>'
        "</sheetData></worksheet>"
    ).encode()
    workbook = (
        f'<?xml version="1.0"?><workbook xmlns="{S_NS}" xmlns:r="{R_NS}"><sheets>'
        '<sheet name="Quotes" sheetId="1" r:id="rId1"/></sheets></workbook>'
    ).encode()
    rels = (
        b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
        b'2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
        b"</Relationships>"
    )
    styles = (
        f'<?xml version="1.0"?><styleSheet xmlns="{S_NS}"><cellXfs count="2">'
        '<xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>'
    ).encode()
    return _zip(
        {
            "[Content_Types].xml": CONTENT_TYPES,
            "xl/workbook.xml": workbook,
            "xl/_rels/workbook.xml.rels": rels,
            "xl/worksheets/sheet1.xml": sheet,
            "xl/sharedStrings.xml": shared,
            "xl/styles.xml": styles,
        }
    )


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _content(items: list[tuple[float, float, str]]) -> bytes:
    ops = ["BT", "/F1 11 Tf"]
    for x, y, text in items:
        ops.append(f"1 0 0 1 {x} {y} Tm ({_esc(text)}) Tj")
    ops.append("ET")
    return "\n".join(ops).encode("latin-1")


def _assemble(objects: dict[int, bytes], trailer_extra: str = "") -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    xref = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    trailer = f"trailer\n<< /Size {size} /Root 1 0 R {trailer_extra}>>\n"
    out += (trailer + f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def _stream(payload: bytes, flate: bool = False) -> bytes:
    if flate:
        payload = zlib.compress(payload, 6)
        return (
            f"<< /Length {len(payload)} /Filter /FlateDecode >>\nstream\n".encode()
            + payload
            + b"\nendstream"
        )
    return f"<< /Length {len(payload)} >>\nstream\n".encode() + payload + b"\nendstream"


def build_text_pdf(flate: bool = False) -> bytes:
    page1 = [
        (72, 720, "Exception EX-0042 (generated)"),
        (72, 705, "Refunds above the regional limit need a second approver."),
        (72, 640, "Region"),
        (200, 640, "Limit"),
        (330, 640, "Approver"),
        (440, 640, "Code"),
        (72, 624, "North"),
        (200, 624, "500"),
        (330, 624, "role-b"),
        (440, 624, "007"),
        (72, 608, "South"),
        (200, 608, "750"),
        (330, 608, "role-c"),
        (440, 608, "012"),
    ]
    page1.reverse()  # content-stream order differs from reading order on purpose
    page2 = [(72, 720, "Appendix: exceptions expire after 30 days.")]
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R "
        b"/Resources << /Font << /F1 7 0 R >> >> >>",
        4: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 6 0 R "
        b"/Resources << /Font << /F1 7 0 R >> >> >>",
        5: _stream(_content(page1), flate),
        6: _stream(_content(page2), flate),
        7: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    return _assemble(objects)


def build_image_pdf() -> bytes:
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /XObject << /Im1 5 0 R >> >> >>",
        4: _stream(b"q 612 0 0 792 0 0 cm /Im1 Do Q"),
        5: b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Length 4 >>\nstream\n"
        + struct.pack("4B", 0, 255, 255, 0)
        + b"\nendstream",
    }
    return _assemble(objects)


def build_encrypted_pdf() -> bytes:
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [] /Count 0 >>",
        3: b"<< /Filter /Standard /V 2 /R 3 /O (generated) /U (generated) /P -4 >>",
    }
    return _assemble(objects, "/Encrypt 3 0 R ")


FILES: dict[str, bytes] = {
    "policies/generated-policy.md": MARKDOWN_POLICY,
    "archive/generated-policy-copy.md": MARKDOWN_POLICY,
    "policies/generated-exception.pdf": build_text_pdf(),
    "procedures/generated-procedure.docx": build_docx(),
    "procedures/generated-sop.native": b"GENERATED-NATIVE-DOCUMENT-STUB\n",
    "procedures/generated-sop-export.md": NATIVE_EXPORT_MARKDOWN,
    "streams/generated-decisions.csv": CSV_DECISIONS,
    "streams/generated-outcomes.jsonl": JSONL_OUTCOMES,
    "streams/generated-workbook.xlsx": build_xlsx(),
    "restricted/generated-scan.pdf": build_image_pdf(),
    "restricted/generated-locked.docx": ENCRYPTED,
    "restricted/generated-blob.bin": unsupported_bytes(),
}

ROLES = {
    "generated-n01": "policy",
    "generated-n02": "policy",
    "generated-n03": "policy",
    "generated-n04": "policy",
    "generated-n05": "observed_decision",
    "generated-n06": "independent_final",
    "generated-n07": "example",
    "generated-n08": "policy",
}

# (id, parent, name, kind, relative path)
FOLDERS = [
    ("generated-f-pol", "generated-policies"),
    ("generated-f-arc", "generated-archive"),
    ("generated-f-proc", "generated-procedures"),
    ("generated-f-str", "generated-streams"),
    ("generated-f-res", "generated-restricted"),
]
LEAVES = [
    (
        "generated-n01",
        "generated-f-pol",
        "generated-policy.md",
        "file",
        "policies/generated-policy.md",
    ),
    (
        "generated-n02",
        "generated-f-arc",
        "generated-policy-copy.md",
        "file",
        "archive/generated-policy-copy.md",
    ),
    (
        "generated-n03",
        "generated-f-pol",
        "generated-exception.pdf",
        "file",
        "policies/generated-exception.pdf",
    ),
    (
        "generated-n04",
        "generated-f-proc",
        "generated-procedure.docx",
        "file",
        "procedures/generated-procedure.docx",
    ),
    (
        "generated-n05",
        "generated-f-str",
        "generated-decisions.csv",
        "file",
        "streams/generated-decisions.csv",
    ),
    (
        "generated-n06",
        "generated-f-str",
        "generated-outcomes.jsonl",
        "file",
        "streams/generated-outcomes.jsonl",
    ),
    (
        "generated-n07",
        "generated-f-str",
        "generated-workbook.xlsx",
        "file",
        "streams/generated-workbook.xlsx",
    ),
    (
        "generated-n08",
        "generated-f-proc",
        "generated-sop.native",
        "native",
        "procedures/generated-sop.native",
    ),
    (
        "generated-n09",
        "generated-f-res",
        "generated-scan.pdf",
        "file",
        "restricted/generated-scan.pdf",
    ),
    (
        "generated-n10",
        "generated-f-res",
        "generated-locked.docx",
        "file",
        "restricted/generated-locked.docx",
    ),
    (
        "generated-n11",
        "generated-f-res",
        "generated-blob.bin",
        "file",
        "restricted/generated-blob.bin",
    ),
]


class MixedFixture:
    """A materialized fixture directory plus its owner manifest."""

    def __init__(self, base: Path, *, opaque_denied: bool = False, delta: bool = True):
        self.base = base
        self.root = base / "export"
        self.outside = base / "outside"
        self.root.mkdir(parents=True)
        self.outside.mkdir()
        (self.outside / "target.txt").write_bytes(b"generated-outside-sentinel\n")
        self.files = dict(FILES)
        self.revisions: dict[str, str] = {}
        for rel, data in self.files.items():
            self.write(rel, data)
        os.symlink(self.outside / "target.txt", self.root / "restricted" / "generated-shortcut.lnk")
        self.write("restricted/generated-ocr.txt", b"generated ocr line one\n")
        self.opaque_denied = opaque_denied
        self.delta = delta
        self.nodes: list[Json] = []
        self.events: list[Json] = []
        self._build_nodes()

    def write(self, rel: str, data: bytes) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.files[rel] = data

    def _build_nodes(self) -> None:
        self.nodes = [
            {
                "id": ROOT,
                "kind": "folder",
                "name": "generated-root",
                "parent": None,
                "revision": "r0",
            }
        ]
        for fid, name in FOLDERS:
            node: Json = {
                "id": fid,
                "kind": "folder",
                "name": name,
                "parent": ROOT,
                "revision": "r0",
            }
            if fid == "generated-f-str":
                node["links"] = [ROOT]  # a folder cycle edge back to an ancestor
            self.nodes.append(node)
        for nid, parent, name, kind, rel in LEAVES:
            node = {
                "id": nid,
                "kind": kind,
                "name": name,
                "parent": parent,
                "path": rel,
                "revision": f"generated-rev-{nid[-2:]}-1",
                "size": len(self.files[rel]),
            }
            if kind == "native":
                node["native"] = {
                    "revision": "generated-native-rev-1",
                    "digest": "generated-native-digest-1",
                }
                node["renditions"] = [
                    {
                        "format": "markdown",
                        "path": "procedures/generated-sop-export.md",
                        "revision": "generated-export-rev-1",
                    }
                ]
            if nid == "generated-n09":
                node["renditions"] = [
                    {
                        "format": "ocr-text",
                        "path": "restricted/generated-ocr.txt",
                        "revision": "generated-ocr-rev-1",
                    }
                ]
            self.nodes.append(node)
        shortcut: Json = {
            "id": "generated-n12",
            "kind": "shortcut",
            "name": "generated-shortcut.lnk",
            "parent": "generated-f-res",
            "path": "restricted/generated-shortcut.lnk",
            "target": OUTSIDE_TARGET,
            "revision": "generated-rev-12-1",
        }
        if self.opaque_denied:
            shortcut = {
                "id": "generated-n12",
                "kind": "shortcut",
                "name": RESTRICTED_NAME,
                "parent": "generated-f-res",
                "access": "denied",
                "opaque": True,
                "revision": "generated-rev-12-1",
            }
        self.nodes.append(shortcut)

    def node(self, nid: str) -> Json:
        return next(n for n in self.nodes if n["id"] == nid)

    def edit(self, nid: str, data: bytes, revision: str) -> None:
        node = self.node(nid)
        self.write(node["path"], data)
        node["revision"], node["size"] = revision, len(data)
        self.events.append({"seq": len(self.events) + 1, "upsert": nid})

    def delete(self, nid: str, *, tombstone: bool) -> None:
        node = self.node(nid)
        self.nodes.remove(node)
        (self.root / node["path"]).unlink()
        if tombstone:
            self.events.append(
                {"seq": len(self.events) + 1, "tombstone": nid, "revision": node["revision"]}
            )

    def rename(self, nid: str, name: str, path: str) -> None:
        node = self.node(nid)
        (self.root / node["path"]).rename(self.root / path)
        node["name"], node["path"] = name, path

    def add_leaf(self, nid: str, parent: str, name: str, rel: str, data: bytes) -> None:
        self.write(rel, data)
        self.nodes.append(
            {
                "id": nid,
                "kind": "file",
                "name": name,
                "parent": parent,
                "path": rel,
                "revision": f"generated-rev-{nid}-1",
                "size": len(data),
            }
        )
        self.events.append({"seq": len(self.events) + 1, "upsert": nid})

    def manifest(self) -> Json:
        return {
            "schema_version": "opentour.mixed-intake.manifest.v1",
            "tenant": TENANT,
            "connection": CONNECTION,
            "root_id": ROOT,
            "access_epoch": 3,
            "as_of": AS_OF,
            "capabilities": {
                "list": True,
                "read": True,
                "version": True,
                "pagination": True,
                "delta": self.delta,
                "deletion": self.delta,
                "permission": True,
                "approved_rendition": True,
            },
            "nodes": self.nodes,
            "change_feed": {"events": self.events, "complete": True},
        }

    def write_manifest(self, name: str = "manifest.json") -> Path:
        path = self.base / name
        path.write_text(json.dumps(self.manifest(), indent=1), encoding="utf-8")
        return path


class GeneratedMcpServer:
    """Generated MCP resource server over the same manifest. Records every response."""

    def __init__(self, fixture: MixedFixture, *, page_size: int = 3, snapshot_only: bool = False):
        self.fixture = fixture
        self.page_size = page_size
        self.snapshot_only = snapshot_only
        self.log: list[str] = []
        self.reads: dict[str, int] = {}
        self.methods: list[str] = []
        self.target_reads = 0

    def _node(self, uri: str) -> Json:
        for node in self.fixture.nodes:
            if node["id"] == uri:
                return node
        raise AdapterError("object_not_readable")

    @staticmethod
    def _item(node: Json) -> Json:
        denied = node.get("access", "granted") != "granted"
        meta: Json = {
            "kind": "unknown" if denied and node.get("opaque") else node["kind"],
            "parent": node.get("parent"),
            "revision": node.get("revision", ""),
            "access": "denied" if denied else "granted",
            "opaque": bool(node.get("opaque")),
        }
        if denied:
            return {"uri": node["id"], "_meta": meta}
        meta.update(
            path=node.get("path"),
            target=node.get("target"),
            renditions=[
                {"format": r["format"], "revision": r["revision"]}
                for r in node.get("renditions", ())
            ],
            original=node.get("native"),
        )
        return {"uri": node["id"], "name": node["name"], "size": node.get("size"), "_meta": meta}

    def request(self, method: str, params: Json) -> Json:
        self.methods.append(method)
        response = self._dispatch(method, params)
        self.log.append(json.dumps(response, sort_keys=True))
        return response

    def _dispatch(self, method: str, params: Json) -> Json:
        nodes = self.fixture.nodes
        if method == "opentour/capabilities":
            caps = self.fixture.manifest()["capabilities"]
            if self.snapshot_only:
                caps = {**caps, "delta": False, "deletion": False}
            return {
                "tenant": TENANT,
                "root": ROOT,
                "accessEpoch": 3,
                "asOf": AS_OF,
                "capabilities": caps,
            }
        if method == "resources/list":
            parent = params["parent"]
            children = [n for n in nodes if n.get("parent") == parent]
            links = next((n.get("links", []) for n in nodes if n["id"] == parent), [])
            children += [n for n in nodes if n["id"] in links]
            start = int(params["cursor"] or 0)
            size = min(int(params["limit"]), self.page_size)
            chunk = children[start : start + size]
            nxt = str(start + size) if start + size < len(children) else None
            return {"resources": [self._item(n) for n in chunk], "nextCursor": nxt}
        if method == "resources/read":
            node = self._node(params["uri"])
            uri = node["id"]
            if node.get("access", "granted") != "granted" or node["kind"] == "shortcut":
                raise AdapterError("object_not_readable")
            if uri == OUTSIDE_TARGET:
                self.target_reads += 1
            self.reads[uri] = self.reads.get(uri, 0) + 1
            if "rendition" in params:
                rend = next(r for r in node["renditions"] if r["format"] == params["rendition"])
                data = (self.fixture.root / rend["path"]).read_bytes()
                revision = rend["revision"]
            else:
                data = (self.fixture.root / node["path"]).read_bytes()
                revision = node["revision"]
            return {
                "contents": [
                    {
                        "uri": uri,
                        "blob": base64.b64encode(data).decode(),
                        "_meta": {"revision": revision},
                    }
                ]
            }
        if method == "opentour/changes":
            since = int(params["since"] or 0)
            events = [e for e in self.fixture.events if e["seq"] > since]
            last = max([since, *(e["seq"] for e in events)])
            return {
                "upserts": [self._item(self._node(e["upsert"])) for e in events if "upsert" in e],
                "tombstones": [
                    {"uri": e["tombstone"], "revision": e["revision"]}
                    for e in events
                    if "tombstone" in e
                ],
                "nextToken": str(last),
                "complete": True,
            }
        raise AdapterError("unknown_method")


def capability_matrix(caps: Capabilities) -> Json:
    return caps.matrix()
