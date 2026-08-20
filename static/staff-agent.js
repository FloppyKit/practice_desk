/**
 * Floating staff agent — desk, office, any practice page.
 * Uses the booker /api/desk/agent (whatever model is plugged in).
 */
(function () {
  "use strict";
  if (window.__staffAgentBooted) return;
  window.__staffAgentBooted = true;

  var cfg = window.STAFF_AGENT || {};
  var API = (cfg.apiBase || "").replace(/\/$/, "");
  var KEY = "dmb-desk";

  function secret() {
    try {
      return localStorage.getItem(KEY) || "";
    } catch (_) {
      return "";
    }
  }
  function setSecret(s) {
    try {
      localStorage.setItem(KEY, s);
    } catch (_) {}
  }
  function esc(s) {
    return String(s || "").replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  var style = document.createElement("style");
  style.textContent =
    "#sa-root{position:fixed;right:1rem;bottom:1rem;z-index:80;font:14px/1.4 system-ui,sans-serif}" +
    "#sa-btn{width:3rem;height:3rem;border-radius:999px;border:1px solid currentColor;background:var(--foreground,#111);color:var(--background,#fff);cursor:pointer;font-weight:600;box-shadow:0 8px 24px rgba(0,0,0,.18)}" +
    "#sa-panel{display:none;position:absolute;right:0;bottom:3.6rem;width:min(22rem,calc(100vw - 1.5rem));max-height:min(28rem,70vh);background:var(--surface,#fff);color:var(--foreground,#111);border:1px solid var(--border,#ddd);border-radius:.9rem;box-shadow:0 16px 40px rgba(0,0,0,.16);overflow:hidden;flex-direction:column}" +
    "#sa-root.is-open #sa-panel{display:flex}" +
    "#sa-head{display:flex;justify-content:space-between;align-items:center;padding:.65rem .8rem;border-bottom:1px solid var(--border,#ddd);font-size:.72rem;letter-spacing:.05em;text-transform:uppercase;color:var(--muted,#666)}" +
    "#sa-close{border:0;background:none;cursor:pointer;font-size:1.1rem;color:inherit}" +
    "#sa-log{flex:1;overflow:auto;padding:.55rem .8rem;min-height:8rem}" +
    "#sa-log .t{margin:0 0 .65rem}" +
    "#sa-log .w{font-size:.65rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted,#666)}" +
    "#sa-log p{margin:.15rem 0 0;white-space:pre-wrap}" +
    "#sa-form{display:flex;gap:.35rem;padding:.55rem .65rem;border-top:1px solid var(--border,#ddd)}" +
    "#sa-form input{flex:1;min-width:0;font:inherit;padding:.45rem .55rem;border:1px solid var(--border,#ddd);border-radius:.55rem;background:var(--background,#fff);color:inherit}" +
    "#sa-form button{font:inherit;padding:.45rem .7rem;border-radius:.55rem;border:0;background:var(--foreground,#111);color:var(--background,#fff);cursor:pointer}" +
    "#sa-lock{padding:.65rem .8rem;border-top:1px solid var(--border,#ddd)}" +
    "#sa-lock p{margin:0 0 .4rem;font-size:.8rem;color:var(--muted,#666)}";
  document.head.appendChild(style);

  var root = document.createElement("div");
  root.id = "sa-root";
  root.innerHTML =
    '<button type="button" id="sa-btn" aria-expanded="false" title="Staff agent">G</button>' +
    '<div id="sa-panel" role="dialog" aria-label="Staff agent">' +
    '<div id="sa-head"><span>Staff agent</span><button type="button" id="sa-close" aria-label="Close">×</button></div>' +
    '<div id="sa-log"></div>' +
    '<div id="sa-lock">' +
    "<p>Same staff secret as the desk.</p>" +
    '<div id="sa-form" style="padding:0;border:0">' +
    '<input id="sa-secret" type="password" placeholder="Staff secret" autocomplete="off" />' +
    '<button type="button" id="sa-unlock">Unlock</button></div></div>' +
    '<form id="sa-ask" class="sa-hidden">' +
    '<div id="sa-form">' +
    '<input id="sa-q" placeholder="Ask to send a form, book, draft…" autocomplete="off" />' +
    '<button type="submit">Ask</button></div></form></div>';
  document.body.appendChild(root);

  var hist = [];
  var logEl = root.querySelector("#sa-log");
  var lockEl = root.querySelector("#sa-lock");
  var askEl = root.querySelector("#sa-ask");
  askEl.style.display = "none";

  function line(who, text) {
    var d = document.createElement("div");
    d.className = "t";
    d.innerHTML = "<div class='w'>" + esc(who) + "</div><p>" + esc(text) + "</p>";
    logEl.appendChild(d);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function unlocked() {
    return !!secret();
  }
  function syncLock() {
    if (unlocked()) {
      lockEl.style.display = "none";
      askEl.style.display = "block";
    } else {
      lockEl.style.display = "block";
      askEl.style.display = "none";
    }
  }
  syncLock();
  if (unlocked()) line("Agent", "Ready. Same tools as the desk: patients, forms, mail, booking, notes.");

  root.querySelector("#sa-btn").onclick = function () {
    root.classList.toggle("is-open");
    root.querySelector("#sa-btn").setAttribute("aria-expanded", root.classList.contains("is-open") ? "true" : "false");
    if (root.classList.contains("is-open")) {
      var focus = unlocked() ? root.querySelector("#sa-q") : root.querySelector("#sa-secret");
      if (focus) focus.focus();
    }
  };
  root.querySelector("#sa-close").onclick = function () {
    root.classList.remove("is-open");
  };
  root.querySelector("#sa-unlock").onclick = function () {
    var s = root.querySelector("#sa-secret").value.trim();
    if (!s) return;
    setSecret(s);
    syncLock();
    line("Agent", "Unlocked.");
    root.querySelector("#sa-q").focus();
  };

  askEl.addEventListener("submit", function (e) {
    e.preventDefault();
    var q = root.querySelector("#sa-q").value.trim();
    if (!q) return;
    root.querySelector("#sa-q").value = "";
    hist.push({ role: "user", content: q });
    line("You", q);
    (window.deskFetch || fetch)(API + "/api/desk/agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: hist }),
    })
      .then(function (r) {
        return r.json().then(function (d) {
          return { ok: r.ok, d: d };
        });
      })
      .then(function (x) {
        var reply = (x.d && (x.d.reply || x.d.detail || x.d.error)) || "No reply";
        if (x.d && x.d.ok) hist.push({ role: "assistant", content: reply });
        if (x.ok === false && /unauthor/i.test(String(reply))) {
          try {
            localStorage.removeItem(KEY);
          } catch (_) {}
          syncLock();
        }
        line("Agent", reply);
      })
      .catch(function (err) {
        line("Agent", "Network: " + (err && err.message ? err.message : "failed"));
      });
  });
})();
