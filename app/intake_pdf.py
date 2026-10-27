"""Signed intake form -> PDF (fields, drawn signature, audit lines). No extra deps. No PHI in logs."""
from __future__ import annotations

import base64
import re
import struct
import zlib
from typing import Any

FORM_TITLES = {
    "personal": "Personal information",
    "consents": "Consents & policies",
    "card-auth": "Payment authorization",
    "roi": "Release of information",
}


def _esc(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").replace("\r", "")


def _wrap(text: str, width: int = 90) -> list[str]:
    out: list[str] = []
    for raw in (text or "").replace("\r", "").split("\n"):
        raw = raw.rstrip()
        while len(raw) > width:
            cut = raw.rfind(" ", 0, width)
            if cut < 20:
                cut = width
            out.append(raw[:cut].rstrip())
            raw = raw[cut:].lstrip()
        out.append(raw)
    return out


def _label(key: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r" \1", key).replace("_", " ").strip()
    return s[:1].upper() + s[1:]


def _value(v: Any) -> str:
    if v is True:
        return "Yes"
    if v is False:
        return "No"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


def decode_png(data: bytes) -> tuple[int, int, bytes, bytes | None]:
    """8-bit non-interlaced PNG -> (w, h, rgb, alpha|None). Raises ValueError on anything else."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a png")
    pos, idat, w, h, ctype = 8, bytearray(), 0, 0, -1
    while pos < len(data):
        (n,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if tag == b"IHDR":
            w, h, depth, ctype, _c, _f, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or interlace != 0 or ctype not in (0, 2, 4, 6):
                raise ValueError("unsupported png")
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
    if not (w and h) or w * h > 4_000_000:
        raise ValueError("bad png size")
    bpp = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(bytes(idat))
    stride = w * bpp
    prev = bytearray(stride)
    rgb, alpha = bytearray(), bytearray()
    i = 0
    for _ in range(h):
        ft = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        if ft == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 255
        elif ft == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif ft == 3:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 255
        elif ft == 4:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        prev = line
        if ctype == 2:
            rgb += line
        elif ctype == 0:
            for g in line:
                rgb += bytes((g, g, g))
        elif ctype == 4:
            for x in range(0, stride, 2):
                g = line[x]
                rgb += bytes((g, g, g))
                alpha.append(line[x + 1])
        else:
            for x in range(0, stride, 4):
                rgb += line[x:x + 3]
                alpha.append(line[x + 3])
    return w, h, bytes(rgb), (bytes(alpha) if ctype in (4, 6) else None)


def signature_png(fields: dict[str, Any]) -> bytes | None:
    sig = fields.get("signature")
    if not isinstance(sig, str) or not sig.startswith("data:image/png;base64,"):
        return None
    try:
        return base64.b64decode(sig.split(",", 1)[1], validate=False)
    except Exception:
        return None


def intake_pdf(
    *,
    form: str,
    fields: dict[str, Any],
    received_at: str,
    ip: str = "",
    user_agent: str = "",
    link_verified: bool = False,
    link_email: str = "",
    client_name: str = "",
) -> bytes:
    title = FORM_TITLES.get(form, form)
    lines: list[tuple[str, str]] = [("h", title), ("t", "Psych Arts - electronic intake submission"), ("t", "")]
    if client_name:
        lines.append(("t", f"Client: {client_name}"))
    lines += [
        ("t", f"Received: {received_at}"),
        ("t", f"IP address: {ip or '(not recorded)'}"),
        ("t", f"Browser: {(user_agent or '(not recorded)')[:200]}"),
        ("t", "Personal intake link: "
              + (f"VERIFIED (opened the link emailed to {link_email})" if link_verified
                 else "not used (form filled without a personal link)")),
        ("t", ""),
        ("b", "Answers"),
    ]
    for k, v in fields.items():
        if k == "signature" or v in ("", None):
            continue
        lines.append(("b2", _label(k)))
        for w in _wrap(_value(v)):
            lines.append(("t", "    " + w))
    sig = signature_png(fields)
    img = None
    if sig:
        try:
            img = decode_png(sig)
        except Exception:
            img = None
    lines.append(("t", ""))
    lines.append(("b", "Signature"))
    if not sig:
        lines.append(("t", "(no drawn signature on this form)"))
    elif not img:
        lines.append(("t", "(drawn signature received but could not be rendered; original is in the practice email)"))

    pages: list[list[str]] = [[]]
    y = 742
    for kind, text in lines:
        if y < 70:
            pages.append([])
            y = 742
        size, font, step = {"h": (15, "/F1", 22), "b": (12, "/F1", 18), "b2": (10, "/F1", 14)}.get(kind, (10, "/F2", 13))
        for w in (_wrap(text, 92) if kind == "t" else [text]):
            pages[-1] += ["BT", f"{font} {size} Tf", f"60 {y} Td", f"({_esc(w)}) Tj", "ET"]
            y -= step
    sig_on_page = None
    if img:
        iw, ih = img[0], img[1]
        dw = min(300.0, float(iw))
        dh = dw * ih / iw
        if dh > 140:
            dh, dw = 140.0, 140.0 * iw / ih
        if y - dh < 60:
            pages.append([])
            y = 742
        sig_on_page = len(pages) - 1
        pages[-1] += ["q", f"{dw:.1f} 0 0 {dh:.1f} 60 {y - dh:.1f} cm", "/Im1 Do", "Q"]
        y -= dh + 16
        pages[-1] += ["BT", "/F2 9 Tf", f"60 {y} Td",
                      f"({_esc('Drawn electronic signature, captured on the web form at the time above.')}) Tj", "ET"]

    objs: list[bytes] = []

    def add(b: bytes) -> int:
        objs.append(b)
        return len(objs)

    add(b"<< /Type /Catalog /Pages 2 0 R >>")
    add(b"")  # pages, filled later
    f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    im = 0
    if img:
        iw, ih, rgb, alpha = img
        smask = ""
        if alpha is not None:
            a = zlib.compress(alpha)
            sm = add(f"<< /Type /XObject /Subtype /Image /Width {iw} /Height {ih} /ColorSpace /DeviceGray "
                     f"/BitsPerComponent 8 /Filter /FlateDecode /Length {len(a)} >>\nstream\n".encode()
                     + a + b"\nendstream")
            smask = f" /SMask {sm} 0 R"
        c = zlib.compress(rgb)
        im = add(f"<< /Type /XObject /Subtype /Image /Width {iw} /Height {ih} /ColorSpace /DeviceRGB "
                 f"/BitsPerComponent 8 /Filter /FlateDecode{smask} /Length {len(c)} >>\nstream\n".encode()
                 + c + b"\nendstream")
    kids = []
    for n, ops in enumerate(pages):
        ops = ops + ["BT", "/F2 8 Tf", "60 36 Td", f"({_esc(f'{title} - page {n + 1} of {len(pages)}')}) Tj", "ET"]
        stream = "\n".join(ops).encode("cp1252", "replace")
        cid = add(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
        xo = f" /XObject << /Im1 {im} 0 R >>" if im and n == sig_on_page else ""
        pid = add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {cid} 0 R "
                  f"/Resources << /Font << /F1 {f1} 0 R /F2 {f2} 0 R >>{xo} >> >>".encode())
        kids.append(pid)
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
