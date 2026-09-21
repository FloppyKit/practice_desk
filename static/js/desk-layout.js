/**
 * Tablet shell layouts for GO-DESK-MOBILE.
 * notes = iPad documentation (no Office pane).
 * office = Tab S7 video chrome (no note editor).
 * desk = current Mac layout.
 * Never puts the staff secret in the URL.
 */
(function (global) {
  "use strict";

  var KEY = "dmb-desk-layout";
  var ALLOWED = { notes: 1, office: 1, desk: 1 };
  var _mode = "desk";

  function uaBundle(nav, win) {
    nav = nav || {};
    win = win || {};
    var screen = win.screen || {};
    return {
      ua: String(nav.userAgent || ""),
      platform: String(nav.platform || ""),
      maxTouchPoints: Number(nav.maxTouchPoints || 0),
      width: Number(screen.width || win.innerWidth || 0),
      height: Number(screen.height || win.innerHeight || 0),
    };
  }

  function isIPad(nav, win) {
    var b = uaBundle(nav, win);
    if (/iPad/i.test(b.ua)) return true;
    return b.platform === "MacIntel" && b.maxTouchPoints > 1;
  }

  function isAndroidTablet(nav, win) {
    var b = uaBundle(nav, win);
    if (!/Android/i.test(b.ua)) return false;
    if (/SM-T87|SM-T73|SM-X[0-9]|Tab S/i.test(b.ua)) return true;
    if (!/Mobile/i.test(b.ua)) return true;
    return Math.min(b.width || 0, b.height || 0) >= 600;
  }

  function isTouch(nav, win) {
    nav = nav || (typeof navigator !== "undefined" ? navigator : {});
    win = win || (typeof window !== "undefined" ? window : {});
    try {
      if (win.matchMedia && win.matchMedia("(pointer: coarse)").matches) return true;
    } catch (_) {}
    if (isIPad(nav, win) || isAndroidTablet(nav, win)) return true;
    return Number(nav.maxTouchPoints || 0) > 1;
  }

  function queryLayout(search) {
    var raw = String(search || "");
    var i = raw.indexOf("layout=");
    if (i < 0) return "";
    var rest = raw.slice(i + 7);
    var end = rest.search(/[&/#]/);
    var val = end < 0 ? rest : rest.slice(0, end);
    try {
      val = decodeURIComponent(val);
    } catch (_) {}
    return ALLOWED[val] ? val : "";
  }

  function detect(nav, win, stored, search) {
    var q = queryLayout(search);
    if (ALLOWED[q]) return q;
    var s = String(stored || "");
    if (ALLOWED[s]) return s;
    nav = nav || {};
    win = win || {};
    if (isIPad(nav, win)) return "notes";
    if (isAndroidTablet(nav, win)) return "office";
    if (isTouch(nav, win)) return "notes";
    return "desk";
  }

  function applyToDocument(mode, root, nav, win) {
    if (!ALLOWED[mode]) mode = "desk";
    _mode = mode;
    if (!root || !root.classList) return mode;
    root.classList.remove("desk-layout-notes", "desk-layout-office", "desk-layout-desk");
    root.classList.add("desk-layout-" + mode);
    if (isTouch(nav, win)) root.classList.add("desk-touch");
    else root.classList.remove("desk-touch");
    return mode;
  }

  function readStored() {
    try {
      return global.localStorage.getItem(KEY) || "";
    } catch (_) {
      return "";
    }
  }

  function writeStored(mode) {
    try {
      global.localStorage.setItem(KEY, mode);
    } catch (_) {}
  }

  function boot(opts) {
    opts = opts || {};
    var nav = opts.nav || (typeof navigator !== "undefined" ? navigator : {});
    var win = opts.win || (typeof window !== "undefined" ? window : {});
    var root = opts.root || (typeof document !== "undefined" ? document.documentElement : null);
    var mode = detect(nav, win, opts.stored != null ? opts.stored : readStored(), opts.search);
    return applyToDocument(mode, root, nav, win);
  }

  function set(mode, opts) {
    opts = opts || {};
    if (!ALLOWED[mode]) mode = "desk";
    var nav = opts.nav || (typeof navigator !== "undefined" ? navigator : {});
    var win = opts.win || (typeof window !== "undefined" ? window : {});
    var root = opts.root || (typeof document !== "undefined" ? document.documentElement : null);
    if (opts.persist !== false) writeStored(mode);
    return applyToDocument(mode, root, nav, win);
  }

  function current() {
    return _mode;
  }

  var api = {
    KEY: KEY,
    detect: detect,
    isIPad: isIPad,
    isAndroidTablet: isAndroidTablet,
    isTouch: isTouch,
    queryLayout: queryLayout,
    applyToDocument: applyToDocument,
    boot: boot,
    set: set,
    current: current,
  };
  global.deskLayout = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : typeof global !== "undefined" ? global : this);
