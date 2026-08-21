/**
 * Booker glue for CIPHER_P1: seal the intake payload and queue it on the
 * server before /api/book runs. Fails closed — when the server reports cipher
 * mode on and sealing breaks, the caller must abort, not fall back to
 * cleartext-only.
 */
(function (global) {
  "use strict";

  var _params = null;

  function sealParams(force) {
    if (_params && !force) return Promise.resolve(_params);
    return fetch("/api/cipher/seal-params")
      .then(function (res) {
        return res.json().catch(function () {
          return {};
        });
      })
      .then(function (data) {
        _params = data && data.enabled ? data : { enabled: false };
        return _params;
      });
  }

  var FIELDS = [
    "kind",
    "name",
    "email",
    "phone",
    "notes",
    "event_type",
    "start",
    "duration_minutes",
    "sms_consent",
    "notify_emails",
    "source",
    "client_id",
  ];

  function sealAndQueue(fields) {
    fields = fields || {};
    return sealParams().then(function (params) {
      if (!params || !params.enabled) return { enabled: false, id: "" };
      if (!global.PsychartsCipher || !global.PsychartsCipher.isAvailable()) {
        throw new Error("Sealed booking is on, but this browser cannot seal. Try a current browser.");
      }
      var payload = { kind: "intake" };
      FIELDS.forEach(function (k) {
        var v = fields[k];
        if (v !== undefined && v !== null && v !== "") payload[k] = v;
      });
      return global.PsychartsCipher.sealJson(params.public_jwk, payload, payload.kind).then(
        function (env) {
          return fetch("/api/cipher/intake", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(env),
          }).then(function (res) {
            return res
              .json()
              .catch(function () {
                return {};
              })
              .then(function (data) {
                if (!res.ok || !data.ok) {
                  throw new Error(data.detail || "Sealed intake was rejected. Booking stopped.");
                }
                return { enabled: true, id: data.id || "", content_hash: data.content_hash || "" };
              });
          });
        }
      );
    });
  }

  global.PsychartsCipherBooker = { sealAndQueue: sealAndQueue, sealParams: sealParams };
})(window);
