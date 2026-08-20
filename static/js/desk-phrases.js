/**
 * Dot phrases — staff text expanders.
 * Type .name then space/tab in a note box. Library lives under Settings.
 * Phrase names are not PHI. Expanded text stays in the field (vault/Drive).
 */
(function (global) {
  "use strict";

  var list = [];
  var openId = "";
  var adding = false;
  var menuEl = null;

  function esc(s) {
    return String(s || "").replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function secret() {
    var el = document.getElementById("secret");
    return el ? String(el.value || "").trim() : "";
  }

  function status(t) {
    var el = document.getElementById("prac-phrase-st");
    if (el) el.textContent = t || "";
  }

  function preview(body) {
    var s = String(body || "").replace(/\s+/g, " ").trim();
    if (s.length > 72) return s.slice(0, 70) + "…";
    return s;
  }

  function normTrigger(raw) {
    var s = String(raw || "")
      .trim()
      .toLowerCase();
    if (s.charAt(0) === ".") s = s.slice(1);
    return s.replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 41);
  }

  async function api(method, extra) {
    extra = extra || {};
    var url = "/api/desk/phrases";
    if (extra.id && method === "DELETE") url += "?id=" + encodeURIComponent(extra.id);
    if (extra.order) url = "/api/desk/phrases/order";
    var opts = { method: method };
    if (method === "POST") {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(extra.body || { ids: extra.ids || [] });
    }
    var res = await (global.deskFetch || fetch)(url, opts);
    var data = await res.json().catch(function () {
      return {};
    });
    if (!res.ok || data.ok === false) {
      var err = data.detail || data.message || res.status;
      if (Array.isArray(err)) err = err.map(function (x) { return x.msg || x; }).join("; ");
      throw new Error(err);
    }
    return data;
  }

  function byTrigger(trig) {
    var want = normTrigger(trig);
    for (var i = 0; i < list.length; i++) {
      if (list[i].id === want) return list[i];
    }
    return null;
  }

  function matches(prefix) {
    var q = normTrigger(prefix);
    return list.filter(function (p) {
      return !q || p.id.indexOf(q) === 0 || (p.title && p.title.toLowerCase().indexOf(q) >= 0);
    });
  }

  function editorHtml(p, isNew) {
    return (
      '<div class="nt-box" id="phrase-ed" data-id="' +
      esc(p.id || "") +
      '">' +
      '<label class="field">Shortcut <input id="phrase-trig" value="' +
      esc(p.trigger || p.id || "") +
      '" placeholder="ssri" ' +
      (isNew ? "" : "readonly ") +
      "/></label>" +
      '<p class="desk-meta">Type .' +
      esc(p.trigger || p.id || "name") +
      " in a note box, then space or tab.</p>" +
      '<label class="field">Label (optional) <input id="phrase-title" value="' +
      esc(p.title || "") +
      '" placeholder="SSRI counseling" /></label>' +
      '<label class="field">Inserts <textarea id="phrase-body" rows="4" placeholder="The text that replaces .shortcut">' +
      esc(p.body || "") +
      "</textarea></label>" +
      '<div class="desk-actions">' +
      '<button class="btn" type="button" id="phrase-save">' +
      (isNew ? "Add phrase" : "Save phrase") +
      "</button>" +
      (isNew
        ? '<button class="btn secondary" type="button" id="phrase-cancel">Cancel</button>'
        : '<button class="btn secondary" type="button" id="phrase-del">Remove</button>') +
      "</div></div>"
    );
  }

  function paint() {
    var box = document.getElementById("prac-phrase-list");
    if (!box) return;
    var html = "";
    if (adding) html += editorHtml({ trigger: "", title: "", body: "" }, true);
    list.forEach(function (p) {
      var open = !adding && openId && openId === p.id;
      html +=
        '<button type="button" class="svc-row' +
        (open ? " is-on" : "") +
        '" data-phrase="' +
        esc(p.id) +
        '"><span><strong>.' +
        esc(p.id) +
        "</strong>" +
        (p.title ? " · " + esc(p.title) : "") +
        '<span class="svc-meta">' +
        esc(preview(p.body)) +
        "</span></span></button>";
      if (open) html += editorHtml(p, false);
    });
    if (!list.length && !adding) {
      html +=
        '<p class="desk-meta">None yet. Add .ssri, .sleep, .labs — whatever you type every visit.</p>';
    }
    box.innerHTML = html;
    bindEditor();
  }

  function bindEditor() {
    var root = document.getElementById("phrase-ed");
    if (!root) return;
    var save = document.getElementById("phrase-save");
    var del = document.getElementById("phrase-del");
    var cancel = document.getElementById("phrase-cancel");
    if (save) save.onclick = function () { saveOne(); };
    if (cancel) cancel.onclick = function () { adding = false; paint(); };
    if (del) {
      del.onclick = function () {
        remove(root.getAttribute("data-id") || "");
      };
    }
  }

  function applyList(phrases) {
    list = phrases || [];
    paint();
  }

  async function load() {
    if (!secret()) return list;
    try {
      var data = await api("GET");
      applyList(data.phrases || []);
      return list;
    } catch (e) {
      status("Could not load phrases.");
      return list;
    }
  }

  async function saveOne() {
    var trig = ((document.getElementById("phrase-trig") || {}).value || "").trim();
    var title = ((document.getElementById("phrase-title") || {}).value || "").trim();
    var body = ((document.getElementById("phrase-body") || {}).value || "").trim();
    if (!normTrigger(trig)) {
      status("Need a shortcut name (letters, numbers, dash).");
      return;
    }
    if (!body) {
      status("Need the text to insert.");
      return;
    }
    status("Saving…");
    try {
      var data = await api("POST", { body: { trigger: trig, title: title, body: body } });
      adding = false;
      openId = data.id || normTrigger(trig);
      await load();
      status("Saved. In a note, type ." + openId + " then space.");
    } catch (e) {
      status(String(e.message || e));
    }
  }

  async function remove(id) {
    if (!id) return;
    if (!window.confirm("Remove ." + id + "?")) return;
    status("Removing…");
    try {
      await api("DELETE", { id: id });
      if (openId === id) openId = "";
      await load();
      status("Removed.");
    } catch (e) {
      status(String(e.message || e));
    }
  }

  function bind() {
    var add = document.getElementById("prac-phrase-add");
    var box = document.getElementById("prac-phrase-list");
    var acc = document.getElementById("prac-phrase-acc");
    if (add) {
      add.onclick = function () {
        adding = true;
        openId = "";
        paint();
        var inp = document.getElementById("phrase-trig");
        if (inp) inp.focus();
      };
    }
    if (box) {
      box.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-phrase]");
        if (!btn || !box.contains(btn)) return;
        var id = btn.getAttribute("data-phrase") || "";
        adding = false;
        openId = openId === id ? "" : id;
        paint();
      });
    }
    if (acc) {
      acc.addEventListener("toggle", function () {
        if (acc.open) load();
      });
    }
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("input", onInput, true);
    document.addEventListener("mousedown", function (e) {
      if (menuEl && !menuEl.contains(e.target)) hideMenu();
    });
  }

  function inNoteField(el) {
    if (!el || el.disabled || el.readOnly) return false;
    if (el.id === "phrase-trig" || el.id === "phrase-title" || el.id === "phrase-body") {
      return false;
    }
    var pop = document.getElementById("note-pop");
    if (!pop || pop.hidden || !pop.contains(el)) return false;
    if (el.tagName === "TEXTAREA") return true;
    if (el.tagName === "INPUT" && (el.type === "text" || el.type === "")) return true;
    return false;
  }

  function tokenAtCaret(el) {
    var start = el.selectionStart;
    var end = el.selectionEnd;
    if (start == null || start !== end) return null;
    var before = String(el.value || "").slice(0, start);
    var m = before.match(/(^|[\s(\[{])(\.([a-z0-9][a-z0-9_-]{0,40})?)$/i);
    if (!m) return null;
    return {
      full: m[2],
      trigger: (m[3] || "").toLowerCase(),
      from: start - m[2].length,
      to: start,
    };
  }

  function insertAt(el, from, to, text) {
    var val = String(el.value || "");
    var next = val.slice(0, from) + text + val.slice(to);
    var caret = from + text.length;
    el.value = next;
    el.selectionStart = el.selectionEnd = caret;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function hideMenu() {
    if (menuEl && menuEl.parentNode) menuEl.parentNode.removeChild(menuEl);
    menuEl = null;
  }

  function showMenu(el, tok) {
    var hits = matches(tok.trigger);
    hideMenu();
    if (!hits.length) return;
    menuEl = document.createElement("div");
    menuEl.className = "phrase-menu";
    menuEl.setAttribute("role", "listbox");
    hits.slice(0, 8).forEach(function (p, i) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "phrase-hit" + (i === 0 ? " is-on" : "");
      b.innerHTML =
        "<strong>." +
        esc(p.id) +
        "</strong>" +
        (p.title ? " <span>" + esc(p.title) + "</span>" : "") +
        "<em>" +
        esc(preview(p.body)) +
        "</em>";
      b.onmousedown = function (e) {
        e.preventDefault();
        insertAt(el, tok.from, tok.to, p.body);
        hideMenu();
        el.focus();
      };
      menuEl.appendChild(b);
    });
    document.body.appendChild(menuEl);
    var r = el.getBoundingClientRect();
    var top = r.bottom + 4;
    if (top + 180 > window.innerHeight) top = Math.max(8, r.top - 180);
    menuEl.style.left = Math.max(8, r.left) + "px";
    menuEl.style.top = top + "px";
    menuEl.style.minWidth = Math.min(360, Math.max(220, r.width)) + "px";
  }

  function expand(el, tok) {
    var p = byTrigger(tok.trigger);
    if (!p) return false;
    insertAt(el, tok.from, tok.to, p.body);
    hideMenu();
    return true;
  }

  function onKey(e) {
    var el = e.target;
    if (!inNoteField(el)) return;
    var tok = tokenAtCaret(el);
    if (menuEl && tok && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      var hits = menuEl.querySelectorAll(".phrase-hit");
      var on = menuEl.querySelector(".phrase-hit.is-on");
      var i = Array.prototype.indexOf.call(hits, on);
      if (e.key === "ArrowDown") i = Math.min(hits.length - 1, i + 1);
      else i = Math.max(0, i - 1);
      hits.forEach(function (h) { h.classList.remove("is-on"); });
      if (hits[i]) hits[i].classList.add("is-on");
      return;
    }
    if (e.key === "Escape" && menuEl) {
      e.preventDefault();
      hideMenu();
      return;
    }
    var commit = e.key === "Tab" || e.key === "Enter" || e.key === " ";
    if (!commit || !tok || !tok.trigger) return;
    if (menuEl && (e.key === "Tab" || e.key === "Enter")) {
      var pick = menuEl.querySelector(".phrase-hit.is-on");
      if (pick) {
        e.preventDefault();
        pick.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
        return;
      }
    }
    if (expand(el, tok)) {
      if (e.key === "Tab" || e.key === "Enter") e.preventDefault();
    }
  }

  function onInput(e) {
    var el = e.target;
    if (!inNoteField(el)) return;
    var tok = tokenAtCaret(el);
    if (tok) showMenu(el, tok);
    else hideMenu();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }

  global.DeskPhrases = {
    load: load,
    list: function () {
      return list;
    },
  };
})(window);
