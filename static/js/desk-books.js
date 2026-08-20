/**
 * Jane's books chip + charge confirm. No fourth tile.
 */
(function (global) {
  "use strict";

  function esc(s) {
    return String(s || "").replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function booksChip(books) {
    books = books || {};
    var owed = Number(books.owed_cents || 0);
    if (owed > 0) {
      return (
        '<span class="home-chip owed">' + esc(books.owed_pretty || "") + " owed</span>"
      );
    }
    return '<span class="home-chip ok">Current</span>';
  }

  function linesHtml(books) {
    var lines = (books && books.lines) || [];
    if (!lines.length) return "";
    return (
      "<ul class='home-list home-books-list'>" +
      lines
        .map(function (ln) {
          return (
            "<li><span>" +
            esc(ln.dos || "") +
            (ln.note ? " · " + esc(ln.note) : "") +
            "</span><span class='desk-meta'>" +
            esc(ln.amount_pretty || "") +
            " · " +
            esc(ln.status || "") +
            "</span></li>"
          );
        })
        .join("") +
      "</ul>"
    );
  }

  function formHtml(books, cardOk) {
    books = books || {};
    var today = new Date().toISOString().slice(0, 10);
    var amt = (books.suggest_pretty || "$0.00").replace("$", "").replace(",", "");
    var inv = books.invoice_url
      ? '<p class="home-kv"><a href="' +
        esc(books.invoice_url) +
        '" target="_blank" rel="noopener">Open pay link</a>' +
        ' · <button type="button" class="desk-num" id="hm-inv-copy">Copy</button></p>'
      : "";
    return (
      '<div class="home-sec home-books" id="hm-books">' +
      "<h4>Books</h4>" +
      linesHtml(books) +
      inv +
      '<div class="home-actions" style="margin:0.35rem 0 0">' +
      (Number(books.owed_cents || 0) > 0
        ? '<button class="btn secondary" type="button" id="hm-invoice">Invoice owed</button>'
        : "") +
      "</div>" +
      '<div class="home-charge" id="hm-charge-box" hidden>' +
      '<label class="field">Amount <input id="hm-fee-amt" inputmode="decimal" value="' +
      esc(amt) +
      '" /></label>' +
      '<label class="field">Date <input id="hm-fee-dos" type="date" value="' +
      esc(today) +
      '" /></label>' +
      '<label class="field">Note <input id="hm-fee-note" maxlength="80" placeholder="30m visit" /></label>' +
      '<p class="home-empty" id="hm-fee-st" style="margin:0.35rem 0 0"></p>' +
      '<div class="home-actions" style="margin-top:0.4rem">' +
      (cardOk
        ? '<button class="btn" type="button" id="hm-fee-go">Charge card on file</button>'
        : "") +
      '<button class="btn secondary" type="button" id="hm-fee-owed">Add to books</button>' +
      "</div></div></div>"
    );
  }

  function bind(root, opts) {
    opts = opts || {};
    var box = root.querySelector("#hm-charge-box");
    var toggle = root.querySelector("#hm-charge");
    if (toggle && box) {
      toggle.onclick = function () {
        box.hidden = !box.hidden;
      };
    }
    function post(charge) {
      var st = root.querySelector("#hm-fee-st");
      var amt = root.querySelector("#hm-fee-amt");
      var dos = root.querySelector("#hm-fee-dos");
      var note = root.querySelector("#hm-fee-note");
      if (!opts.clientId) {
        if (st) st.textContent = "Open a directory client first.";
        return;
      }
      if (st) st.textContent = charge ? "Charging…" : "Saving…";
      var url =
        String(opts.apiBase || "").replace(/\/$/, "") +
        "/api/desk/fees";
      (window.deskFetch || fetch)(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          client_id: opts.clientId,
          dos: dos ? dos.value : "",
          amount: amt ? amt.value : "",
          note: note ? note.value : "",
          kind: "visit",
          event_id: opts.eventId || "",
          charge: !!charge,
        }),
      })
        .then(function (r) {
          return r.text().then(function (t) {
            var d = {};
            try {
              d = t ? JSON.parse(t) : {};
            } catch (_) {
              d = { detail: t.slice(0, 160) };
            }
            return { ok: r.ok, d: d };
          });
        })
        .then(function (x) {
          if (!x.ok || !x.d || !x.d.ok) {
            if (st) st.textContent = (x.d && (x.d.detail || x.d.error)) || "Could not post.";
            return;
          }
          if (typeof opts.onDone === "function") opts.onDone(x.d);
        })
        .catch(function (e) {
          if (st) st.textContent = (e && e.message) || "failed";
        });
    }
    var go = root.querySelector("#hm-fee-go");
    var add = root.querySelector("#hm-fee-owed");
    if (go) go.onclick = function () { post(true); };
    if (add) add.onclick = function () { post(false); };
    var invBtn = root.querySelector("#hm-invoice");
    var copyBtn = root.querySelector("#hm-inv-copy");
    var booksBox = root.querySelector("#hm-books");
    function say(t) {
      var st = root.querySelector("#hm-fee-st");
      if (!st && booksBox) {
        st = document.createElement("p");
        st.className = "home-empty";
        st.id = "hm-fee-st";
        st.style.margin = "0.35rem 0 0";
        booksBox.appendChild(st);
      }
      if (st) st.textContent = t || "";
    }
    if (copyBtn && opts.books && opts.books.invoice_url) {
      copyBtn.onclick = function () {
        var u = opts.books.invoice_url;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(u).then(function () { say("Pay link copied."); });
        }
      };
    }
    if (invBtn) {
      invBtn.onclick = function () {
        if (!opts.clientId) {
          say("Open a directory client first.");
          return;
        }
        say("Making invoice…");
        var url =
          String(opts.apiBase || "").replace(/\/$/, "") +
          "/api/desk/fees/invoice";
        (window.deskFetch || fetch)(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ client_id: opts.clientId }),
        })
          .then(function (r) {
            return r.text().then(function (t) {
              var d = {};
              try {
                d = t ? JSON.parse(t) : {};
              } catch (_) {
                d = { detail: t.slice(0, 160) };
              }
              return { ok: r.ok, d: d };
            });
          })
          .then(function (x) {
            if (!x.ok || !x.d || !x.d.ok) {
              say((x.d && (x.d.detail || x.d.error)) || "Could not invoice.");
              return;
            }
            if (x.d.pay_url && navigator.clipboard && navigator.clipboard.writeText) {
              navigator.clipboard.writeText(x.d.pay_url);
            }
            if (typeof opts.onDone === "function") opts.onDone(x.d);
          })
          .catch(function (e) {
            say((e && e.message) || "failed");
          });
      };
    }
    var books = opts.books || {};
    if (root.querySelector("#hm-fee-note") && books.suggest_pretty && !root.querySelector("#hm-fee-note").value) {
      var cents = Number(books.suggest_cents || 0);
      var hint = cents === 60000 ? "90m visit" : cents === 42000 ? "60m visit" : "30m visit";
      root.querySelector("#hm-fee-note").placeholder = hint;
    }
    if (opts.dos && root.querySelector("#hm-fee-dos")) {
      root.querySelector("#hm-fee-dos").value = opts.dos;
    }
  }

  global.DeskBooks = {
    chip: booksChip,
    form: formHtml,
    bind: bind,
  };
})(window);
