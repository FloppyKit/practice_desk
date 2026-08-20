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


def after_visit_pdf(
    fields: dict[str, str],
    sections: list[dict] | None = None,
    title: str = "",
    letterhead: dict[str, str] | None = None,
) -> bytes:
    """Build a one-page-ish note. Practice letterhead, identity, then sections."""
    head = title or "After-Visit Summary"
    practice = letterhead or {}
    physician = fields.get("physician") or practice.get("clinician") or "Matthew Brown, D.O."
    blocks: list[tuple[str, str]] = [
        ("", head),
        ("Name", fields.get("name") or ""),
        ("Date of birth", fields.get("dob") or ""),
        ("Physician", physician),
    ]
    if sections:
        for sec in sections:
            sid = str(sec.get("id") or "")
            if sid in ("name", "dob", "physician"):
                continue
            blocks.append((str(sec.get("label") or sid), fields.get(sid) or ""))
    else:
        blocks += [
            ("Date of service", fields.get("dos") or ""),
            ("Return visit", fields.get("return_visit") or ""),
            ("Subjective", fields.get("subjective") or ""),
            ("Objective", fields.get("objective") or ""),
            ("Assessment", fields.get("assessment") or ""),
            ("Medications", fields.get("medications") or ""),
            ("Supplements", fields.get("supplements") or ""),
            ("Other recommendations", fields.get("recommendations") or ""),
        ]
    content_lines: list[tuple[str, bool]] = []
    for label, val in blocks:
        if not label:
            content_lines.append((val, True))
            continue
        content_lines.append((f"{label}:", True))
        for ln in _wrap(val):
            content_lines.append((ln, False))
        content_lines.append(("", False))

    y = 740
    stream_parts: list[str] = []
    name = (practice.get("name") or "").strip()
    addr = (practice.get("address") or "").strip()
    phone = (practice.get("phone") or "").strip()
    fax = (practice.get("fax") or "").strip()
    npi = (practice.get("npi") or "").strip()
    if name or addr or phone or fax or npi:
        if name:
            stream_parts += ["BT", "/F1 11 Tf", f"72 {y} Td", f"({_esc(name)}) Tj", "ET"]
            y -= 14
        if addr:
            stream_parts += ["BT", "/F2 9 Tf", f"72 {y} Td", f"({_esc(addr)}) Tj", "ET"]
            y -= 12
        contact = " · ".join(p for p in (
            f"Phone {phone}" if phone else "",
            f"Fax {fax}" if fax else "",
            f"NPI {npi}" if npi else "",
        ) if p)
        if contact:
            stream_parts += ["BT", "/F2 9 Tf", f"72 {y} Td", f"({_esc(contact)}) Tj", "ET"]
            y -= 18
        y -= 4
    stream_parts += ["BT", "/F1 16 Tf", f"72 {y} Td", f"({_esc(head)}) Tj", "ET"]
    y -= 22
    for i, (text, bold) in enumerate(content_lines):
        if i == 0:
            continue
        if y < 64:
            break
        size = 11 if bold else 10
        font = "/F1" if bold else "/F2"
        stream_parts += [
            "BT",
            f"{font} {size} Tf",
            f"72 {y} Td",
            f"({_esc(text)}) Tj",
            "ET",
        ]
        y -= 16 if bold else 13

    stream = "\n".join(stream_parts).encode("latin-1", "replace")
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
