/**
 * Practice services + per-type weekly hours. Overlay, not yaml.
 */
(function (global) {
  "use strict";

  var DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
  var DAY_LAB = {
    monday: "Mon",
    tuesday: "Tue",
    wednesday: "Wed",
    thursday: "Thu",
    friday: "Fri",
    saturday: "Sat",
    sunday: "Sun",
  };
  var list = [];
  var openSlug = "";
  var adding = false;

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
    var el = document.getElementById("prac-svc-st");
    if (el) el.textContent = t || "";
  }

  function prettyHour(hhmm) {
    var p = String(hhmm || "").split(":");
    var h = parseInt(p[0], 10);
    var m = p[1] || "00";
    if (isNaN(h)) return hhmm || "";
    if (m === "00") return String(h);
    return h + ":" + m;
  }

  function hoursSummary(hours) {
    hours = hours || {};
    var parts = [];
    var i = 0;
    while (i < DAYS.length) {
      var day = DAYS[i];
      var block = hours[day];
      if (!block) {
        i += 1;
        continue;
      }
      var j = i;
      var key = (block.start || "") + "-" + (block.end || "");
      while (j + 1 < DAYS.length) {
        var nxt = hours[DAYS[j + 1]];
        if (!nxt || (nxt.start || "") + "-" + (nxt.end || "") !== key) break;
        j += 1;
      }
      var range = prettyHour(block.start) + "–" + prettyHour(block.end);
      if (j === i) parts.push(DAY_LAB[day] + " " + range);
      else parts.push(DAY_LAB[day] + "–" + DAY_LAB[DAYS[j]] + " " + range);
      i = j + 1;
    }
    return parts.length ? parts.join(" · ") : "No hours";
  }

  function priceSummary(svc) {
    var durs = svc.durations || [];
    var by = svc.price_by_duration || {};
    var bits = durs.map(function (d) {
      var raw = by[String(d)];
      if (raw == null) raw = by[d];
      if (raw == null) raw = svc.price_usd;
      if (raw == null || raw === "") return "";
      var n = Number(raw);
      if (n === 0) return "No charge";
      if (!isNaN(n)) return "$" + (n === Math.floor(n) ? String(n) : n.toFixed(2).replace(/\.00$/, ""));
      return String(raw);
    }).filter(Boolean);
    if (bits.length) return bits.join(" · ");
    return svc.price_label || "";
  }

  function durLabel(svc) {
    var durs = svc.durations || [];
    if (!durs.length) return "";
    if (durs.length === 1) return durs[0] + " min";
    return durs.join(" or ") + " min";
  }

  function slugFromTitle(title) {
    return String(title || "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 40);
  }

  function fillDurButtons(svcs) {
    var box = document.getElementById("pop-dur");
    if (!box) return;
    var on = (svcs || []).filter(function (s) {
      return s && s.enabled !== false;
    });
    if (!on.length) return;
    var html = [];
    on.forEach(function (s) {
      var durs = s.durations && s.durations.length ? s.durations : [30];
      durs.forEach(function (d) {
        var lab = durs.length === 1 ? s.title || s.slug : d + " min";
        html.push(
          '<button type="button" data-dur="' +
            d +
            '" data-type="' +
            esc(s.slug) +
            '"' +
            (html.length === 0 ? ' class="active"' : "") +
            ">" +
            esc(lab) +
            "</button>"
        );
      });
    });
    box.innerHTML = html.join("");
    var first = box.querySelector("button[data-dur]");
    var dur = document.getElementById("bk-dur");
    var typ = document.getElementById("bk-type");
    if (first && dur && !dur.value) dur.value = first.getAttribute("data-dur") || "30";
    if (first && typ && !typ.value) typ.value = first.getAttribute("data-type") || "follow-up";
  }

  function hoursEditor(hours) {
    hours = hours || {};
    return (
      '<p class="desk-meta">Hours this visit can be booked</p><div class="svc-hours">' +
      DAYS.map(function (day) {
        var on = !!(hours[day] && hours[day].start && hours[day].end);
        var start = on ? hours[day].start : "09:00";
        var end = on ? hours[day].end : "17:00";
        return (
          '<label class="svc-day' +
          (on ? "" : " is-off") +
          '"><input type="checkbox" data-day="' +
          day +
          '"' +
          (on ? " checked" : "") +
          " /><span>" +
          DAY_LAB[day] +
          '</span><input type="time" data-start="' +
          day +
          '" value="' +
          esc(start) +
          '" /><span>–</span><input type="time" data-end="' +
          day +
          '" value="' +
          esc(end) +
          '" /></label>'
        );
      }).join("") +
      "</div>"
    );
  }

  function priceField(svc) {
    var durs = svc.durations || [];
    var by = svc.price_by_duration || {};
    if (durs.length > 1) {
      var bits = durs.map(function (d) {
        var raw = by[String(d)];
        if (raw == null) raw = by[d];
        if (raw == null) raw = svc.price_usd;
        return d + "=" + (raw == null ? "" : raw);
      });
      return bits.join(", ");
    }
    if (svc.price_usd != null && svc.price_usd !== "") return String(svc.price_usd);
    if (durs.length === 1) {
      var one = by[String(durs[0])];
      if (one == null) one = by[durs[0]];
      if (one != null) return String(one);
    }
    return "";
  }

  function editorHtml(svc, isNew) {
    svc = svc || {};
    var durs = (svc.durations || []).join(", ");
    var noticeH = Math.round(Number(svc.minimum_notice_minutes || 120) / 60);
    if (!noticeH && svc.minimum_notice_minutes) noticeH = 2;
    return (
      '<div class="svc-ed" id="svc-ed" data-slug="' +
      esc(svc.slug || "") +
      '">' +
      '<label class="field">Title <input id="svc-title" value="' +
      esc(svc.title || "") +
      '" /></label>' +
      (isNew
        ? '<label class="field">Slug <input id="svc-slug" value="' +
          esc(svc.slug || "") +
          '" placeholder="couples-60" /></label>'
        : '<p class="desk-meta">/' +
          esc(svc.slug || "") +
          (svc.public && svc.enabled !== false
            ? ' · <a href="/' + esc(svc.slug) + '" target="_blank" rel="noopener">Open book page</a>'
            : " · hidden from the book page") +
          "</p>") +
      '<label class="field">Lengths (minutes) <input id="svc-durs" value="' +
      esc(durs || (isNew ? "50" : "")) +
      '" placeholder="30, 60" /></label>' +
      '<label class="field">Price <input id="svc-price" value="' +
      esc(priceField(svc)) +
      '" placeholder="265 or 30=265, 60=420" /></label>' +
      '<label class="field">Calendar block (minutes, optional) <input id="svc-cal" inputmode="numeric" value="' +
      esc(svc.calendar_duration_minutes || "") +
      '" placeholder="same as visit" /></label>' +
      '<label class="field">Notice (hours) <input id="svc-notice" inputmode="numeric" value="' +
      esc(String(noticeH || 2)) +
      '" /></label>' +
      '<label class="field">Book ahead (days) <input id="svc-window" inputmode="numeric" value="' +
      esc(String(svc.booking_window_days || 30)) +
      '" /></label>' +
      '<label class="field" style="flex-direction:row;align-items:center;gap:0.45rem">' +
      '<input type="checkbox" id="svc-on"' +
      (svc.enabled !== false ? " checked" : "") +
      " /><span>Enabled</span></label>" +
      '<label class="field" style="flex-direction:row;align-items:center;gap:0.45rem">' +
      '<input type="checkbox" id="svc-public"' +
      (svc.public !== false ? " checked" : "") +
      " /><span>On the public book page</span></label>" +
      hoursEditor(svc.weekly_hours) +
      '<div class="desk-actions">' +
      '<button class="btn" type="button" id="svc-save">' +
      (isNew ? "Add service" : "Save service") +
      "</button>" +
      (isNew
        ? '<button class="btn secondary" type="button" id="svc-cancel">Cancel</button>'
        : svc.shipped
          ? '<button class="btn secondary" type="button" id="svc-hide">Hide</button>'
          : '<button class="btn secondary" type="button" id="svc-del">Remove</button>') +
      "</div></div>"
    );
  }

  function paint() {
    var box = document.getElementById("prac-svc-list");
    if (!box) return;
    var html = "";
    if (adding) {
      html += editorHtml({ enabled: true, public: true, durations: [50], weekly_hours: {} }, true);
    }
    list.forEach(function (svc) {
      var open = !adding && openSlug && openSlug === svc.slug;
      html +=
        '<button type="button" class="svc-row' +
        (open ? " is-on" : "") +
        (svc.enabled === false ? " off" : "") +
        '" data-open="' +
        esc(svc.slug) +
        '"><span><strong>' +
        esc(svc.title || svc.slug) +
        "</strong><span class=\"svc-meta\">" +
        esc(durLabel(svc)) +
        (priceSummary(svc) ? " · " + esc(priceSummary(svc)) : "") +
        (svc.enabled === false ? " · off" : svc.public === false ? " · private" : "") +
        "</span><span class=\"svc-meta\">" +
        esc(hoursSummary(svc.weekly_hours)) +
        "</span></span></button>";
      if (open) html += editorHtml(svc, false);
    });
    if (!list.length && !adding) {
      html += '<p class="desk-meta">No services yet.</p>';
    }
    box.innerHTML = html;
    bindEditor();
  }

  function readHours(root) {
    var out = {};
    DAYS.forEach(function (day) {
      var ck = root.querySelector('input[type=checkbox][data-day="' + day + '"]');
      if (!ck || !ck.checked) return;
      var startEl = root.querySelector('input[data-start="' + day + '"]');
      var endEl = root.querySelector('input[data-end="' + day + '"]');
      var start = startEl ? startEl.value : "";
      var end = endEl ? endEl.value : "";
      if (start && end) out[day] = { start: start, end: end };
    });
    return out;
  }

  function parsePrice(raw, durs) {
    var s = String(raw || "").trim();
    var by = {};
    if (!s) return { price_usd: null, price_by_duration: {} };
    if (s.indexOf("=") >= 0 || s.indexOf(",") >= 0) {
      s.split(",").forEach(function (part) {
        var bits = part.split("=");
        if (bits.length < 2) return;
        var d = parseInt(bits[0], 10);
        var n = parseFloat(String(bits[1]).replace(/[$,]/g, ""));
        if (!isNaN(d) && !isNaN(n)) by[d] = n;
      });
      return { price_usd: null, price_by_duration: by };
    }
    var n = parseFloat(s.replace(/[$,]/g, ""));
    if (isNaN(n)) return { price_usd: null, price_by_duration: {} };
    if (durs.length > 1) {
      durs.forEach(function (d) {
        by[d] = n;
      });
      return { price_usd: null, price_by_duration: by };
    }
    return { price_usd: n, price_by_duration: {} };
  }

  function parseDurs(raw) {
    return String(raw || "")
      .split(/[\s,]+/)
      .map(function (x) {
        return parseInt(x, 10);
      })
      .filter(function (n) {
        return n >= 5 && n <= 240;
      });
  }

  function readEditor() {
    var root = document.getElementById("svc-ed");
    if (!root) return null;
    var durs = parseDurs((document.getElementById("svc-durs") || {}).value);
    var prices = parsePrice((document.getElementById("svc-price") || {}).value, durs);
    var calRaw = ((document.getElementById("svc-cal") || {}).value || "").trim();
    var noticeH = parseFloat((document.getElementById("svc-notice") || {}).value || "2");
    var windowD = parseInt((document.getElementById("svc-window") || {}).value || "30", 10);
    var slugEl = document.getElementById("svc-slug");
    return {
      slug: slugEl ? slugEl.value.trim() : root.getAttribute("data-slug") || "",
      title: ((document.getElementById("svc-title") || {}).value || "").trim(),
      duration_minutes: durs.length === 1 ? durs[0] : durs,
      calendar_duration_minutes: calRaw ? parseInt(calRaw, 10) : null,
      price_usd: prices.price_usd,
      price_by_duration: prices.price_by_duration,
      weekly_hours: readHours(root),
      enabled: !!(document.getElementById("svc-on") && document.getElementById("svc-on").checked),
      public: !!(document.getElementById("svc-public") && document.getElementById("svc-public").checked),
      slot_interval_minutes: durs.length && Math.min.apply(null, durs) <= 15 ? 15 : 30,
      minimum_notice_minutes: isNaN(noticeH) ? 120 : Math.round(noticeH * 60),
      booking_window_days: isNaN(windowD) ? 30 : windowD,
      creating: adding,
    };
  }

  function applyList(services) {
    list = services || [];
    fillDurButtons(list);
    paint();
  }

  async function api(method, extra) {
    extra = extra || {};
    var url = "/api/desk/services";
    if (extra.slug && method === "DELETE") url += "?slug=" + encodeURIComponent(extra.slug);
    var opts = { method: method };
    if (method === "POST") {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(extra.body || {});
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

  async function load() {
    if (!secret()) return list;
    try {
      var data = await api("GET");
      applyList(data.services || []);
      return list;
    } catch (e) {
      status("Could not load services.");
      return list;
    }
  }

  async function save() {
    var body = readEditor();
    if (!body) return;
    if (!body.title) {
      status("Need a title.");
      return;
    }
    if (adding && !body.slug) body.slug = slugFromTitle(body.title);
    status("Saving…");
    try {
      var data = await api("POST", { body: body });
      adding = false;
      openSlug = (data.service && data.service.slug) || body.slug;
      applyList(data.services || []);
      status("Saved. Live on the book page if it is public.");
    } catch (e) {
      status(String(e.message || e));
    }
  }

  async function remove(slug, hide) {
    if (!slug) return;
    if (!hide && !window.confirm("Remove this service from the book page?")) return;
    status(hide ? "Hiding…" : "Removing…");
    try {
      var data = await api("DELETE", { slug: slug });
      if (openSlug === slug) openSlug = "";
      applyList(data.services || []);
      status(hide ? "Hidden from the book page." : "Removed.");
    } catch (e) {
      status(String(e.message || e));
    }
  }

  function bindEditor() {
    var root = document.getElementById("svc-ed");
    if (!root) return;
    root.querySelectorAll("input[type=checkbox][data-day]").forEach(function (ck) {
      ck.onchange = function () {
        var row = ck.closest(".svc-day");
        if (row) row.classList.toggle("is-off", !ck.checked);
      };
    });
    var title = document.getElementById("svc-title");
    var slug = document.getElementById("svc-slug");
    if (title && slug && adding) {
      title.addEventListener("input", function () {
        if (!slug.dataset.touched) slug.value = slugFromTitle(title.value);
      });
      slug.addEventListener("input", function () {
        slug.dataset.touched = "1";
      });
    }
    var saveBtn = document.getElementById("svc-save");
    if (saveBtn) saveBtn.onclick = function () { save(); };
    var cancel = document.getElementById("svc-cancel");
    if (cancel) {
      cancel.onclick = function () {
        adding = false;
        paint();
        status("");
      };
    }
    var hide = document.getElementById("svc-hide");
    if (hide) hide.onclick = function () { remove(root.getAttribute("data-slug"), true); };
    var del = document.getElementById("svc-del");
    if (del) del.onclick = function () { remove(root.getAttribute("data-slug"), false); };
  }

  function bind() {
    var box = document.getElementById("prac-svc-list");
    if (!box || box.dataset.bound) return;
    box.dataset.bound = "1";
    box.addEventListener("click", function (e) {
      var b = e.target.closest("button[data-open]");
      if (!b || !box.contains(b)) return;
      var slug = b.getAttribute("data-open");
      adding = false;
      openSlug = openSlug === slug ? "" : slug;
      paint();
    });
    var add = document.getElementById("prac-svc-add");
    if (add) {
      add.onclick = function () {
        adding = true;
        openSlug = "";
        paint();
        var title = document.getElementById("svc-title");
        if (title) title.focus();
      };
    }
    var acc = document.getElementById("prac-svc-acc");
    if (acc) {
      acc.addEventListener("toggle", function () {
        if (acc.open) load();
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }

  global.DeskServices = {
    load: load,
    list: function () {
      return list;
    },
    fillDurButtons: fillDurButtons,
  };
})(window);
