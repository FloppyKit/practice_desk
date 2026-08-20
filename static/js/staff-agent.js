/**
 * Practice assistant (Pa). Local verbs first; leftover English hits /api/desk/agent.
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
    "#sa-btn{width:3rem;height:3rem;border-radius:999px;border:1px solid currentColor;background:var(--foreground,#111);color:var(--background,#fff);cursor:pointer;font-weight:600;font-size:.82rem;letter-spacing:.04em;box-shadow:0 8px 24px rgba(0,0,0,.18)}" +
    "#sa-panel{display:none;position:absolute;right:0;bottom:3.6rem;width:min(22rem,calc(100vw - 1.5rem));max-height:min(28rem,70vh);background:var(--surface,#fff);color:var(--foreground,#111);border:1px solid var(--border,#ddd);border-radius:.9rem;box-shadow:0 16px 40px rgba(0,0,0,.16);overflow:hidden;flex-direction:column}" +
    "#sa-root.is-open #sa-panel{display:flex}" +
    "#sa-head{display:flex;justify-content:space-between;align-items:center;padding:.65rem .8rem;border-bottom:1px solid var(--border,#ddd);font-size:.72rem;letter-spacing:.05em;text-transform:uppercase;color:var(--muted,#666)}" +
    "#sa-head .sa-tools{display:flex;align-items:center;gap:.15rem}" +
    "#sa-speak,#sa-close{border:0;background:none;cursor:pointer;font-size:1rem;color:inherit;padding:.1rem .25rem;border-radius:.35rem}" +
    "#sa-speak.is-on{outline:1px solid currentColor}" +
    "#sa-log{flex:1;overflow:auto;padding:.55rem .8rem;min-height:8rem}" +
    "#sa-log .t{margin:0 0 .65rem}" +
    "#sa-log .w{display:flex;align-items:center;gap:.35rem;font-size:.65rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted,#666)}" +
    "#sa-log .say{border:0;background:none;cursor:pointer;font-size:.8rem;padding:0;line-height:1;opacity:.7}" +
    "#sa-log .say:hover{opacity:1}" +
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
    '<button type="button" id="sa-btn" aria-expanded="false" title="Practice assistant">Pa</button>' +
    '<div id="sa-panel" role="dialog" aria-label="Practice assistant">' +
    '<div id="sa-head"><span>Practice assistant</span><div class="sa-tools">' +
    '<button type="button" id="sa-speak" title="Auto-read replies" aria-pressed="false">🔇</button>' +
    '<button type="button" id="sa-close" aria-label="Close">×</button></div></div>' +
    '<div id="sa-log"></div>' +
    '<div id="sa-lock">' +
    "<p>Same staff secret as the desk.</p>" +
    '<div id="sa-form" style="padding:0;border:0">' +
    '<input id="sa-secret" type="password" placeholder="Staff secret" autocomplete="off" />' +
    '<button type="button" id="sa-unlock">Unlock</button></div></div>' +
    '<form id="sa-ask" class="sa-hidden">' +
    '<div id="sa-form">' +
    '<input id="sa-q" placeholder="Call, open, send, book…" autocomplete="off" />' +
    '<button type="submit">Ask</button></div></form></div>';
  document.body.appendChild(root);

  var hist = [];
  var logEl = root.querySelector("#sa-log");
  var lockEl = root.querySelector("#sa-lock");
  var askEl = root.querySelector("#sa-ask");
  var speakBtn = root.querySelector("#sa-speak");
  askEl.style.display = "none";

  function canSpeak() {
    return typeof window !== "undefined" && "speechSynthesis" in window;
  }
  function stopSpeak() {
    if (canSpeak()) window.speechSynthesis.cancel();
  }
  function speakText(text) {
    if (!canSpeak()) return;
    var clean = String(text || "").replace(/\s+/g, " ").trim();
    if (!clean) return;
    stopSpeak();
    var u = new SpeechSynthesisUtterance(clean);
    u.rate = 1.02;
    u.lang = "en-US";
    window.speechSynthesis.speak(u);
  }
  function autoOn() {
    try {
      return localStorage.getItem("dmb-sa-speak") === "1";
    } catch (_) {
      return false;
    }
  }
  function setAuto(on) {
    try {
      localStorage.setItem("dmb-sa-speak", on ? "1" : "0");
    } catch (_) {}
    speakBtn.textContent = on ? "🔊" : "🔇";
    speakBtn.classList.toggle("is-on", on);
    speakBtn.setAttribute("aria-pressed", on ? "true" : "false");
    speakBtn.title = on ? "Auto-read on — click to mute" : "Auto-read off — click to speak replies";
    if (!on) stopSpeak();
  }
  setAuto(autoOn());
  speakBtn.onclick = function (e) {
    e.stopPropagation();
    setAuto(!autoOn());
  };

  function line(who, text, opts) {
    opts = opts || {};
    var d = document.createElement("div");
    d.className = "t";
    var head = "<div class='w'><span>" + esc(who) + "</span>";
    if (opts.speakable && canSpeak()) {
      head += "<button type='button' class='say' title='Read this' aria-label='Read this'>▶</button>";
    }
    head += "</div>";
    d.innerHTML = head + "<p>" + esc(text) + "</p>";
    var say = d.querySelector(".say");
    if (say) {
      say.onclick = function () {
        speakText(text);
      };
    }
    logEl.appendChild(d);
    logEl.scrollTop = logEl.scrollHeight;
    if (opts.auto && autoOn()) speakText(text);
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
  if (unlocked()) line("Pa", "Ready. Call, open, send, or book — or ask in English.");

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
    try {
      window.dispatchEvent(new Event("dmb-desk-unlocked"));
    } catch (_) {}
    line("Pa", "Unlocked.");
    root.querySelector("#sa-q").focus();
  };

  askEl.addEventListener("submit", function (e) {
    e.preventDefault();
    var q = root.querySelector("#sa-q").value.trim();
    if (!q) return;
    root.querySelector("#sa-q").value = "";
    line("You", q);

    function askModel() {
      hist.push({ role: "user", content: q });
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
          line("Pa", reply, { speakable: true, auto: true });
          if (x.d && x.d.action && x.d.action.type === "confirm_call" && window.DeskCall) {
            DeskCall.confirm(x.d.action);
          }
        })
        .catch(function (err) {
          line("Pa", "Network: " + (err && err.message ? err.message : "failed"));
        });
    }

    if (window.PaRouter && typeof PaRouter.handle === "function") {
      PaRouter.handle(q)
        .then(function (r) {
          if (r && r.handled) {
            line("Pa", r.reply || "Done.", { speakable: true, auto: true });
            return;
          }
          askModel();
        })
        .catch(function () {
          askModel();
        });
      return;
    }
    askModel();
  });
})();
