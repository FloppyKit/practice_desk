"""Tiny After-Visit Summary PDF. No extra deps. No PHI in logs."""
from __future__ import annotations


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", "")
    )


def _wrap(text: str, width: int = 86) -> list[str]:
    lines: list[str] = []
    for raw in (text or "").replace("\r", "").split("\n"):
        raw = raw.strip()
        if not raw:
            lines.append("")
            continue
        while len(raw) > width:
            cut = raw.rfind(" ", 0, width)
            if cut < 20:
                cut = width
            lines.append(raw[:cut].strip())
            raw = raw[cut:].strip()
        if raw:
            lines.append(raw)
    return lines or [""]


# Patient-facing AVS order (January 2025 letterhead, Meet again first). S/O/A never go on it.
AVS_PATIENT_ORDER: tuple[tuple[str, str], ...] = (
    ("medications", "Medications"),
    ("supplements", "Supplements"),
    ("recommendations", "Other recommendations"),
)
CHART_ONLY = ("subjective", "objective", "assessment", "plan", "interval_update", "data", "long_run", "addendum")


def _pretty_dos(raw: str) -> str:
    from datetime import datetime

    s = (raw or "").strip()[:10]
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return s
    return f"{d.strftime('%B')} {d.day} {d.year}"


def _enc(s: str) -> bytes:
    return _esc(s).encode("cp1252", "replace")


def _letterhead_lines(practice: dict) -> tuple[str, list[str]]:
    name = str(practice.get("name") or "").strip()
    right: list[str] = []
    right += _wrap(str(practice.get("address") or "").strip(), 44)[:3] if practice.get("address") else []
    contact = "   ".join(p for p in (
        f"Tel: {practice['phone']}" if practice.get("phone") else "",
        f"Fax: {practice['fax']}" if practice.get("fax") else "",
    ) if p)
    if contact:
        right.append(contact)
    if practice.get("email"):
        right.append(str(practice["email"]))
    lic = " · ".join(str(x) for x in practice.get("licenses") or [] if str(x).strip())
    if lic:
        right += _wrap("Licenses: " + lic, 60)[:2]
    if practice.get("npi"):
        right.append("NPI " + str(practice["npi"]))
    return name, [r for r in right if r]


def after_visit_pdf(
    fields: dict[str, str],
    sections: list[dict] | None = None,
    title: str = "",
    letterhead: dict | None = None,
    *,
    patient: bool = False,
    logo=None,
) -> bytes:
    """AVS / note PDF on the practice letterhead (multi-page, WinAnsi text, optional uploaded logo).

    patient=True: the patient-facing AVS (Meet again, identity, Medications, Supplements,
    Other recommendations). Chart-only sections (S/O/A, plan, interval update) are never printed on it.
    """
    practice = letterhead or {}
    head = title or "After Visit Summary"
    physician = fields.get("physician") or practice.get("clinician") or "Matthew Brown, DO"
    blocks: list[tuple[str, str]] = []
    if patient:
        if (fields.get("return_visit") or "").strip():
            blocks.append(("Meet again", fields.get("return_visit") or ""))
        blocks += [("Name", fields.get("name") or ""), ("Date of Birth", fields.get("dob") or ""),
                   ("Physician", physician)]
        for fid, label in AVS_PATIENT_ORDER:
            if (fields.get(fid) or "").strip():
                blocks.append((label, fields.get(fid) or ""))
    else:
        blocks += [("Name", fields.get("name") or ""), ("Date of Birth", fields.get("dob") or ""),
                   ("Physician", physician)]
        if sections:
            for sec in sections:
                sid = str(sec.get("id") or "")
                if sid in ("name", "dob", "physician", "dos"):
                    continue
                blocks.append((str(sec.get("label") or sid), fields.get(sid) or ""))
        else:
            blocks += [
                ("Meet again", fields.get("return_visit") or ""),
                ("Subjective", fields.get("subjective") or ""),
                ("Objective", fields.get("objective") or ""),
                ("Assessment", fields.get("assessment") or ""),
                ("Medications", fields.get("medications") or ""),
                ("Supplements", fields.get("supplements") or ""),
                ("Other recommendations", fields.get("recommendations") or ""),
            ]

    # ---- layout into pages ------------------------------------------------
    pages: list[list[str]] = [[]]
    image = None
    if logo:
        try:
            from .superbill_pdf import _image_xobject

            image = _image_xobject(logo)
        except Exception:
            image = None

    def put(x: float, y: float, text: str, size: int, bold: bool) -> None:
        font = "/F1" if bold else "/F2"
        pages[-1].append(f"BT {font} {size} Tf {x:.1f} {y:.1f} Td (" + "\x00" + text + ") Tj ET")

    # letterhead (page 1)
    name, right = _letterhead_lines(practice)
    top = 756
    lx = 72
    if image:
        iw, ih = image["w"], image["h"]
        scale = min(56.0 / ih, 150.0 / iw)
        dw, dh = iw * scale, ih * scale
        pages[-1].append(f"q {dw:.1f} 0 0 {dh:.1f} 72 {top + 12 - dh:.1f} cm /Im1 Do Q")
        lx = 72 + dw + 10
    if name:
        put(lx, top - 4, name, 14, True)
    ry = top
    for ln in right:
        put(318, ry, ln, 9, False)
        ry -= 11
    y = min(ry, top - 30) - 6
    pages[-1].append(f"0.75 G 0.6 w 72 {y:.1f} m 540 {y:.1f} l S 0 G")
    y -= 24
    put(72, y, head + (":" if patient else ""), 16, True)
    y -= 18
    sub = _pretty_dos(fields.get("dos") or "")
    dur = str(fields.get("duration") or "").strip()
    if dur.isdigit():
        sub = (sub + " • " if sub else "") + f"{dur} Min Office Visit"
    if sub:
        put(72, y, sub, 10, False)
        y -= 18

    def newpage() -> float:
        pages.append([])
        return 740

    inline = {"Name", "Date of Birth", "Physician"}
    for label, val in blocks:
        if label in inline:
            if y < 72:
                y = newpage()
            put(72, y, f"{label}:", 10, True)
            put(160, y, (val or "").strip(), 10, False)
            y -= 15
            if label == "Physician":
                y -= 6
            continue
        if y < 90:
            y = newpage()
        put(72, y, f"{label}:", 11, True)
        y -= 15
        for indent, ln, size in _layout_lines(val):
            if y < 60:
                y = newpage()
            if ln:
                put(indent, y, ln, size, False)
            y -= 13 if size >= 10 else 11
        y -= 8

    # ---- serialize ----------------------------------------------------------
    n_pages = len(pages)
    page_ids = [7 + 2 * i if image else 5 + 2 * i for i in range(n_pages)]
    # objects: 1 catalog, 2 pages, 3 font bold, 4 font regular, [5,6 = image + smask if image... ]
    objs: dict[int, bytes] = {}
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    objs[4] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    raw_image: list[bytes] = []
    xobj = b""
    if image:
        # superbill helper numbers its objects 7 (image) and 8 (soft mask); renumber to 5/6.
        for chunk in image["objects"]:
            chunk = chunk.replace(b"/SMask 8 0 R", b"/SMask 6 0 R")
            num, rest = chunk.split(b" 0 obj ", 1)
            num_i = {b"7": 5, b"8": 6}[num.strip()]
            body = rest.rsplit(b"endobj", 1)[0].rstrip()
            objs[num_i] = body
        xobj = b" /XObject << /Im1 5 0 R >>"
    kids = []
    for i, ops in enumerate(pages):
        pid = page_ids[i]
        cid = pid + 1
        kids.append(f"{pid} 0 R".encode())
        stream = b"\n".join(_op_bytes(o) for o in ops)
        objs[pid] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents " + f"{cid} 0 R".encode()
                     + b" /Resources << /Font << /F1 3 0 R /F2 4 0 R >>" + xobj + b" >> >>")
        objs[cid] = b"<< /Length " + str(len(stream)).encode() + b" >> stream\n" + stream + b"\nendstream"
    objs[2] = b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count " + str(n_pages).encode() + b" >>"
    top_id = max(objs)
    for k in range(1, top_id + 1):
        objs.setdefault(k, b"null")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for k in range(1, top_id + 1):
        offsets.append(len(out))
        out += f"{k} 0 obj ".encode() + objs[k] + b" endobj\n"
    xref = len(out)
    out += f"xref\n0 {top_id + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer << /Size {top_id + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


_BULLETS = ("•", "-", "○", "*")


def _layout_lines(val: str) -> list[tuple[float, str, int]]:
    """(x, text) lines. Bullets at 84pt; indented lines (dot-phrase text under a bullet) and
    wrapped bullet tails at 96pt; plain paragraphs at 84pt."""
    out: list[tuple[float, str, int]] = []
    for raw in (val or "").replace("\r", "").split("\n"):
        if not raw.strip():
            out.append((84, "", 10))
            continue
        nested = raw[:1] in (" ", "\t")
        s = raw.strip()
        bullet = s.startswith(_BULLETS)
        if s.startswith("* "):
            s = "• " + s[2:]
        x = 96 if nested else 84
        link = "http" in s
        size = 8 if link else 10  # links stay on one line where possible (8pt fits ~105 chars)
        width = (104 if link else 84) if nested else (106 if link else 90)
        first = True
        for piece in _wrap(s, width):
            out.append((x if first else (96 if (bullet or nested) else 84), piece, size))
            first = False
    while out and not out[-1][1]:
        out.pop()
    return out


def _op_bytes(op: str) -> bytes:
    """Ops are plain ASCII except text inside a \\x00-marked Tj string, which is escaped + WinAnsi."""
    if "\x00" not in op:
        return op.encode("latin-1", "replace")
    pre, rest = op.split("\x00", 1)
    text, post = rest.rsplit(") Tj ET", 1)
    return pre.encode("latin-1") + _enc(text) + b") Tj ET" + post.encode("latin-1")


def markdown_pdf(body: str) -> bytes:
    """Plain text-ish PDF from a rendered template."""
    lines: list[tuple[str, bool]] = []
    for raw in (body or "").replace("\r", "").split("\n"):
        s = raw.rstrip()
        if s.startswith("# "):
            lines.append((s[2:].strip(), True))
        elif s.startswith("## "):
            lines.append((s[3:].strip(), True))
        elif s.startswith("- "):
            lines.append((s[2:].strip(), False))
        else:
            for w in _wrap(s.lstrip("* ").rstrip("*"), 88):
                lines.append((w, False))
    y = 740
    parts = []
    for text, bold in lines:
        if y < 56:
            break
        size = 13 if bold else 10
        font = "/F1" if bold else "/F2"
        parts += ["BT", f"{font} {size} Tf", f"72 {y} Td", f"({_esc(text)}) Tj", "ET"]
        y -= 18 if bold else 13
    stream = "\n".join(parts).encode("latin-1", "replace")
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >> endobj\n",
        b"4 0 obj << /Length "
        + str(len(stream)).encode()
        + b" >> stream\n"
        + stream
        + b"\nendstream endobj\n",
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >> endobj\n",
        b"6 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(out))
        out += obj
    xref = len(out)
    out += f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode()
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)
