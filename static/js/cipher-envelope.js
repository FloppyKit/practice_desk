/**
 * Sealed-envelope client for CIPHER_P1 (Web Crypto only).
 * Ephemeral P-256 ECDH against the server public JWK. The shared x-coordinate
 * (32 bytes) is used raw as the AES-256-GCM key — this matches
 * app/cipher_seal.py ecdh_shared_key, which reverses the exact same layout.
 * GCM tag (128-bit) rides appended to the ciphertext, Web Crypto default.
 */
(function (global) {
  "use strict";

  var TEXT = new TextEncoder();

  function bufToB64(buf) {
    var bytes = new Uint8Array(buf);
    var s = "";
    for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s); // standard base64 — server decodes with base64.b64decode
  }

  function hexOf(buf) {
    var b = new Uint8Array(buf);
    var s = "";
    for (var i = 0; i < b.length; i++) s += (b[i] < 16 ? "0" : "") + b[i].toString(16);
    return s;
  }

  function isAvailable() {
    return !!(
      global.crypto &&
      crypto.subtle &&
      typeof crypto.subtle.generateKey === "function" &&
      typeof crypto.subtle.deriveBits === "function"
    );
  }

  function sealJson(publicJwk, obj, kind) {
    if (!isAvailable()) return Promise.reject(new Error("web crypto unavailable"));
    if (!publicJwk || !publicJwk.x || !publicJwk.y) {
      return Promise.reject(new Error("bad server seal key"));
    }
    var ephemeral = null;
    var iv = null;
    var ctBuf = null;
    return crypto.subtle
      .generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"])
      .then(function (pair) {
        ephemeral = pair;
        return crypto.subtle.importKey(
          "jwk",
          { kty: "EC", crv: "P-256", x: publicJwk.x, y: publicJwk.y },
          { name: "ECDH", namedCurve: "P-256" },
          false,
          []
        );
      })
      .then(function (serverPub) {
        // P-256 ECDH shared secret IS the 32-byte x-coordinate. No KDF —
        // the server uses those raw bytes as the AES key, so we must too.
        return crypto.subtle.deriveBits(
          { name: "ECDH", public: serverPub },
          ephemeral.privateKey,
          256
        );
      })
      .then(function (bits) {
        var raw = new Uint8Array(bits);
        if (raw.length > 32) raw = raw.slice(0, 32);
        return crypto.subtle.importKey("raw", raw, { name: "AES-GCM", length: 256 }, false, [
          "encrypt",
        ]);
      })
      .then(function (aesKey) {
        iv = crypto.getRandomValues(new Uint8Array(12));
        return crypto.subtle.encrypt(
          { name: "AES-GCM", iv: iv, tagLength: 128 },
          aesKey,
          TEXT.encode(JSON.stringify(obj))
        );
      })
      .then(function (ct) {
        ctBuf = ct; // ciphertext || 16-byte tag
        return crypto.subtle.digest("SHA-256", ctBuf);
      })
      .then(function (hash) {
        var contentHash = "sha256:" + hexOf(hash);
        return crypto.subtle.exportKey("jwk", ephemeral.publicKey).then(function (epk) {
          return {
            v: 1,
            wrap: "ECDH-ES",
            alg: "A256GCM",
            kind: kind || "intake",
            epk: { kty: epk.kty, crv: epk.crv, x: epk.x, y: epk.y },
            iv: bufToB64(iv.buffer),
            ct: bufToB64(ctBuf),
            content_hash: contentHash,
            bytes: ctBuf.byteLength,
            created: new Date().toISOString(),
          };
        });
      });
  }

  global.PsychartsCipher = { sealJson: sealJson, isAvailable: isAvailable };
})(window);
