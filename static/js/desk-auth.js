/**
 * Staff fetches: header, never ?secret=.
 * Secret stays in the unlock field / localStorage — not the URL.
 */
(function (global) {
  "use strict";

  var KEY = "dmb-desk";
  var HEADER = "X-Staff-Secret";

  function secret() {
    var el = document.getElementById("secret");
    if (el && String(el.value || "").trim()) return String(el.value).trim();
    try {
      if (window.__HOST_BOOT && window.__HOST_BOOT.secret) return window.__HOST_BOOT.secret;
    } catch (_) {}
    try {
      var ct = window.CLIENT_TILE || {};
      if (ct.secret) return ct.secret;
    } catch (_) {}
    try {
      return localStorage.getItem(KEY) || "";
    } catch (_) {
      return "";
    }
  }

  function stripSecret(url) {
    var s = String(url || "");
    var i = s.indexOf("?");
    if (i < 0) return s;
    var path = s.slice(0, i);
    var rest = s.slice(i + 1);
    var hash = "";
    var h = rest.indexOf("#");
    if (h >= 0) {
      hash = rest.slice(h);
      rest = rest.slice(0, h);
    }
    var kept = rest.split("&").filter(function (part) {
      return part && part.split("=")[0] !== "secret";
    });
    return path + (kept.length ? "?" + kept.join("&") : "") + hash;
  }

  function deskFetch(url, opts) {
    opts = opts || {};
    var headers = {};
    if (opts.headers) {
      if (typeof Headers !== "undefined" && opts.headers instanceof Headers) {
        opts.headers.forEach(function (v, k) {
          headers[k] = v;
        });
      } else {
        Object.keys(opts.headers).forEach(function (k) {
          headers[k] = opts.headers[k];
        });
      }
    }
    var s = secret();
    if (s) headers[HEADER] = s;
    var next = {};
    Object.keys(opts).forEach(function (k) {
      if (k !== "headers") next[k] = opts[k];
    });
    next.headers = headers;
    if (next.credentials == null) next.credentials = "same-origin";
    return fetch(stripSecret(url), next);
  }

  global.deskAuth = { secret: secret, fetch: deskFetch, stripSecret: stripSecret };
  global.deskFetch = deskFetch;
})(window);
