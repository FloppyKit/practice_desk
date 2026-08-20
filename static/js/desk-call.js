/**
 * Click-to-call. Browser mic (Twilio Voice) or ring-your-cell.
 * Caller ID is the office DID. SDK loads only when a desktop call starts.
 */
(function () {
  "use strict";
  if (window.DeskCall) return;

  var SDK = "https://cdn.jsdelivr.net/npm/@twilio/voice-sdk@2.12.3/dist/twilio.min.js";

  function cfg() {
    return window.STAFF_AGENT || window.CLIENT_TILE || {};
  }
  function apiBase() {
    return String(cfg().apiBase || "").replace(/\/$/, "");
  }
  function secret() {
    if (cfg().secret) return cfg().secret;
    try {
      if (window.__HOST_BOOT && window.__HOST_BOOT.secret) return window.__HOST_BOOT.secret;
    } catch (_) {}
    try {
      return localStorage.getItem("dmb-desk") || "";
    } catch (_) {
      return "";
    }
  }
  function esc(s) {
    return String(s || "").replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function videoLive() {
    return !!window.__deskVideoLive;
  }

  var style = document.createElement("style");
  style.textContent =
    "#dc-bar{position:fixed;right:1rem;bottom:4.7rem;z-index:81;width:min(22rem,calc(100vw - 1.5rem));" +
    "background:var(--surface,#fff);color:var(--foreground,#111);border:1px solid var(--border,#ddd);" +
    "border-radius:.9rem;box-shadow:0 16px 40px rgba(0,0,0,.16);padding:.7rem .8rem;font:14px/1.4 system-ui,sans-serif}" +
    "#dc-bar[hidden]{display:none!important}" +
    "#dc-bar p{margin:0 0 .45rem;font-size:.82rem}" +
    "#dc-bar .dc-meta{font-size:.75rem;color:var(--muted,#666);margin:0 0 .55rem}" +
    "#dc-bar .dc-row{display:flex;gap:.35rem;flex-wrap:wrap}" +
    "#dc-bar button{font:inherit;font-size:.82rem;padding:.4rem .7rem;border-radius:.55rem;border:0;cursor:pointer}" +
    "#dc-bar .dc-go{background:var(--foreground,#111);color:var(--background,#fff)}" +
    "#dc-bar .dc-stop{background:#b91c1c;color:#fff}" +
    "#dc-bar .dc-x{background:transparent;border:1px solid var(--border,#ddd);color:inherit}" +
    "button.desk-num{border:0;background:none;padding:0;font:inherit;color:inherit;" +
    "text-decoration:underline;text-underline-offset:.12em;cursor:pointer}";
  document.head.appendChild(style);

  var bar = document.createElement("div");
  bar.id = "dc-bar";
  bar.hidden = true;
  bar.setAttribute("role", "status");
  document.body.appendChild(bar);

  var pending = null;
  var liveSid = "";
  var pollTimer = 0;
  var startedAt = 0;
  var handset = "browser";
  var twilioCall = null;
  var twilioDevice = null;
  var muted = false;

  function q(path, opts) {
    var url = apiBase() + path + (path.indexOf("?") >= 0 ? "&" : "?") + "secret=" + encodeURIComponent(secret());
    return fetch(url, opts || {}).then(function (r) {
      return r.json().then(function (d) {
        return { ok: r.ok, status: r.status, d: d };
      });
    });
  }

  function stopPoll() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = 0;
  }

  function clock() {
    if (!startedAt) return "";
    var s = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    var m = Math.floor(s / 60);
    var r = s % 60;
    return m + ":" + (r < 10 ? "0" : "") + r;
  }

  function isBrowser() {
    return handset !== "phone";
  }

  function paint() {
    if (!pending && !liveSid && !twilioCall) {
      bar.hidden = true;
      bar.innerHTML = "";
      return;
    }
    bar.hidden = false;
    if (liveSid || twilioCall) {
      var st = isBrowser()
        ? "This computer is calling… They see " + esc((pending && pending.from_pretty) || "the office line") + "."
        : "Ringing your phone… They see " + esc((pending && pending.from_pretty) || "the office line") + ".";
      bar.innerHTML =
        "<p><strong>" +
        esc(pending && pending.name ? pending.name : "Practice call") +
        "</strong></p>" +
        '<p class="dc-meta" id="dc-st">' +
        st +
        "</p>" +
        '<div class="dc-row">' +
        (isBrowser() ? '<button type="button" class="dc-x" id="dc-mute">Mute</button>' : "") +
        '<button type="button" class="dc-stop" id="dc-end">End</button></div>';
      var muteBtn = bar.querySelector("#dc-mute");
      if (muteBtn) muteBtn.onclick = toggleMute;
      bar.querySelector("#dc-end").onclick = hangup;
      return;
    }
    var who = pending.name || pending.to_pretty || pending.to_masked || "this number";
    var how = isBrowser()
      ? "This computer will call (allow the mic). They see "
      : "Your phone rings first. They see ";
    bar.innerHTML =
      "<p>Call <strong>" +
      esc(who) +
      "</strong> at " +
      esc(pending.to_pretty || pending.to_masked || "") +
      "?</p>" +
      '<p class="dc-meta">' +
      how +
      esc(pending.from_pretty || "the office line") +
      " — never your personal number.</p>" +
      '<div class="dc-row">' +
      '<button type="button" class="dc-go" id="dc-go">Call</button>' +
      '<button type="button" class="dc-x" id="dc-no">Cancel</button>' +
      "</div>";
    bar.querySelector("#dc-go").onclick = place;
    bar.querySelector("#dc-no").onclick = cancel;
  }

  function confirm(info) {
    if (!info || !info.to) return;
    pending = {
      name: info.name || "",
      to: info.to,
      to_masked: info.to_masked || "",
      to_pretty: info.to_pretty || "",
      client_id: info.client_id || "",
      from_pretty: info.from_pretty || info.from_did || "",
    };
    liveSid = "";
    stopPoll();
    paint();
  }

  function dropTwilio() {
    try {
      if (twilioCall) twilioCall.disconnect();
    } catch (_) {}
    twilioCall = null;
    try {
      if (twilioDevice) twilioDevice.destroy();
    } catch (_) {}
    twilioDevice = null;
    muted = false;
  }

  function cancel() {
    pending = null;
    liveSid = "";
    stopPoll();
    dropTwilio();
    paint();
  }

  function loadSdk() {
    if (window.Twilio && Twilio.Device) return Promise.resolve();
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = SDK;
      s.async = true;
      s.onload = function () {
        if (window.Twilio && Twilio.Device) resolve();
        else reject(new Error("Voice library missing"));
      };
      s.onerror = function () {
        reject(new Error("Could not load the voice library"));
      };
      document.head.appendChild(s);
    });
  }

  function startBrowser(data) {
    return loadSdk().then(function () {
      return navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
        try {
          stream.getTracks().forEach(function (t) {
            t.stop();
          });
        } catch (_) {}
        twilioDevice = new Twilio.Device(data.token, { closeProtection: true, logLevel: "error" });
        return twilioDevice.connect({ params: { nonce: data.nonce } }).then(function (call) {
          twilioCall = call;
          startedAt = Date.now();
          liveSid = "browser";
          paint();
          call.on("accept", function () {
            var el = bar.querySelector("#dc-st");
            if (el) {
              el.textContent =
                "On the line · " +
                clock() +
                ". They see " +
                ((pending && pending.from_pretty) || "the office line") +
                ".";
            }
            if (pollTimer) return;
            pollTimer = setInterval(function () {
              var st = bar.querySelector("#dc-st");
              if (st && twilioCall) {
                st.textContent =
                  "On the line · " +
                  clock() +
                  ". They see " +
                  ((pending && pending.from_pretty) || "the office line") +
                  ".";
              }
            }, 1000);
          });
          call.on("disconnect", function () {
            var st = bar.querySelector("#dc-st");
            if (st) st.textContent = "Ended.";
            liveSid = "";
            twilioCall = null;
            stopPoll();
            setTimeout(cancel, 1200);
          });
          call.on("error", function (err) {
            bar.innerHTML =
              "<p>" +
              esc((err && err.message) || "Call failed") +
              '</p><div class="dc-row"><button type="button" class="dc-x" id="dc-no">Close</button></div>';
            bar.querySelector("#dc-no").onclick = cancel;
            dropTwilio();
          });
          call.on("cancel", function () {
            cancel();
          });
        });
      });
    });
  }

  function toggleMute() {
    if (!twilioCall) return;
    muted = !muted;
    try {
      twilioCall.mute(muted);
    } catch (_) {}
    var b = bar.querySelector("#dc-mute");
    if (b) b.textContent = muted ? "Unmute" : "Mute";
  }

  function place() {
    if (!pending || !pending.to) return Promise.resolve();
    if (!secret()) {
      bar.hidden = false;
      bar.innerHTML = "<p>Unlock the desk first.</p>";
      return Promise.resolve();
    }
    if (isBrowser() && videoLive()) {
      bar.innerHTML =
        "<p>A video visit is live in this window. Pop the visit out, or use My phone, then call.</p>" +
        '<div class="dc-row"><button type="button" class="dc-x" id="dc-no">Close</button></div>';
      bar.querySelector("#dc-no").onclick = cancel;
      return Promise.resolve();
    }
    var go = bar.querySelector("#dc-go");
    if (go) go.disabled = true;
    return q("/api/desk/call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        to: pending.to,
        client_id: pending.client_id || "",
        name: pending.name || "",
        handset: handset,
      }),
    })
      .then(function (x) {
        if (!x.ok || !x.d || !x.d.ok) {
          var err = (x.d && (x.d.detail || x.d.error)) || "Could not place the call.";
          bar.innerHTML = "<p>" + esc(err) + '</p><div class="dc-row"><button type="button" class="dc-x" id="dc-no">Close</button></div>';
          bar.querySelector("#dc-no").onclick = cancel;
          return;
        }
        if (x.d.from_pretty) pending.from_pretty = x.d.from_pretty;
        if (x.d.name) pending.name = x.d.name;
        if (x.d.mode === "browser" || x.d.handset === "browser") {
          handset = "browser";
          return startBrowser(x.d).catch(function (e) {
            bar.innerHTML =
              "<p>" +
              esc(e && e.message ? e.message : "Could not start the desktop call.") +
              '</p><div class="dc-row"><button type="button" class="dc-x" id="dc-no">Close</button></div>';
            bar.querySelector("#dc-no").onclick = cancel;
            dropTwilio();
          });
        }
        if (!x.d.sid) {
          bar.innerHTML = "<p>Could not place the call.</p>";
          return;
        }
        liveSid = x.d.sid;
        startedAt = Date.now();
        paint();
        startPoll();
      })
      .catch(function (e) {
        bar.innerHTML = "<p>Network: " + esc(e && e.message ? e.message : "failed") + "</p>";
      });
  }

  function hangup() {
    if (twilioCall) {
      dropTwilio();
      var st = bar.querySelector("#dc-st");
      if (st) st.textContent = "Ended.";
      liveSid = "";
      stopPoll();
      setTimeout(cancel, 800);
      return Promise.resolve();
    }
    if (!liveSid) {
      cancel();
      return Promise.resolve();
    }
    return q("/api/desk/call/hangup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid: liveSid }),
    }).finally(function () {
      var st = bar.querySelector("#dc-st");
      if (st) st.textContent = "Ended.";
      liveSid = "";
      stopPoll();
      setTimeout(cancel, 1200);
    });
  }

  function startPoll() {
    stopPoll();
    pollTimer = setInterval(function () {
      if (!liveSid || liveSid === "browser") return stopPoll();
      q("/api/desk/call?sid=" + encodeURIComponent(liveSid))
        .then(function (x) {
          var st = (x.d && x.d.status) || "";
          var el = bar.querySelector("#dc-st");
          if (!el) return;
          if (st === "queued" || st === "ringing") {
            el.textContent =
              "Ringing your phone… They see " +
              (pending && pending.from_pretty ? pending.from_pretty : "the office line") +
              ".";
          } else if (st === "in-progress") {
            el.textContent =
              "On the line · " +
              clock() +
              ". They see " +
              (pending && pending.from_pretty ? pending.from_pretty : "the office line") +
              ".";
          } else if (st) {
            el.textContent = st === "completed" ? "Ended." : st.replace(/-/g, " ") + ".";
            liveSid = "";
            stopPoll();
            setTimeout(cancel, 1400);
          }
        })
        .catch(function () {});
    }, 2000);
  }

  function applyConfig(d) {
    if (!d) return;
    if (d.handset === "phone" || d.handset === "browser") handset = d.handset;
    if (d.did_pretty && pending) pending.from_pretty = d.did_pretty;
  }

  function fromNumber(opts) {
    opts = opts || {};
    confirm({
      name: opts.name || "",
      to: opts.to || opts.phone || "",
      client_id: opts.client_id || "",
      to_pretty: opts.to_pretty || "",
      from_pretty: opts.from_pretty || "",
    });
    q("/api/desk/call")
      .then(function (x) {
        applyConfig(x.d);
        if (!liveSid && !twilioCall) paint();
        if (x.d && x.d.ready === false && x.d.detail && !liveSid && !twilioCall) {
          bar.hidden = false;
          bar.innerHTML =
            "<p>" +
            esc(x.d.detail) +
            '</p><div class="dc-row"><button type="button" class="dc-x" id="dc-no">Close</button></div>';
          bar.querySelector("#dc-no").onclick = cancel;
        }
      })
      .catch(function () {});
  }

  window.DeskCall = {
    confirm: confirm,
    fromNumber: fromNumber,
    place: place,
    hangup: hangup,
    cancel: cancel,
    pending: function () {
      return pending && pending.to && !liveSid && !twilioCall ? pending : null;
    },
    live: function () {
      return !!(liveSid || twilioCall);
    },
  };
})();
