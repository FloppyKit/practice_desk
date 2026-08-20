/**
 * Compact Proton Drive browser + preview.
 * Auth: desk secret OR LiveKit host-link secret (office already logged in).
 */
(function () {
  "use strict";
  var cfg = window.PROTON_DRIVE || {};
  var API = (cfg.apiBase || "").replace(/\/$/, "");
  var mountSel = cfg.mount || "#pdrive-root";

  function apiFetch(url, opts) {
    if (window.deskFetch) return window.deskFetch(url, opts);
    opts = opts || {};
    var headers = Object.assign({}, opts.headers || {});
    var s = secret();
    if (s) headers["X-Staff-Secret"] = s;
    opts.headers = headers;
    return fetch(url, opts);
  }
  function secret() {
    if (cfg.hostSecret) return cfg.hostSecret;
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
  function fmtSize(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }
  function extOf(name) {
    var i = String(name || "").lastIndexOf(".");
    return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
  }
  function kindOf(name) {
    var e = extOf(name);
    if (e === "pdf") return "pdf";
    if (["png", "jpg", "jpeg", "gif", "webp", "svg"].indexOf(e) >= 0) return "image";
    if (["txt", "md", "csv", "json", "html", "css", "js", "xml", "log"].indexOf(e) >= 0)
      return "text";
    if (e === "docx") return "docx";
    return "other";
  }
  function mimeOf(name) {
    var e = extOf(name);
    if (e === "pdf") return "application/pdf";
    if (e === "png") return "image/png";
    if (e === "jpg" || e === "jpeg") return "image/jpeg";
    if (e === "gif") return "image/gif";
    if (e === "webp") return "image/webp";
    if (e === "svg") return "image/svg+xml";
    if (e === "json") return "application/json";
    if (e === "html") return "text/html";
    return "text/plain";
  }

  var style = document.createElement("style");
  style.textContent =
    "#pdrive-prev{position:fixed;inset:0;z-index:90;display:none;align-items:center;justify-content:center;background:rgba(0,0,0,.55);padding:1rem}" +
    "#pdrive-prev.is-open{display:flex}" +
    "#pdrive-prev .sheet{width:min(52rem,100%);height:min(86vh,100%);background:var(--surface,#fff);color:var(--foreground,#111);border-radius:.85rem;border:1px solid var(--border,#ddd);display:flex;flex-direction:column;overflow:hidden}" +
    "#pdrive-prev .top{display:flex;justify-content:space-between;align-items:center;gap:.5rem;padding:.55rem .75rem;border-bottom:1px solid var(--border,#ddd)}" +
    "#pdrive-prev .top strong{font-size:.85rem}" +
    "#pdrive-prev .acts{display:flex;gap:.35rem}" +
    "#pdrive-prev .acts button,#pdrive-prev .x{font:inherit;border:1px solid var(--border,#ddd);background:var(--background,#fff);color:inherit;border-radius:.45rem;padding:.3rem .55rem;cursor:pointer}" +
    "#pdrive-prev iframe,#pdrive-prev img{flex:1;width:100%;border:0;background:#111}" +
    "#pdrive-prev textarea{flex:1;width:100%;border:0;padding:.75rem;font:13px/1.45 ui-monospace,monospace;resize:none;background:var(--background,#fff);color:inherit}" +
    "#pdrive-prev .note{padding:.5rem .75rem;font-size:.75rem;color:var(--muted,#666)}" +
    "#pdrive-prev .tb{display:flex;gap:.3rem;padding:.35rem .6rem;border-bottom:1px solid var(--border,#ddd)}" +
    "#pdrive-prev .tb button{font:inherit;border:1px solid var(--border,#ddd);background:var(--background,#fff);border-radius:.35rem;padding:.2rem .45rem;cursor:pointer}" +
    "#pdrive-prev .ed{flex:1;overflow:auto;padding:.85rem 1rem;outline:none;font:15px/1.5 Georgia,serif}" +
    "#pdrive-share{display:none;flex-wrap:wrap;gap:.45rem .7rem;align-items:center;padding:.45rem .75rem;border-bottom:1px solid var(--border,#ddd);font-size:.8rem}" +
    "#pdrive-share.is-open{display:flex}" +
    "#pdrive-share label{display:inline-flex;align-items:center;gap:.3rem;cursor:pointer}" +
    "#pdrive-share input[type=text],#pdrive-share input[type=email],#pdrive-share input[type=tel]{font:inherit;border:1px solid var(--border,#ddd);border-radius:.4rem;padding:.28rem .45rem;min-width:8rem;background:var(--background,#fff);color:inherit}" +
    "#pdrive-share .st{font-size:.75rem;color:var(--muted,#666);flex:1 1 100%}";
  document.head.appendChild(style);

  var prev = document.createElement("div");
  prev.id = "pdrive-prev";
  prev.innerHTML =
    '<div class="sheet">' +
    '<div class="top"><strong id="pdrive-prev-name"></strong>' +
    '<div class="acts">' +
    '<button type="button" id="pdrive-prev-save" hidden>Save</button>' +
    '<button type="button" id="pdrive-prev-dl">Download</button>' +
    '<button type="button" id="pdrive-prev-share">Share with client</button>' +
    '<button type="button" class="x" id="pdrive-prev-x">×</button>' +
    "</div></div>" +
    '<div id="pdrive-share">' +
    '<label><input type="checkbox" id="pdrive-share-email" checked /> Email</label>' +
    '<label><input type="checkbox" id="pdrive-share-sms" checked /> SMS</label>' +
    '<input type="text" id="pdrive-share-name" placeholder="Name" autocomplete="off" />' +
    '<input type="email" id="pdrive-share-to" placeholder="Email" autocomplete="off" />' +
    '<input type="tel" id="pdrive-share-phone" placeholder="Phone" autocomplete="off" />' +
    '<button type="button" id="pdrive-share-go">Send</button>' +
    '<span class="st" id="pdrive-share-status"></span>' +
    "</div>" +
    '<div id="pdrive-prev-body" style="flex:1;display:flex;flex-direction:column;min-height:0"></div>' +
    "</div>";
  document.body.appendChild(prev);

  var prevUrl = "";
  var prevPath = "";
  function closePrev() {
    prev.classList.remove("is-open");
    if (prevUrl) {
      try {
        URL.revokeObjectURL(prevUrl);
      } catch (_) {}
      prevUrl = "";
    }
    prevPath = "";
    document.getElementById("pdrive-prev-body").innerHTML = "";
    var sh = document.getElementById("pdrive-share");
    if (sh) sh.classList.remove("is-open");
  }
  document.getElementById("pdrive-prev-x").onclick = closePrev;
  document.getElementById("pdrive-prev-share").onclick = function () {
    var box = document.getElementById("pdrive-share");
    var open = box.classList.toggle("is-open");
    if (open) fillShareForm();
  };
  document.getElementById("pdrive-share-go").onclick = function () {
    if (!prevPath) return;
    var wantMail = document.getElementById("pdrive-share-email").checked;
    var wantSms = document.getElementById("pdrive-share-sms").checked;
    var status = document.getElementById("pdrive-share-status");
    if (!wantMail && !wantSms) {
      status.textContent = "Check email, SMS, or both.";
      return;
    }
    var c = currentClient() || {};
    var btn = document.getElementById("pdrive-share-go");
    btn.disabled = true;
    status.textContent = "Sharing…";
    apiFetch(API + "/api/desk/drive/share", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: prevPath,
        client_id: c.id || "",
        name: document.getElementById("pdrive-share-name").value.trim(),
        email: document.getElementById("pdrive-share-to").value.trim(),
        phone: document.getElementById("pdrive-share-phone").value.trim(),
        send_email: wantMail,
        send_sms: wantSms,
      }),
    })
      .then(function (r) {
        return r.json().then(function (d) {
          return { ok: r.ok && d.ok, d: d, status: r.status };
        });
      })
      .then(function (x) {
        var bits = [];
        if (x.d && x.d.email && x.d.email.ok) bits.push("email sent");
        if (x.d && x.d.sms && x.d.sms.ok) bits.push("SMS sent");
        if (x.d && x.d.email && x.d.email.detail && !x.d.email.ok && !x.d.email.skipped)
          bits.push("email: " + x.d.email.detail);
        if (x.d && x.d.sms && x.d.sms.detail && !x.d.sms.ok && !x.d.sms.skipped)
          bits.push("SMS: " + x.d.sms.detail);
        if (x.ok) {
          bits.push(x.d.cloaked ? "cloaked link" : "Proton link");
          status.textContent = bits.join(" · ");
        } else {
          status.textContent = (x.d && (x.d.detail || x.d.error)) || bits.join(" · ") || "Share failed";
        }
      })
      .catch(function () {
        status.textContent = "Share failed";
      })
      .then(function () {
        btn.disabled = false;
      });
  };
  prev.addEventListener("click", function (e) {
    if (e.target === prev) closePrev();
  });

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      if (document.querySelector('script[src="' + src + '"]')) {
        resolve();
        return;
      }
      var s = document.createElement("script");
      s.src = src;
      s.onload = function () {
        resolve();
      };
      s.onerror = function () {
        reject(new Error("Could not load editor"));
      };
      document.head.appendChild(s);
    });
  }
  function blobToB64(blob) {
    return new Promise(function (resolve, reject) {
      var r = new FileReader();
      r.onload = function () {
        var s = String(r.result || "");
        var i = s.indexOf(",");
        resolve(i >= 0 ? s.slice(i + 1) : s);
      };
      r.onerror = reject;
      r.readAsDataURL(blob);
    });
  }
  function currentClient() {
    try {
      if (typeof cfg.client === "function") return cfg.client() || null;
    } catch (_) {}
    return null;
  }
  function fillShareForm() {
    var c = currentClient() || {};
    document.getElementById("pdrive-share-name").value = c.name || "";
    document.getElementById("pdrive-share-to").value = c.email || "";
    document.getElementById("pdrive-share-phone").value = c.phone || "";
    document.getElementById("pdrive-share-email").checked = !!c.email;
    document.getElementById("pdrive-share-sms").checked = !!c.phone;
    if (!c.email && !c.phone) {
      document.getElementById("pdrive-share-email").checked = true;
      document.getElementById("pdrive-share-sms").checked = true;
    }
    document.getElementById("pdrive-share-status").textContent = "";
  }
  function fileUrl(p) {
    return (
      API +
      "/api/desk/drive/file?path=" +
      encodeURIComponent(p)
    );
  }

  function openDocxEditor(blob, body) {
    document.getElementById("pdrive-prev-save").hidden = false;
    body.innerHTML = '<p class="note">Opening Word file in RAM…</p>';
    loadScript("https://cdn.jsdelivr.net/npm/mammoth@1.9.1/mammoth.browser.min.js")
      .then(function () {
        return blob.arrayBuffer();
      })
      .then(function (buf) {
        return window.mammoth.convertToHtml({ arrayBuffer: buf });
      })
      .then(function (res) {
        body.innerHTML = "";
        var tb = document.createElement("div");
        tb.className = "tb";
        [
          ["bold", "B"],
          ["italic", "I"],
          ["insertUnorderedList", "• list"],
          ["insertOrderedList", "1."],
        ].forEach(function (pair) {
          var b = document.createElement("button");
          b.type = "button";
          b.textContent = pair[1];
          b.onclick = function () {
            document.execCommand(pair[0], false, null);
          };
          tb.appendChild(b);
        });
        var ed = document.createElement("div");
        ed.id = "pdrive-ed";
        ed.className = "ed";
        ed.contentEditable = "true";
        ed.innerHTML = res.value || "<p></p>";
        body.appendChild(tb);
        body.appendChild(ed);
        var n = document.createElement("p");
        n.className = "note";
        n.textContent =
          "Light RAM editor — not Word itself. Fancy headers/tables may simplify when you save.";
        body.appendChild(n);
      })
      .catch(function (e) {
        body.innerHTML = '<p class="note">' + esc(e.message || "Could not open Word file") + "</p>";
      });
  }

  function openPreview(p) {
    prevPath = p;
    var name = p.split("/").pop();
    document.getElementById("pdrive-prev-name").textContent = name;
    var body = document.getElementById("pdrive-prev-body");
    body.innerHTML = '<p class="note">Loading…</p>';
    document.getElementById("pdrive-prev-save").hidden = true;
    prev.classList.add("is-open");
    fillShareForm();
    document.getElementById("pdrive-prev-dl").onclick = function () {
      window.open(fileUrl(p), "_blank");
    };
    var k = kindOf(name);
    apiFetch(fileUrl(p))
      .then(function (r) {
        if (!r.ok) throw new Error("Could not open file");
        if (k === "text") return r.text().then(function (t) {
          return { k: k, t: t };
        });
        return r.blob().then(function (b) {
          return { k: k, b: b };
        });
      })
      .then(function (x) {
        body.innerHTML = "";
        if (x.k === "pdf") {
          prevUrl = URL.createObjectURL(new Blob([x.b], { type: "application/pdf" }));
          var fr = document.createElement("iframe");
          fr.src = prevUrl;
          fr.title = name;
          body.appendChild(fr);
          var n = document.createElement("p");
          n.className = "note";
          n.textContent = "PDF preview — edit in a full editor, then re-upload from Proton if needed.";
          body.appendChild(n);
        } else if (x.k === "image") {
          prevUrl = URL.createObjectURL(x.b);
          var img = document.createElement("img");
          img.src = prevUrl;
          img.alt = name;
          img.style.objectFit = "contain";
          body.appendChild(img);
        } else if (x.k === "text") {
          var ta = document.createElement("textarea");
          ta.id = "pdrive-prev-ta";
          ta.value = x.t;
          body.appendChild(ta);
          document.getElementById("pdrive-prev-save").hidden = false;
        } else if (x.k === "docx") {
          openDocxEditor(x.b, body);
        } else {
          var p2 = document.createElement("p");
          p2.className = "note";
          p2.textContent = "No in-browser preview for this type. Use Download.";
          body.appendChild(p2);
        }
      })
      .catch(function (e) {
        body.innerHTML = '<p class="note">' + esc(e.message || "Failed") + "</p>";
      });
  }

  function postSave(body) {
    var btn = document.getElementById("pdrive-prev-save");
    btn.disabled = true;
    btn.textContent = "Saving…";
    return apiFetch(API + "/api/desk/drive/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (r) {
        return r.json().then(function (d) {
          return { ok: r.ok && d.ok, d: d };
        });
      })
      .then(function (x) {
        btn.textContent = x.ok ? "Saved" : (x.d && x.d.error) || "Save failed";
        setTimeout(function () {
          btn.textContent = "Save";
          btn.disabled = false;
        }, 1400);
      })
      .catch(function () {
        btn.textContent = "Save failed";
        btn.disabled = false;
      });
  }

  document.getElementById("pdrive-prev-save").onclick = function () {
    if (!prevPath) return;
    var ta = document.getElementById("pdrive-prev-ta");
    var ed = document.getElementById("pdrive-ed");
    if (ta) {
      postSave({ path: prevPath, text: ta.value });
      return;
    }
    if (!ed) return;
    loadScript("https://cdn.jsdelivr.net/npm/html-docx-js@0.3.1/dist/html-docx.js")
      .then(function () {
        var html =
          "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>" +
          ed.innerHTML +
          "</body></html>";
        var blob = window.htmlDocx.asBlob(html);
        return blobToB64(blob);
      })
      .then(function (b64) {
        return postSave({ path: prevPath, b64: b64 });
      })
      .catch(function (e) {
        var btn = document.getElementById("pdrive-prev-save");
        btn.textContent = e.message || "Save failed";
        btn.disabled = false;
      });
  };

  function boot() {
    var root = document.querySelector(mountSel);
    if (!root) return;
    root.className = (root.className + " pdrive").trim();
    root.innerHTML =
      '<div class="pdrive-bar">' +
      '<button type="button" class="pdrive-up" id="pdrive-up">↑</button>' +
      '<span class="pdrive-path" id="pdrive-path">/</span>' +
      '<button type="button" class="pdrive-ref" id="pdrive-ref">↻</button>' +
      "</div>" +
      '<div class="pdrive-list" id="pdrive-list">' +
      (cfg.auto === false ? "Open Chart or Billing from a client." : "Loading Drive…") +
      "</div>";

    var path = "/";
    function setPath(p) {
      path = p || "/";
      root.querySelector("#pdrive-path").textContent = path;
    }
    function parentOf(p) {
      if (!p || p === "/") return "/";
      var i = p.replace(/\/$/, "").lastIndexOf("/");
      return i <= 0 ? "/" : p.slice(0, i);
    }
    function paintList(items, fromCache) {
      var box = root.querySelector("#pdrive-list");
      if (!items || !items.length) {
        box.textContent = fromCache ? "Loading…" : "Empty folder.";
        return;
      }
      box.innerHTML = "";
      items.forEach(function (it) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "pdrive-item" + (it.type === "folder" ? " is-folder" : "");
        b.innerHTML =
          "<span>" +
          (it.type === "folder" ? "▸ " : "") +
          esc(it.name) +
          "</span>" +
          (it.type === "file" ? "<em>" + fmtSize(it.size) + "</em>" : "");
        b.onclick = function () {
          var next = (path === "/" ? "" : path) + "/" + it.name;
          if (it.type === "folder") {
            setPath(next);
            load();
          } else {
            openPreview(next);
          }
        };
        box.appendChild(b);
      });
      if (fromCache) box.dataset.cached = "1";
      else delete box.dataset.cached;
    }
    function load() {
      var box = root.querySelector("#pdrive-list");
      if (!secret()) {
        box.textContent =
          "Unlock Pa once, or open this office from your secret host link.";
        return;
      }
      var reqPath = path;
      var painted = false;
      var vault = window.PsychartsVault;
      if (vault && vault.ready() && vault.getListing) {
        vault.getListing(reqPath).then(function (hit) {
          if (path !== reqPath) return;
          if (hit && hit.items && hit.items.length && !painted) {
            paintList(hit.items, true);
            painted = true;
          }
        });
      }
      if (!painted) box.textContent = "Loading…";
      apiFetch(
        API +
          "/api/desk/drive/list?path=" +
          encodeURIComponent(reqPath)
      )
        .then(function (r) {
          return r.text().then(function (t) {
            var d = {};
            try {
              d = t ? JSON.parse(t) : {};
            } catch (_) {
              d = { error: t ? t.slice(0, 160) : r.statusText || "bad response" };
            }
            return { ok: r.ok, d: d };
          });
        })
        .then(function (x) {
          if (path !== reqPath) return;
          if (!x.ok || !x.d || !x.d.ok) {
            if (painted) return;
            var raw = (x.d && (x.d.detail || x.d.error)) || "Could not list Drive.";
            if (raw && typeof raw === "object") raw = raw.error || raw.detail || "Could not list Drive.";
            box.textContent = String(raw).replace(/^Error:\s*/i, "").trim() || "Could not list Drive.";
            return;
          }
          var items = x.d.items || [];
          painted = true;
          paintList(items, false);
          if (vault && vault.ready() && vault.putListing) vault.putListing(reqPath, items);
        })
        .catch(function (e) {
          if (path !== reqPath || painted) return;
          box.textContent = "Network: " + (e && e.message ? e.message : "failed");
        });
    }
    root.querySelector("#pdrive-up").onclick = function () {
      setPath(parentOf(path));
      load();
    };
    root.querySelector("#pdrive-ref").onclick = load;
    function go(p) {
      setPath(p || "/");
      load();
    }
    if (cfg.auto !== false) load();
    root._pdriveReload = load;
    root._pdriveGo = go;
    window.PsychartsDrive = {
      go: go,
      reload: load,
    };
    window.addEventListener("dmb-desk-unlocked", function () {
      if (cfg.auto !== false) load();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
