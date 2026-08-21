"""Sealed-envelope server side for CIPHER_P1. Stdlib only (no crypto dep on the box).

Browser seals with Web Crypto: ephemeral P-256 ECDH against the server public JWK,
deriveKey straight to AES-256-GCM (shared x-coordinate IS the key, per Web Crypto),
GCM tag appended to ciphertext. This module reverses that with pure-Python
P-256 and AES-GCM. Demo/P1 pipe only — synthetic data, clinical_claim: false.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- P-256 (secp256r1)

_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (_GX, _GY)


def _pt_add(p1: tuple[int, int] | None, p2: tuple[int, int] | None) -> tuple[int, int] | None:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p1 == p2:
        m = (3 * x1 * x1 + _A) * pow(2 * y1, -1, _P) % _P
    else:
        m = (y2 - y1) * pow((x2 - x1) % _P, -1, _P) % _P
    x3 = (m * m - x1 - x2) % _P
    y3 = (m * (x1 - x3) - y1) % _P
    return (x3, y3)


def _scalar_mult(k: int, point: tuple[int, int]) -> tuple[int, int] | None:
    result = None
    addend: tuple[int, int] | None = point
    while k:
        if k & 1:
            result = _pt_add(result, addend)
        addend = _pt_add(addend, addend)
        k >>= 1
    return result


def _on_curve(pt: tuple[int, int]) -> bool:
    x, y = pt
    if not (0 <= x < _P and 0 <= y < _P):
        return False
    return (y * y - (x * x * x + _A * x + _B)) % _P == 0


def ecdh_shared_key(priv_d: int, peer: tuple[int, int]) -> bytes:
    """Web Crypto ECDH deriveKey(AES-GCM-256) uses the shared x-coordinate raw."""
    shared = _scalar_mult(priv_d, peer)
    if shared is None:
        raise ValueError("bad peer point")
    return shared[0].to_bytes(32, "big")


# ---------------------------------------------------------------- AES-256 (FIPS-197)


def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _gf_pow(a: int, n: int) -> int:
    r = 1
    while n:
        if n & 1:
            r = _gmul(r, a)
        a = _gmul(a, a)
        n >>= 1
    return r


def _sbox_byte(b: int) -> int:
    inv = 0 if b == 0 else _gf_pow(b, 254)
    s = inv
    for r in (1, 2, 3, 4):
        s ^= ((inv << r) | (inv >> (8 - r))) & 0xFF
    return s ^ 0x63


_SBOX = [_sbox_byte(i) for i in range(256)]


def _expand_key(key: bytes) -> list[list[int]]:
    if len(key) != 32:
        raise ValueError("AES-256 key must be 32 bytes")
    nk, nr = 8, 14
    words: list[list[int]] = [list(key[4 * i : 4 * i + 4]) for i in range(nk)]
    rc = 1
    for i in range(nk, 4 * (nr + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= rc
            rc = _gmul(rc, 2)
        elif i % nk == 4:
            temp = [_SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return words


def _aes_encrypt_block(key_words: list[list[int]], block: bytes) -> bytes:
    st = list(block)  # column-major: st[4*c + r]

    def add_round_key(rnd: int) -> None:
        for c in range(4):
            w = key_words[rnd * 4 + c]
            for r in range(4):
                st[4 * c + r] ^= w[r]

    def sub_bytes() -> None:
        for i in range(16):
            st[i] = _SBOX[st[i]]

    def shift_rows() -> None:
        old = list(st)
        for r in range(4):
            for c in range(4):
                st[4 * c + r] = old[4 * ((c + r) % 4) + r]

    def mix_columns() -> None:
        for c in range(4):
            s0, s1, s2, s3 = st[4 * c : 4 * c + 4]
            st[4 * c + 0] = _gmul(s0, 2) ^ _gmul(s1, 3) ^ s2 ^ s3
            st[4 * c + 1] = s0 ^ _gmul(s1, 2) ^ _gmul(s2, 3) ^ s3
            st[4 * c + 2] = s0 ^ s1 ^ _gmul(s2, 2) ^ _gmul(s3, 3)
            st[4 * c + 3] = _gmul(s0, 3) ^ s1 ^ s2 ^ _gmul(s3, 2)

    add_round_key(0)
    for rnd in range(1, 14):
        sub_bytes()
        shift_rows()
        mix_columns()
        add_round_key(rnd)
    sub_bytes()
    shift_rows()
    add_round_key(14)
    return bytes(st)


# ---------------------------------------------------------------- GCM (SP 800-38D)

_GCM_R = 0xE1000000000000000000000000000000


def _ghash_mul(x: int, y: int) -> int:
    z = 0
    v = y
    for i in range(128):
        if (x >> (127 - i)) & 1:
            z ^= v
        v = (v >> 1) ^ _GCM_R if v & 1 else v >> 1
    return z


def _ghash(h: int, data: bytes) -> int:
    y = 0
    for off in range(0, len(data), 16):
        block = data[off : off + 16]
        if len(block) < 16:
            block = block + b"\x00" * (16 - len(block))
        y = _ghash_mul(y ^ int.from_bytes(block, "big"), h)
    return y


def _inc32(block: bytes) -> bytes:
    ctr = (int.from_bytes(block[-4:], "big") + 1) & 0xFFFFFFFF
    return block[:-4] + ctr.to_bytes(4, "big")


def _gctr(key_words: list[list[int]], icb: bytes, data: bytes) -> bytes:
    if not data:
        return b""
    out = bytearray()
    cb = icb
    for off in range(0, len(data), 16):
        cb = _inc32(cb)
        block = data[off : off + 16]
        ks = _aes_encrypt_block(key_words, cb)
        out.extend(bytes(a ^ b for a, b in zip(block, ks)))
    return bytes(out)


def _gcm_tag(key_words: list[list[int]], h: int, j0: bytes, aad: bytes, ct: bytes) -> bytes:
    lens = (len(aad) * 8).to_bytes(8, "big") + (len(ct) * 8).to_bytes(8, "big")
    s = _ghash(h, aad + b"\x00" * ((-len(aad)) % 16) + ct + b"\x00" * ((-len(ct)) % 16) + lens)
    ks = _aes_encrypt_block(key_words, j0)
    return bytes(a ^ b for a, b in zip(s.to_bytes(16, "big"), ks))


def aes_gcm_encrypt(key: bytes, iv: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """Returns ciphertext || 16-byte tag, matching Web Crypto AES-GCM output."""
    if len(iv) != 12:
        raise ValueError("96-bit IV required")
    kw = _expand_key(key)
    h = int.from_bytes(_aes_encrypt_block(kw, b"\x00" * 16), "big")
    j0 = iv + b"\x00\x00\x00\x01"
    ct = _gctr(kw, j0, plaintext)
    return ct + _gcm_tag(kw, h, j0, aad, ct)


def aes_gcm_decrypt(key: bytes, iv: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    if len(iv) != 12:
        raise ValueError("96-bit IV required")
    if len(blob) < 16:
        raise ValueError("ciphertext too short")
    ct, tag = blob[:-16], blob[-16:]
    kw = _expand_key(key)
    h = int.from_bytes(_aes_encrypt_block(kw, b"\x00" * 16), "big")
    j0 = iv + b"\x00\x00\x00\x01"
    expect = _gcm_tag(kw, h, j0, aad, ct)
    if not hmac.compare_digest(expect, tag):
        raise ValueError("gcm tag mismatch")
    return _gctr(kw, j0, ct)


# ---------------------------------------------------------------- envelope helpers


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64u_dec(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def content_hash(ct_b64: str) -> str:
    raw = base64.b64decode(ct_b64)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


_KINDS = ("intake", "client", "visit_detail", "vault_snapshot")
_WRAP_ECDH = "ECDH-ES"
_ALG = "A256GCM"

_key_cache: dict[str, Any] | None = None


def _key_path() -> Path:
    return Path(os.environ.get("CIPHER_SEAL_KEY_FILE") or "/data/logs/cipher-seal-v1.json")


def _fallback_key_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "logs" / "cipher-seal-v1.json"


def _load_or_create() -> dict[str, Any]:
    global _key_cache
    if _key_cache is not None:
        return _key_cache
    path = _key_path()
    try:
        if path.exists():
            _key_cache = json.loads(path.read_text(encoding="utf-8"))
            return _key_cache
    except OSError:
        pass
    d = secrets.randbelow(_N - 1) + 1
    pub = _scalar_mult(d, _G)
    if pub is None:
        raise RuntimeError("keygen failed")
    rec = {
        "v": 1,
        "crv": "P-256",
        "d": _b64u(d.to_bytes(32, "big")),
        "x": _b64u(pub[0].to_bytes(32, "big")),
        "y": _b64u(pub[1].to_bytes(32, "big")),
        "created": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    for target in (path, _fallback_key_path()):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(rec, indent=2), encoding="utf-8")
            os.chmod(target, 0o600)
            break
        except OSError:
            continue
    _key_cache = rec
    return rec


def ensure_keypair() -> dict[str, str]:
    """Public JWK for seal-params. Private half never leaves this module."""
    rec = _load_or_create()
    return {"kty": "EC", "crv": "P-256", "x": rec["x"], "y": rec["y"]}


def seal_params(enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {"enabled": False}
    return {
        "enabled": True,
        "v": 1,
        "alg": "ECDH-ES+A256GCM",
        "public_jwk": ensure_keypair(),
        "note": "public seal key; not a staff secret",
    }


def seal_envelope_python(plaintext: dict[str, Any], kind: str = "intake") -> dict[str, Any]:
    """Browser-equivalent seal in pure Python. Tests and local tooling only —
    the production sealer is Web Crypto in static/js/cipher-envelope.js."""
    rec = _load_or_create()
    peer = (int.from_bytes(_b64u_dec(rec["x"]), "big"), int.from_bytes(_b64u_dec(rec["y"]), "big"))
    eph = secrets.randbelow(_N - 1) + 1
    epk = _scalar_mult(eph, _G)
    if epk is None:
        raise RuntimeError("ephemeral keygen failed")
    key = ecdh_shared_key(eph, peer)
    iv = secrets.token_bytes(12)
    blob = aes_gcm_encrypt(key, iv, json.dumps(plaintext, separators=(",", ":")).encode("utf-8"))
    ct_b64 = base64.b64encode(blob).decode("ascii")
    return {
        "v": 1,
        "wrap": _WRAP_ECDH,
        "alg": _ALG,
        "kind": kind,
        "epk": {
            "kty": "EC",
            "crv": "P-256",
            "x": _b64u(epk[0].to_bytes(32, "big")),
            "y": _b64u(epk[1].to_bytes(32, "big")),
        },
        "iv": base64.b64encode(iv).decode("ascii"),
        "ct": ct_b64,
        "content_hash": content_hash(ct_b64),
        "bytes": len(blob),
        "created": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def open_envelope(env: dict[str, Any]) -> dict[str, Any]:
    """Unseal a browser-sealed envelope with the server private key."""
    if int(env.get("v") or 0) != 1:
        raise ValueError("unsupported envelope version")
    if env.get("wrap") != _WRAP_ECDH:
        raise ValueError("server opens ECDH-ES envelopes only (vault packs stay client-side)")
    if env.get("alg") != _ALG:
        raise ValueError("unsupported alg")
    epk = env.get("epk") or {}
    try:
        peer = (int.from_bytes(_b64u_dec(epk["x"]), "big"), int.from_bytes(_b64u_dec(epk["y"]), "big"))
    except Exception as e:
        raise ValueError("bad ephemeral key") from e
    if not _on_curve(peer):
        raise ValueError("ephemeral key not on curve")
    ct_b64 = str(env.get("ct") or "")
    iv_b64 = str(env.get("iv") or "")
    if not ct_b64 or not iv_b64:
        raise ValueError("empty envelope")
    want_hash = str(env.get("content_hash") or "")
    if want_hash and want_hash != content_hash(ct_b64):
        raise ValueError("content_hash mismatch")
    rec = _load_or_create()
    d = int.from_bytes(_b64u_dec(rec["d"]), "big")
    key = ecdh_shared_key(d, peer)
    blob = base64.b64decode(ct_b64)
    iv = base64.b64decode(iv_b64)
    pt = aes_gcm_decrypt(key, iv, blob)
    out = json.loads(pt.decode("utf-8"))
    if not isinstance(out, dict):
        raise ValueError("envelope payload must be an object")
    return out


def validate_envelope(env: Any) -> dict[str, Any]:
    """Shape check for POSTed envelopes. Raises ValueError on junk."""
    if not isinstance(env, dict):
        raise ValueError("envelope must be an object")
    if int(env.get("v") or 0) != 1:
        raise ValueError("unsupported envelope version")
    if env.get("wrap") != _WRAP_ECDH or env.get("alg") != _ALG:
        raise ValueError("unsupported wrap/alg")
    kind = str(env.get("kind") or "")
    if kind not in _KINDS:
        raise ValueError("unknown kind")
    ct_b64 = str(env.get("ct") or "")
    iv_b64 = str(env.get("iv") or "")
    if not ct_b64 or not iv_b64:
        raise ValueError("empty envelope")
    try:
        ct_raw = base64.b64decode(ct_b64)
        iv_raw = base64.b64decode(iv_b64)
    except Exception as e:
        raise ValueError("bad base64") from e
    if len(ct_raw) > 256 * 1024:
        raise ValueError("envelope too large")
    if len(ct_raw) < 16 or len(iv_raw) != 12:
        raise ValueError("bad ct/iv length")
    epk = env.get("epk") or {}
    if not epk.get("x") or not epk.get("y"):
        raise ValueError("missing ephemeral key")
    want = str(env.get("content_hash") or "")
    if want != content_hash(ct_b64):
        raise ValueError("content_hash mismatch")
    return env
