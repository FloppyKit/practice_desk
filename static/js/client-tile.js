/**
 * Shared Client tile (desk + live office).
 * Auth: desk secret or LiveKit host-link secret.
 */
(function () {
  "use strict";

  function cfg() {
    return window.CLIENT_TILE || {};
  }
  function apiBase() {
    return String(cfg().apiBase || "").replace(/\/$/, "");
  }
  function deskUrl() {
    return String(cfg().deskUrl || "https://book.psycharts.org/desk").replace(/\/$/, "");
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
  function apiFetch(url, opts) {
    if (window.deskFetch) return window.deskFetch(url, opts);
    opts = opts || {};
    var headers = Object.assign({}, opts.headers || {});
    var s = secret();
    if (s) headers["X-Staff-Secret"] = s;
    opts.headers = headers;
    return fetch(url, opts);
  }
  function $(id, root) {
    if (root && root.querySelector) {
      return root.querySelector("#" + CSS.escape(id)) || document.getElementById(id);
    }
    return document.getElementById(id);
  }
  function esc(s) {
    return String(s || "").replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function telHref(phone) {
    var digits = String(phone || "").replace(/[^\d+]/g, "");
    return digits ? "tel:" + digits : "";
  }
  function bindOfficeCall(btn, c) {
    if (!btn) return;
    btn.onclick = function (e) {
      e.preventDefault();
      if (window.DeskCall) {
        DeskCall.fromNumber({ name: c.name || "", to: c.phone || "", client_id: c.id || "" });
        return;
      }
      var href = telHref(c.phone);
      if (href) window.location.href = href;
    };
  }

  var homeTimer = 0;
  var homeCurrent = null;
  var focusEventId = "";
  var rootEl = null;

  window.PROTON_DRIVE = window.PROTON_DRIVE || {};
  window.PROTON_DRIVE.client = function () {
    return (homeCurrent && homeCurrent.client) || null;
  };

  function setHomeHint(t) {
    var el = $("home-hint", rootEl);
    if (!el) return;
    el.textContent = t || "";
    el.hidden = !t;
  }

  function closeHome() {
    homeCurrent = null;
    focusEventId = "";
    var body = $("home-body", rootEl);
    var empty = $("home-empty", rootEl);
    var q = $("home-q", rootEl);
    if (body) {
      body.hidden = true;
      body.innerHTML = "";
    }
    if (empty) {
      empty.hidden = false;
      empty.textContent = cfg().emptyText || "Search for a client.";
    }
    if (q) q.value = "";
    setHomeHint("");
    var hits = $("home-hits", rootEl);
    if (hits) {
      hits.hidden = true;
      hits.innerHTML = "";
    }
  }

  function openDesk(extra) {
    var c = (homeCurrent && homeCurrent.client) || {};
    var bits = [];
    if (c.id) bits.push("c=" + encodeURIComponent(c.id));
    else if (c.email) bits.push("e=" + encodeURIComponent(c.email));
    else if (c.name) bits.push("n=" + encodeURIComponent(c.name));
    var url = deskUrl() + (bits.length ? "#" + bits.join("&") : "");
    if (extra) url += (bits.length ? "&" : "#") + extra;
    window.open(url, "psycharts-desk");
  }

  function ensureFilesPop() {
    var pop = document.getElementById("files-pop");
    if (pop) return pop;
    pop = document.createElement("div");
    pop.id = "files-pop";
    pop.className = "client-files-pop";
    pop.hidden = true;
    pop.innerHTML =
      '<div class="book-pop-top">' +
      '<div><div class="book-pop-kicker">Proton Drive</div>' +
      '<p class="book-pop-time" id="files-pop-title">Practice files</p></div>' +
      '<button type="button" class="book-pop-close" id="files-close" aria-label="Close">×</button>' +
      "</div>" +
      '<div id="pdrive-root"></div>';
    document.body.appendChild(pop);
    var closer = document.getElementById("files-close");
    if (closer) closer.onclick = function () {
      pop.hidden = true;
    };
    return pop;
  }

  function openDriveAt(path, title) {
    var pop = ensureFilesPop();
    pop.hidden = false;
    var label = document.getElementById("files-pop-title");
    if (label) label.textContent = title || "Practice files";
    if (window.PsychartsDrive && typeof window.PsychartsDrive.go === "function") {
      window.PsychartsDrive.go(path || "/");
    }
  }

  async function searchHome() {
    var qEl = $("home-q", rootEl);
    var box = $("home-hits", rootEl);
    var empty = $("home-empty", rootEl);
    if (!qEl || !box) return;
    var q = qEl.value.trim();
    if (q.length < 2) {
      box.hidden = true;
      box.innerHTML = "";
      if (empty && !homeCurrent) empty.textContent = cfg().emptyText || "Search for a client.";
      return;
    }
    try {
      var res = await apiFetch(
        apiBase() +
          "/api/desk/clients?q=" +
          encodeURIComponent(q)
      );
      var data = await res.json().catch(function () {
        return {};
      });
      if (!res.ok) {
        box.hidden = true;
        if (empty) {
          empty.hidden = false;
          empty.textContent = data.detail || "Search failed.";
        }
        return;
      }
      var rows = data.clients || [];
      box.hidden = !rows.length;
      box.innerHTML = "";
      if (!rows.length && empty && !homeCurrent) {
        empty.hidden = false;
        empty.textContent = "No matches.";
      }
      rows.forEach(function (c) {
        var b = document.createElement("button");
        b.type = "button";
        var card =
          c.card_on_file && c.card_last4
            ? " · " + (c.card_brand || "card") + " •••• " + c.card_last4
            : "";
        b.textContent = (c.name || "—") + " · " + (c.email || "no email") + card;
        b.onclick = function () {
          qEl.value = c.name || c.email || "";
          box.hidden = true;
          openHome({ id: c.id, email: c.email || "", name: c.name || "" });
        };
        box.appendChild(b);
      });
    } catch (e) {
      box.hidden = true;
      if (empty) {
        empty.hidden = false;
        empty.textContent = "Search failed: network.";
      }
    }
  }

  async function openHome(ref) {
    var empty = $("home-empty", rootEl);
    var body = $("home-body", rootEl);
    if (empty) {
      empty.textContent = "Loading…";
      empty.hidden = false;
    }
    if (body) body.hidden = true;
    setHomeHint("");
    var q = new URLSearchParams();
    if (ref.id) q.set("id", ref.id);
    if (ref.email) q.set("email", ref.email);
    if (ref.name) q.set("name", ref.name);
    var res = await apiFetch(apiBase() + "/api/desk/client?" + q.toString());
    var data = await res.json().catch(function () {
      return {};
    });
    if (!res.ok || !data.ok) {
      if (empty) empty.textContent = data.detail || "Client not found.";
      return;
    }
    homeCurrent = data;
    renderHome(data);
  }

  function visitLine(v, opts) {
    opts = opts || {};
    var marked = !!(v.event_id && (opts.noShowIds || []).indexOf(v.event_id) >= 0);
    var extra = "";
    if (opts.allowNoShow && v.event_id) {
      extra = marked
        ? "<span class='home-noshow is-on'>No-show</span>"
        : "<button type='button' class='home-noshow' data-eid='" +
          esc(v.event_id || "") +
          "' data-start='" +
          esc(v.start_iso || "") +
          "' data-dur='" +
          esc(String(v.duration_minutes || "")) +
          "'>No-show</button>";
    }
    return (
      "<li class='home-visit-row'><button type='button' class='home-visit' data-eid='" +
      esc(v.event_id || "") +
      "' data-start='" +
      esc(v.start_iso || "") +
      "'><span>" +
      esc(v.when_label || v.start_iso || "") +
      " · " +
      esc(v.title || "Visit") +
      (v.duration_minutes ? " · " + v.duration_minutes + "m" : "") +
      "</span><span class='desk-meta'>" +
      esc(v.price_label || "") +
      "</span></button>" +
      extra +
      "</li>"
    );
  }

  function visitBlock(items, emptyText, opts) {
    if (!items.length) {
      return "<p class='home-empty' style='margin:0.35rem 0 0'>" + emptyText + "</p>";
    }
    var cap = 4;
    var head = items.slice(0, cap);
    var tail = items.slice(cap);
    var html = "<ul class='home-list'>" + head.map(function (v) { return visitLine(v, opts); }).join("") + "</ul>";
    if (tail.length) {
      html +=
        "<button type='button' class='home-more-dot' aria-expanded='false' aria-label='Show " +
        tail.length +
        " more visits'>···</button>";
      html +=
        "<ul class='home-list home-rest' hidden>" +
        tail.map(function (v) { return visitLine(v, opts); }).join("") +
        "</ul>";
    }
    return html;
  }

  function renderHome(data) {
    var c = data.client || {};
    var card = data.card || {};
    var links = data.links || {};
    var up = (data.visits && data.visits.upcoming) || [];
    var past = (data.visits && data.visits.past) || [];
    var last = past[0] || null;
    var older = past.slice(1);
    var noShowIds = (data.books && data.books.no_show_event_ids) || [];
    var pastOpts = { allowNoShow: !!c.id, noShowIds: noShowIds };
    var note = data.last_note || {};
    var plan = data.plan || {};
    var files = data.files || {};
    var cardLabel = "No card on file";
    var cardOk = false;
    if (card.on_file && card.last4) {
      cardLabel =
        (card.brand || "Card") +
        " •••• " +
        card.last4 +
        (card.exp_month && card.exp_year
          ? " · " + card.exp_month + "/" + String(card.exp_year).slice(-2)
          : "");
      cardOk = true;
    } else if (card.on_file) {
      cardLabel = "Payment on file";
      cardOk = true;
    }
    var bits = [];
    if (c.preferred_name && c.preferred_name !== c.name) bits.push("goes by " + c.preferred_name);
    if (c.dob) bits.push("DOB " + c.dob);
    if (c.status) bits.push(c.status);
    if (!c.directory) bits.push("calendar only");
    var mail = links.mailto || (c.email ? "mailto:" + c.email : "");
    var who = c.email
      ? mail
        ? "<a href='" + esc(mail) + "'>" + esc(c.email) + "</a>"
        : esc(c.email)
      : "no email";
    var phoneBit = c.phone
      ? " · <button type='button' class='desk-num' id='hm-phone' title='Call from the office line'>" +
        esc(c.phone) +
        "</button>"
      : "";
    var body = $("home-body", rootEl);
    var empty = $("home-empty", rootEl);
    body.innerHTML =
      '<div class="home-id">' +
      '<div class="home-id-top">' +
      "<h3>" +
      esc(c.name || c.email || "Client") +
      "</h3>" +
      '<button class="home-close" type="button" id="hm-close" aria-label="Close client">×</button>' +
      "</div>" +
      '<p class="home-kv">' +
      who +
      phoneBit +
      "</p>" +
      '<p class="home-kv">' +
      esc(bits.join(" · ")) +
      "</p>" +
      '<div class="home-chips">' +
      '<span class="home-chip' +
      (cardOk ? " ok" : "") +
      '">' +
      esc(cardLabel) +
      "</span>" +
      (window.DeskBooks ? DeskBooks.chip(data.books) : "") +
      "</div></div>" +
      '<div class="home-actions">' +
      '<button class="btn" type="button" id="hm-send">Send forms</button>' +
      (c.id ? '<button class="btn secondary" type="button" id="hm-charge">Charge</button>' : "") +
      (c.phone
        ? '<button class="btn secondary" type="button" id="hm-call">Call</button>'
        : "") +
      '<button class="btn secondary" type="button" id="hm-book">Book next</button>' +
      '<a class="btn secondary" id="hm-mail" ' +
      (mail ? "href='" + esc(mail) + "'" : "hidden") +
      ">Email</a>" +
      '<button class="btn secondary" type="button" id="hm-pay">Copy pay link</button>' +
      '<button class="btn secondary" type="button" id="hm-files">Files</button>' +
      '<button class="btn secondary" type="button" id="hm-note">Note</button>' +
      '<button class="btn secondary" type="button" id="hm-superbill">Superbill</button>' +
      "</div>" +
      '<div class="home-files" id="hm-files-row" hidden></div>' +
      (window.DeskBooks ? DeskBooks.form(data.books, cardOk) : "") +
      (last
        ? "<div class='home-sec'><h4>Last visit</h4><ul class='home-list'>" +
          visitLine(last, pastOpts) +
          "</ul></div>"
        : "") +
      "<div class='home-sec'><h4>Upcoming</h4>" +
      visitBlock(up, "Nothing on the calendar.") +
      "</div>" +
      "<div class='home-sec'><h4>Recent</h4>" +
      (older.length
        ? "<ul class='home-list'>" + older.map(function (v) { return visitLine(v, pastOpts); }).join("") + "</ul>"
        : "<p class='home-empty' style='margin:0.35rem 0 0'>" +
          (last ? "No older visits." : "No past visits in the last 6 months.") +
          "</p>") +
      "</div>" +
      '<div class="home-ghosts">' +
      '<p class="home-ghost"><strong>Last note</strong>' +
      esc(note.hint || "None yet.") +
      (note.count > 1 ? " · " + note.count + " in Visit notes" : "") +
      "</p>" +
      '<p class="home-ghost"><strong>Plan</strong>' +
      esc(plan.hint || "None yet.") +
      "</p></div>";
    empty.hidden = true;
    body.hidden = false;
    if (window.PsychartsVault && PsychartsVault.ready()) {
      PsychartsVault.getNote({ id: c.id || "", email: c.email || "" }).then(function (n) {
        var ghosts = body.querySelector(".home-ghosts");
        if (!n || !ghosts) return;
        function line(lab, t) {
          return t ? "<p><strong>" + esc(lab) + "</strong>" + esc(t) + "</p>" : "";
        }
        var html =
          line("Last note", (n.dos ? n.dos + " · " : "") + (n.template || "Signed")) +
          line("Diagnosis", n.diagnosis) +
          line("Medications", n.medications) +
          line("Supplements", n.supplements) +
          line("Recommendations", n.recommendations) +
          line("Return visit", n.return_visit);
        if (html) ghosts.innerHTML = "<div class='home-note-fields'>" + html + "</div>";
      }).catch(function () {});
    }
    $("hm-close", body).onclick = closeHome;
    bindOfficeCall($("hm-phone", body), c);
    if ($("hm-call", body)) bindOfficeCall($("hm-call", body), c);
    if (window.DeskBooks) {
      var hit = last || up[0] || {};
      DeskBooks.bind(body, {
        apiBase: apiBase(),
        secret: secret(),
        clientId: c.id || "",
        books: data.books || {},
        eventId: hit.event_id || "",
        dos: hit.start_iso ? String(hit.start_iso).slice(0, 10) : "",
        onDone: function () {
          openHome({ id: c.id, email: c.email || "" });
        },
      });
    }
    body.querySelectorAll(".home-visit").forEach(function (b) {
      b.onclick = function () {
        focusEventId = b.dataset.eid || "";
        setHomeHint("Open Desk to jump the week to that visit.");
      };
    });
    body.querySelectorAll("button.home-noshow").forEach(function (b) {
      b.onclick = function (ev) {
        ev.stopPropagation();
        if (!c.id) return;
        b.disabled = true;
        apiFetch(apiBase() + "/api/desk/fees/no-show", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            client_id: c.id,
            event_id: b.dataset.eid || "",
            dos: (b.dataset.start || "").slice(0, 10),
            duration_minutes: b.dataset.dur ? Number(b.dataset.dur) : 30,
            notify: true,
            charge: false,
          }),
        })
          .then(function (r) {
            return r.json().then(function (d) { return { ok: r.ok, d: d }; });
          })
          .then(function (x) {
            if (!x.ok || !x.d || !x.d.ok) {
              setHomeHint((x.d && (x.d.detail || x.d.error)) || "Could not mark no-show.");
              b.disabled = false;
              return;
            }
            openHome({ id: c.id, email: c.email || "" });
          })
          .catch(function () {
            b.disabled = false;
          });
      };
    });
    body.querySelectorAll(".home-more-dot").forEach(function (b) {
      b.onclick = function () {
        var rest = b.nextElementSibling;
        var open = rest && !rest.hidden;
        if (rest) rest.hidden = !!open;
        b.setAttribute("aria-expanded", open ? "false" : "true");
        b.textContent = open ? "···" : "–";
      };
    });
    $("hm-send", body).onclick = function () {
      openDesk("send=1");
    };
    $("hm-book", body).onclick = function () {
      setHomeHint("Book next lives on the week. Opening Desk…");
      openDesk("book=1");
    };
    $("hm-pay", body).onclick = async function () {
      if (!links.pay) return;
      try {
        await navigator.clipboard.writeText(links.pay);
      } catch (e) {}
      setHomeHint("Pay link copied.");
    };
    $("hm-note", body).onclick = function () {
      openDesk("note=1");
    };
    $("hm-superbill", body).onclick = function () {
      openDesk("sb=1");
    };
    $("hm-files", body).onclick = async function () {
      var row = $("hm-files-row", body);
      if (!row.hidden) {
        row.hidden = true;
        return;
      }
      row.hidden = false;
      row.innerHTML = "<p class='home-empty' style='margin:0'>Finding folder…</p>";
      function showFileBtns(chartPath, billPath, folderPath) {
        if (!chartPath && !billPath && !folderPath) return false;
        row.innerHTML =
          "<button class='btn' type='button' id='hm-chart'>Chart</button>" +
          "<button class='btn secondary' type='button' id='hm-billing'>Billing</button>";
        $("hm-chart", row).onclick = function () {
          openDriveAt(chartPath || folderPath, "Chart");
        };
        $("hm-billing", row).onclick = function () {
          openDriveAt(billPath || folderPath, "Billing");
        };
        return true;
      }
      if (window.PsychartsVault && PsychartsVault.ready() && PsychartsVault.getClientPaths) {
        try {
          var cached = await PsychartsVault.getClientPaths(c);
          if (cached) showFileBtns(cached.chart_path, cached.billing_path, cached.folder_path);
        } catch (_) {}
      }
      var chartPath = "";
      var billPath = "";
      var folderPath = "";
      try {
        var q = new URLSearchParams();
        if (c.id) q.set("id", c.id);
        if (c.email) q.set("email", c.email);
        var res = await apiFetch(apiBase() + "/api/desk/client/drive?" + q.toString());
        var data = await res.json().catch(function () {
          return {};
        });
        chartPath = data.chart_path || "";
        billPath = data.billing_path || "";
        folderPath = data.folder_path || "";
      } catch (_) {}
      if (!showFileBtns(chartPath, billPath, folderPath)) {
        if (!row.querySelector("#hm-chart")) {
          row.innerHTML =
            "<p class='home-empty' style='margin:0'>No matching Drive folder yet.</p>";
        }
        return;
      }
      if (window.PsychartsVault && PsychartsVault.ready() && PsychartsVault.putClientPaths) {
        PsychartsVault.putClientPaths(c, {
          chart_path: chartPath,
          billing_path: billPath,
          folder_path: folderPath,
        });
      }
    };
  }

  function mount(el) {
    if (!el) return;
    rootEl = el;
    el.classList.add("client-tile", "desk-client");
    el.innerHTML =
      '<div class="tile-head">' +
      '<h2 class="step-title">Client</h2>' +
      '<button type="button" class="home-close" id="client-close" aria-label="Clear client">×</button>' +
      "</div>" +
      '<div class="home-search">' +
      '<input id="home-q" type="search" placeholder="Find a client by name, email, or phone…" autocomplete="off" />' +
      '<div id="home-hits" class="hits" hidden></div>' +
      "</div>" +
      '<p id="home-empty" class="home-empty">' +
      esc(cfg().emptyText || "Search for a client.") +
      "</p>" +
      '<div id="home-body" hidden></div>' +
      '<p id="home-hint" class="home-hint" hidden></p>';
    $("client-close", el).onclick = closeHome;
    var filesClose = document.getElementById("files-close");
    if (filesClose) {
      filesClose.onclick = function () {
        var pop = document.getElementById("files-pop");
        if (pop) pop.hidden = true;
      };
    }
    $("home-q", el).addEventListener("input", function () {
      clearTimeout(homeTimer);
      homeTimer = setTimeout(searchHome, 180);
    });
  }

  function boot() {
    var sel = cfg().mount || "#office-client";
    var el = document.querySelector(sel);
    if (!el) return;
    mount(el);
    window.PsychartsClient = {
      open: openHome,
      clear: closeHome,
      current: function () {
        return homeCurrent;
      },
      openFiles: function (kind, _client, path) {
        var title = kind === "billing" ? "Billing" : kind === "chart" ? "Chart" : "Practice files";
        openDriveAt(path || "/", title);
      },
    };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
