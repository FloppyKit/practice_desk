"""Statement of reimbursement. Matches the CarePatron Superbill shape. No extra deps."""
from __future__ import annotations

from datetime import datetime
from typing import Any


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("\r", "")
    )


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    for raw in (text or "").replace("\r", "").split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        while len(raw) > width:
            cut = raw.rfind(" ", 0, width)
            if cut < 8:
                cut = width
            lines.append(raw[:cut].strip())
            raw = raw[cut:].strip()
        if raw:
            lines.append(raw)
    return lines or [""]


def _money(raw: Any) -> str:
    s = str(raw or "").strip().replace("$", "").replace(",", "")
    try:
        return f"${float(s):.2f}"
    except ValueError:
        return str(raw or "").strip() or "$0.00"


def _money_num(raw: Any) -> float:
    s = str(raw or "").strip().replace("$", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def pretty_date(raw: str) -> str:
    s = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            dt = datetime.strptime(s[:10], fmt)
            return dt.strftime("%b ") + str(dt.day) + dt.strftime(", %Y")
        except ValueError:
            continue
    return s


def superbill_filename(dos: str) -> str:
    s = (dos or "").strip()
    try:
        dt = datetime.strptime(s[:10], "%Y-%m-%d")
        return f"Superbill {dt.month}-{dt.day}-{dt.year}.pdf"
    except ValueError:
        today = datetime.now()
        return f"Superbill {today.month}-{today.day}-{today.year}.pdf"


def superbill_pdf(doc: dict) -> bytes:
    """One-page statement. Identity + diagnoses + CPT lines. No PHI in logs."""
    y = 760
    parts: list[str] = []

    def text(x: float, yy: float, s: str, *, size: int = 10, bold: bool = False, r: int = 0) -> None:
        font = "/F1" if bold else "/F2"
        parts.extend(
            ["BT", f"{font} {size} Tf", f"{x:.1f} {yy:.1f} Td", f"({_esc(s)}) Tj", "ET"]
        )
        if r:
            pass

    def rule(yy: float) -> None:
        parts.extend(["0.82 G", "0.6 w", f"48 {yy:.1f} m 564 {yy:.1f} l S", "0 G"])

    prac = doc.get("practice") or {}
    name = (prac.get("name") or "").strip()
    addr = (prac.get("address") or "").strip()
    header_lines = [name] if name else []
    header_lines.extend(_wrap(addr, 42)[:3])
    hy = 760
    for i, ln in enumerate(header_lines):
        text(300, hy, ln, size=11 if i == 0 else 9, bold=i == 0)
        hy -= 12
    y = min(hy, 720) - 10

    text(48, y, "Statement of reimbursement", size=16, bold=True)
    y -= 14
    rule(y)
    y -= 22

    text(48, y, "Statement #", size=8, bold=True)
    text(200, y, "Date issued", size=8, bold=True)
    y -= 13
    text(48, y, str(doc.get("statement") or ""), size=11)
    text(200, y, pretty_date(str(doc.get("issued") or "")), size=11)
    y -= 28

    text(48, y, "Client", size=8, bold=True)
    text(230, y, "Provider", size=8, bold=True)
    text(400, y, "Practice details", size=8, bold=True)
    y -= 13

    client = doc.get("client") or {}
    provider = doc.get("provider") or {}
    left = [client.get("name") or ""]
    if client.get("email"):
        left.append(str(client["email"]))
    if client.get("phone"):
        left.append(str(client["phone"]))
    if client.get("address"):
        left.extend(_wrap(str(client["address"]), 28)[:3])
    if client.get("dob"):
        left.append("Date of birth: " + pretty_date(str(client["dob"])))

    mid = [provider.get("name") or ""]
    if provider.get("email"):
        mid.append(str(provider["email"]))
    if provider.get("phone"):
        mid.append(str(provider["phone"]))
    if provider.get("npi"):
        mid.append("NPI: " + str(provider["npi"]))
    licenses = doc.get("licenses") or []
    if licenses:
        mid.append("License number:")
        for bit in licenses:
            mid.extend(_wrap(str(bit), 32))

    right = [prac.get("name") or ""]
    if prac.get("ein"):
        right.append("EIN: " + str(prac["ein"]))
    if prac.get("npi"):
        right.append("NPI: " + str(prac["npi"]))

    rows = max(len(left), len(mid), len(right), 1)
    for i in range(rows):
        if i < len(left) and left[i]:
            text(48, y, str(left[i])[:42], size=9)
        if i < len(mid) and mid[i]:
            text(230, y, str(mid[i])[:36], size=9)
        if i < len(right) and right[i]:
            text(400, y, str(right[i])[:28], size=9)
        y -= 12
    y -= 8
    rule(y)
    y -= 20

    text(48, y, "DX", size=8, bold=True)
    text(80, y, "Diagnosis code", size=8, bold=True)
    y -= 16
    diagnoses = doc.get("diagnoses") or []
    if not diagnoses:
        text(48, y, "—", size=10)
        y -= 14
    for i, dx in enumerate(diagnoses, 1):
        code = (dx.get("code") or "").strip()
        label = (dx.get("label") or "").strip()
        line = f"{code} - {label}".strip(" -") if label else code
        text(48, y, str(i), size=10, bold=True)
        for j, w in enumerate(_wrap(line, 78)):
            text(80, y, w, size=10)
            y -= 13
        y -= 2
    y -= 6
    rule(y)
    y -= 18

    heads = [
        (48, "Date"),
        (128, "POS"),
        (158, "Service"),
        (320, "Code"),
        (380, "DX"),
        (418, "Units"),
        (458, "Cost"),
        (518, "Paid"),
    ]
    for x, lab in heads:
        text(x, y, lab, size=8, bold=True)
    y -= 14
    total = 0.0
    for line in doc.get("lines") or []:
        if y < 80:
            break
        dos = pretty_date(str(line.get("dos") or doc.get("dos") or ""))
        pos = str(line.get("pos") or "10")
        svc = (line.get("service") or "").strip()
        code = (line.get("code") or "").strip()
        mod = (line.get("modifier") or "").strip()
        if mod and f"-{mod}" not in code:
            code = f"{code} - {mod}" if code else mod
        dxp = str(line.get("dx") or "")
        units = str(line.get("units") or "1")
        cost = _money(line.get("cost"))
        paid = _money(line.get("paid") if line.get("paid") not in (None, "") else line.get("cost"))
        total += _money_num(line.get("paid") if line.get("paid") not in (None, "") else line.get("cost"))
        text(48, y, dos, size=9)
        text(128, y, pos, size=9)
        svc_lines = _wrap(svc, 26)
        text(158, y, svc_lines[0], size=9)
        text(320, y, code[:14], size=9)
        text(380, y, dxp[:8], size=9)
        text(418, y, units[:4], size=9)
        text(458, y, cost, size=9)
        text(518, y, paid, size=9)
        y -= 12
        for extra in svc_lines[1:3]:
            text(158, y, extra, size=9)
            y -= 11
        y -= 4

    y -= 6
    text(400, y, "Total (USD)", size=10)
    text(500, y, f"${total:.2f}", size=12, bold=True)

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
