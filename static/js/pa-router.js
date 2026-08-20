/**
 * Practice assistant router. Verbs run here. The model only sees leftover English.
 */
(function (global) {
  "use strict";

  var SEND_ALIASES = [
    { id: "intake", words: ["intake", "packet", "forms", "form packet", "new patient"] },
    { id: "personal", words: ["personal", "info form", "information form"] },
    { id: "consents", words: ["consents", "consent", "policies"] },
    { id: "card", words: ["card", "card auth", "authorization", "payment auth"] },
    { id: "roi", words: ["roi", "release"] },
    { id: "pay", words: ["pay link", "payment link", "pay", "payment"] },
  ];
  var SEND_LABEL = {
    intake: "intake packet",
    personal: "personal information form",
    consents: "consents",
    card: "payment authorization",
    roi: "release of information",
    pay: "pay link",
  };
  var DAYS = {
    sunday: 0, sun: 0,
    monday: 1, mon: 1,
    tuesday: 2, tue: 2, tues: 2,
    wednesday: 3, wed: 3,
    thursday: 4, thu: 4, thur: 4, thurs: 4,
    friday: 5, fri: 5,
    saturday: 6, sat: 6,
  };

  var pending = null;
  var KEY = "dmb-desk";

  function apiBase() {
    var cfg = global.STAFF_AGENT || global.CLIENT_TILE || {};
    return String(cfg.apiBase || "").replace(/\/$/, "");
  }
  function secret() {
    try {
      return localStorage.getItem(KEY) || "";
    } catch (_) {
      return "";
    }
  }
  function yesLine(s) {
    return /^(yes|y|ok|okay|do it|confirm|go)$/i.test(String(s || "").trim());
  }
  function noLine(s) {
    return /^(no|n|cancel|stop|nevermind|never mind)$/i.test(String(s || "").trim());
  }

  function currentClient() {
    var api = global.PsychartsClient;
    if (!api || typeof api.current !== "function") return null;
    var cur = api.current();
    if (!cur) return null;
    var c = cur.client || cur;
    if (!c || (!c.id && !c.name && !c.email && !c.phone)) return null;
    return {
      id: c.id || "",
      name: c.name || "",
      email: c.email || "",
      phone: c.phone || "",
    };
  }

  function searchClients(q) {
    var url = apiBase() + "/api/desk/clients?q=" + encodeURIComponent(q);
    return (global.deskFetch || fetch)(url).then(function (r) {
      return r.json().then(function (d) {
        return { ok: r.ok, clients: (d && d.clients) || [] };
      });
    });
  }

  function looksPhone(s) {
    var d = String(s || "").replace(/[^\d]/g, "");
    return d.length >= 10 && d.length <= 15;
  }

  function resolveWho(who) {
    who = String(who || "").replace(/[?.!,]+$/g, "").trim();
    if (!who || /^(her|him|them|this|them|the patient|the client)$/i.test(who)) {
      var cur = currentClient();
      if (cur) return Promise.resolve({ ok: true, client: cur });
      return Promise.resolve({ ok: false, error: "Who? Open a client or say the name." });
    }
    if (looksPhone(who)) {
      return Promise.resolve({
        ok: true,
        client: { id: "", name: "", email: "", phone: who },
      });
    }
    return searchClients(who).then(function (res) {
      var rows = res.clients || [];
      if (!rows.length) return { ok: false, error: "No directory match for “" + who + ".”" };
      if (rows.length === 1) return { ok: true, client: rows[0] };
      var q = who.toLowerCase();
      var exact = rows.filter(function (c) {
        return String(c.name || "").toLowerCase() === q;
      });
      if (exact.length === 1) return { ok: true, client: exact[0] };
      var names = rows
        .slice(0, 4)
        .map(function (c) {
          return c.name || c.email || "unnamed";
        })
        .join("; ");
      return { ok: false, error: "Ambiguous — " + names + ". Use a fuller name." };
    });
  }

  function stripWhoNoise(s) {
    return String(s || "")
      .replace(/^(the\s+)?(patient|client)\s+/i, "")
      .replace(/\s+(please|now)$/i, "")
      .trim();
  }

  function extractItems(raw) {
    var s = " " + String(raw || "").toLowerCase() + " ";
    var ids = [];
    SEND_ALIASES.forEach(function (a) {
      a.words.forEach(function (w) {
        if (s.indexOf(" " + w + " ") >= 0 && ids.indexOf(a.id) < 0) ids.push(a.id);
      });
    });
    if (!ids.length && /\bform/i.test(raw)) ids.push("intake");
    return ids;
  }

  function itemsLabel(ids) {
    var labs = (ids || []).map(function (id) {
      return SEND_LABEL[id] || id;
    });
    if (!labs.length) return "forms";
    if (labs.length === 1) return labs[0];
    return labs.slice(0, -1).join(", ") + " and " + labs[labs.length - 1];
  }

  function parseSend(rest) {
    var raw = stripWhoNoise(rest);
    var items = extractItems(raw);
    var who = raw;
    SEND_ALIASES.forEach(function (a) {
      a.words.forEach(function (w) {
        who = who.replace(new RegExp("\\b" + w.replace(/ /g, "\\s+") + "\\b", "ig"), " ");
      });
    });
    who = who
      .replace(/\b(the|a|an|to|for|and|link|packet|form|forms)\b/gi, " ")
      .replace(/\s+/g, " ")
      .trim();
    if (!items.length) items = ["intake"];
    return { verb: "send", who: who, items: items };
  }

  function parseTime(text) {
    var s = String(text || "").toLowerCase();
    var now = new Date();
    var day = null;
    var m;

    if (/\btoday\b/.test(s)) day = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    else if (/\btomorrow\b/.test(s)) {
      day = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    } else {
      var dayHit = s.match(
        /\b(this|next)?\s*(sun(day)?|mon(day)?|tue(s|sday)?|wed(nesday)?|thu(r|rs|rsday)?|fri(day)?|sat(urday)?)\b/
      );
      if (dayHit) {
        var token = dayHit[2].slice(0, 3);
        var want = DAYS[token];
        if (want === undefined) want = DAYS[dayHit[2]];
        var add = (want - now.getDay() + 7) % 7;
        if (add === 0 && /next/.test(dayHit[0] || "")) add = 7;
        day = new Date(now.getFullYear(), now.getMonth(), now.getDate() + add);
      }
    }

    var iso = s.match(/\b(20\d{2}-\d{2}-\d{2})\b/);
    if (iso) {
      var p = iso[1].split("-");
      day = new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
    }

    var hm = s.match(/\b(\d{1,2}):(\d{2})\s*(am|pm)?\b/);
    if (!hm) hm = s.match(/\b(\d{1,2})\s*(am|pm)\b/);
    if (!hm) return null;
    var h = Number(hm[1]);
    var min = 0;
    var ap = "";
    if (hm[0].indexOf(":") >= 0) {
      min = Number(hm[2] || 0);
      ap = String(hm[3] || "").toLowerCase();
    } else {
      ap = String(hm[2] || "").toLowerCase();
    }
    if (ap === "pm" && h < 12) h += 12;
    if (ap === "am" && h === 12) h = 0;
    if (!ap && h < 7) h += 12;
    if (!day) {
      day = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      if (h * 60 + min <= now.getHours() * 60 + now.getMinutes()) {
        day.setDate(day.getDate() + 1);
      }
    }
    day.setHours(h, min, 0, 0);
    return day;
  }

  function parseDuration(text) {
    var m = String(text || "").match(/\b(15|20|30|45|60|90)\s*(m|min|mins|minutes|minute)?\b/i);
    if (m) return Number(m[1]);
    if (/\bhour\b|\b1\s*hr\b/i.test(text)) return 60;
    if (/\bhalf[-\s]?hour\b/i.test(text)) return 30;
    return 30;
  }

  function parseBook(rest) {
    var raw = stripWhoNoise(rest);
    var when = parseTime(raw);
    var duration = parseDuration(raw);
    var who = raw
      .replace(/\b(today|tomorrow|this|next)\b/gi, " ")
      .replace(/\b(sun(day)?|mon(day)?|tue(s|sday)?|wed(nesday)?|thu(r|rs|rsday)?|fri(day)?|sat(urday)?)\b/gi, " ")
      .replace(/\b\d{1,2}(?::\d{2})?\s*(am|pm)?\b/gi, " ")
      .replace(/\b20\d{2}-\d{2}-\d{2}\b/g, " ")
      .replace(/\b(15|20|30|45|60|90)\s*(m|min|mins|minutes|minute)?\b/gi, " ")
      .replace(/\b(at|on|for|a|an|the|follow-?up|visit|appointment)\b/gi, " ")
      .replace(/\s+/g, " ")
      .trim();
    return { verb: "book", who: who, when: when, duration: duration };
  }

  function parse(q) {
    var s = String(q || "").trim();
    if (!s) return null;
    if (yesLine(s)) return { verb: "yes" };
    if (noLine(s)) return { verb: "no" };
    if (/^help$/i.test(s) || /^what can you do\??$/i.test(s)) return { verb: "help" };

    var m = s.match(/^(?:please\s+)?(?:call|dial)\s*(.*)$/i);
    if (m && /^(?:please\s+)?(?:call|dial)\b/i.test(s)) {
      return { verb: "call", who: stripWhoNoise(m[1] || "") };
    }
    m = s.match(/^(?:please\s+)?open\s+(.+)$/i);
    if (m) {
      var rest = m[1];
      var target = "home";
      if (/\bchart\b/i.test(rest)) {
        target = "chart";
        rest = rest.replace(/\bchart\b/gi, "");
      } else if (/\bbilling\b/i.test(rest)) {
        target = "billing";
        rest = rest.replace(/\bbilling\b/gi, "");
      } else if (/\bfiles?\b/i.test(rest)) {
        target = "files";
        rest = rest.replace(/\bfiles?\b/gi, "");
      }
      return { verb: "open", who: stripWhoNoise(rest.replace(/^\s*(for|of)\s+/i, "")), target: target };
    }
    m = s.match(/^(?:please\s+)?send\s+(.+)$/i);
    if (m) return parseSend(m[1]);
    m = s.match(/^(?:please\s+)?(?:book|schedule)\s+(.+)$/i);
    if (m) return parseBook(m[1]);
    return null;
  }

  function isoLocal(d) {
    function pad(n) {
      return (n < 10 ? "0" : "") + n;
    }
    var off = -d.getTimezoneOffset();
    var sign = off >= 0 ? "+" : "-";
    var ah = Math.floor(Math.abs(off) / 60);
    var am = Math.abs(off) % 60;
    return (
      d.getFullYear() +
      "-" +
      pad(d.getMonth() + 1) +
      "-" +
      pad(d.getDate()) +
      "T" +
      pad(d.getHours()) +
      ":" +
      pad(d.getMinutes()) +
      ":00" +
      sign +
      pad(ah) +
      ":" +
      pad(am)
    );
  }

  function whenLabel(d) {
    try {
      return d.toLocaleString(undefined, {
        weekday: "short",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
    } catch (_) {
      return isoLocal(d);
    }
  }

  function post(path, body) {
    return (global.deskFetch || fetch)(apiBase() + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      return r.json().then(function (d) {
        return { ok: r.ok, status: r.status, d: d };
      });
    });
  }

  function openDrive(client, target) {
    if (target === "home") return Promise.resolve();
    var q = new URLSearchParams({ secret: secret() });
    if (client.id) q.set("id", client.id);
    if (client.email) q.set("email", client.email);
    return fetch(apiBase() + "/api/desk/client/drive?" + q.toString())
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        var path = "";
        if (target === "chart") path = data.chart_path || data.folder_path || "";
        else if (target === "billing") path = data.billing_path || data.folder_path || "";
        else path = data.folder_path || data.chart_path || "/";
        if (global.PsychartsDrive && typeof global.PsychartsDrive.go === "function") {
          global.PsychartsDrive.go(path || "/");
        } else if (global.PsychartsClient && typeof global.PsychartsClient.openFiles === "function") {
          global.PsychartsClient.openFiles(target, client, path);
        }
      })
      .catch(function () {});
  }

  function doOpen(client, target) {
    if (global.PsychartsClient && typeof global.PsychartsClient.open === "function") {
      return Promise.resolve(global.PsychartsClient.open(client)).then(function () {
        return openDrive(client, target || "home");
      });
    }
    return openDrive(client, target || "files");
  }

  function runSend(job) {
    return post("/api/desk/send", {
      items: job.items,
      client_id: job.client.id || "",
      name: job.client.name || "",
      email: job.client.email || "",
      send_email: true,
    }).then(function (x) {
      if (!x.ok || (x.d && x.d.ok === false)) {
        return { handled: true, reply: (x.d && (x.d.detail || x.d.error)) || "Could not send." };
      }
      return {
        handled: true,
        reply: "Sent " + itemsLabel(job.items) + " to " + (job.client.email || job.client.name) + ".",
      };
    });
  }

  function runBook(job) {
    return post("/api/desk/book", {
      event_type: "follow-up",
      start: isoLocal(job.when),
      duration_minutes: job.duration || 30,
      client_id: job.client.id || "",
      name: job.client.name || "",
      email: job.client.email || "",
      phone: job.client.phone || "",
      send_email: true,
    }).then(function (x) {
      if (!x.ok || (x.d && x.d.ok === false)) {
        return { handled: true, reply: (x.d && (x.d.detail || x.d.error)) || "Could not book." };
      }
      return {
        handled: true,
        reply: "Booked " + (job.client.name || "them") + " · " + whenLabel(job.when) + " · " + (job.duration || 30) + "m.",
      };
    });
  }

  function handle(text) {
    var intent = parse(text);
    if (!intent) return Promise.resolve({ handled: false });

    if (intent.verb === "help") {
      return Promise.resolve({
        handled: true,
        reply: "Call, open, send, or book. Examples: call Jane · open Fuller chart · send Jane intake · book Jane Thursday 2pm. Yes / no confirms. Leftover English still goes to the model.",
      });
    }
    if (intent.verb === "yes") {
      if (!pending) return Promise.resolve({ handled: false });
      var job = pending;
      pending = null;
      if (job.type === "call") {
        if (global.DeskCall) global.DeskCall.place();
        return Promise.resolve({ handled: true, reply: "Placing the call." });
      }
      if (job.type === "send") return runSend(job);
      if (job.type === "book") return runBook(job);
      return Promise.resolve({ handled: true, reply: "Nothing to confirm." });
    }
    if (intent.verb === "no") {
      if (!pending && !(global.DeskCall && DeskCall.pending && DeskCall.pending())) {
        return Promise.resolve({ handled: false });
      }
      pending = null;
      if (global.DeskCall && DeskCall.cancel) DeskCall.cancel();
      return Promise.resolve({ handled: true, reply: "Cancelled." });
    }

    if (intent.verb === "call") {
      return resolveWho(intent.who).then(function (hit) {
        if (!hit.ok) return { handled: true, reply: hit.error };
        var c = hit.client;
        if (!c.phone && !looksPhone(intent.who)) {
          return { handled: true, reply: (c.name || "They") + " has no phone on file." };
        }
        if (global.DeskCall) {
          DeskCall.fromNumber({
            name: c.name || "",
            to: c.phone || intent.who,
            client_id: c.id || "",
          });
          pending = { type: "call", client: c };
        }
        return { handled: true, reply: "Confirm the call on the card, or say yes." };
      });
    }

    if (intent.verb === "open") {
      return resolveWho(intent.who).then(function (hit) {
        if (!hit.ok) return { handled: true, reply: hit.error };
        return doOpen(hit.client, intent.target).then(function () {
          var bit = intent.target === "home" ? "" : " · " + intent.target;
          return { handled: true, reply: "Opened " + (hit.client.name || hit.client.email || "client") + bit + "." };
        });
      });
    }

    if (intent.verb === "send") {
      return resolveWho(intent.who).then(function (hit) {
        if (!hit.ok) return { handled: true, reply: hit.error };
        var c = hit.client;
        if (!c.email) return { handled: true, reply: (c.name || "They") + " has no email on file." };
        pending = { type: "send", client: c, items: intent.items };
        return {
          handled: true,
          reply:
            "Send " +
            itemsLabel(intent.items) +
            " to " +
            (c.name || "them") +
            " at " +
            c.email +
            "? Say yes or no.",
        };
      });
    }

    if (intent.verb === "book") {
      if (!intent.when) {
        return Promise.resolve({
          handled: true,
          reply: "What time? e.g. Thursday 2pm or tomorrow 3:30.",
        });
      }
      return resolveWho(intent.who).then(function (hit) {
        if (!hit.ok) return { handled: true, reply: hit.error };
        var c = hit.client;
        if (!c.email) return { handled: true, reply: (c.name || "They") + " has no email — needed to book." };
        pending = { type: "book", client: c, when: intent.when, duration: intent.duration || 30 };
        return {
          handled: true,
          reply:
            "Book " +
            (c.name || "them") +
            " · " +
            whenLabel(intent.when) +
            " · " +
            (intent.duration || 30) +
            "m? Say yes or no.",
        };
      });
    }

    return Promise.resolve({ handled: false });
  }

  global.PaRouter = {
    handle: handle,
    parse: parse,
    pending: function () {
      return pending;
    },
    clear: function () {
      pending = null;
    },
  };
})(window);
