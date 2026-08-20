async function fetchJSON(url, opts) {
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.detail || data.message || res.statusText || "Request failed";
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

function el(id) {
  return document.getElementById(id);
}

function setMsg(node, text, kind) {
  if (!node) return;
  node.className = "msg " + (kind || "info");
  node.textContent = text;
  node.hidden = !text;
}

const TZ = "America/Chicago";

function dateKeyFromIso(iso) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(iso));
}

function timeLabelFromIso(iso) {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: TZ,
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(iso));
}

function fullLabelFromIso(iso) {
  const d = new Date(iso);
  const day = new Intl.DateTimeFormat("en-US", {
    timeZone: TZ,
    weekday: "short",
    month: "short",
    day: "numeric",
  }).format(d);
  return day + " · " + timeLabelFromIso(iso);
}

function parseKey(key) {
  const [y, m, d] = key.split("-").map(Number);
  return { y, m, d };
}

function formatMonthTitle(year, monthIndex0) {
  const d = new Date(Date.UTC(year, monthIndex0, 1));
  return new Intl.DateTimeFormat("en-US", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(d);
}

function formatDayHeading(key) {
  const { y, m, d } = parseKey(key);
  const date = new Date(Date.UTC(y, m - 1, d, 12));
  return new Intl.DateTimeFormat("en-US", {
    weekday: "long",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  }).format(date);
}

function groupSlotsByDay(slots) {
  const map = new Map();
  for (const s of slots) {
    const key = dateKeyFromIso(s.start);
    if (!map.has(key)) map.set(key, []);
    map.get(key).push({
      ...s,
      timeLabel: timeLabelFromIso(s.start),
      fullLabel: s.label || fullLabelFromIso(s.start),
    });
  }
  return map;
}

function monthKeys(year, monthIndex0) {
  const first = new Date(year, monthIndex0, 1);
  const startDow = first.getDay();
  const daysInMonth = new Date(year, monthIndex0 + 1, 0).getDate();
  return { startDow, daysInMonth };
}

function priceForDuration(opts, minutes) {
  if (opts.prices && opts.prices[minutes] != null) return opts.prices[minutes];
  if (opts.prices && opts.prices[String(minutes)] != null) return opts.prices[String(minutes)];
  if (opts.price) return opts.price;
  return "";
}

function timesSubLabel(opts, minutes) {
  const price = priceForDuration(opts, minutes);
  const short = minutes + "-min";
  if (price) return short + " · " + price + " · Chicago time";
  return durationPhrase(minutes) + " openings · Chicago time";
}

function durationPhrase(minutes) {
  if (minutes === 60) return "60-minute";
  if (minutes === 90) return "90-minute";
  if (minutes === 15) return "15-minute";
  return minutes + "-minute";
}




const NOTIFY_EXTRA_MAX = 4;

function collectNotifyEmails(root) {
  const scope = root || document;
  const vals = [];
  scope.querySelectorAll("input[data-notify-extra]").forEach((inp) => {
    const v = (inp.value || "").trim();
    if (v) vals.push(v);
  });
  return vals.join(", ");
}

function renumberNotifyExtras(fields) {
  if (!fields) return;
  fields.querySelectorAll(".notify-extra-row").forEach((row, idx) => {
    const lab = row.querySelector(".notify-extra-field-label");
    if (lab) lab.textContent = "Extra email " + (idx + 1);
  });
}

function refreshNotifyExtrasUI(wrap) {
  if (!wrap) return;
  const fields = wrap.querySelector(".notify-extra-fields");
  const addBtn = wrap.querySelector(".notify-add-btn");
  if (!fields || !addBtn) return;
  const n = fields.querySelectorAll("input[data-notify-extra]").length;
  addBtn.hidden = n >= NOTIFY_EXTRA_MAX;
  addBtn.disabled = n >= NOTIFY_EXTRA_MAX;
}

function addNotifyExtraField(wrap) {
  if (!wrap) return;
  const fields = wrap.querySelector(".notify-extra-fields");
  if (!fields) return;
  const n = fields.querySelectorAll("input[data-notify-extra]").length;
  if (n >= NOTIFY_EXTRA_MAX) return;

  const row = document.createElement("div");
  row.className = "notify-extra-row";
  const idx = n + 1;
  row.innerHTML =
    '<div class="notify-extra-field-label">Extra email ' +
    idx +
    "</div>" +
    '<div class="notify-extra-row-inner">' +
    '<input type="email" data-notify-extra="1" autocomplete="email" placeholder="name@example.com" />' +
    '<button type="button" class="notify-remove-btn" aria-label="Remove this email" onclick="return window.bookerRemoveNotifyExtra(this)">×</button>' +
    "</div>";
  fields.appendChild(row);
  renumberNotifyExtras(fields);
  refreshNotifyExtrasUI(wrap);
  const input = row.querySelector("input");
  if (input) input.focus();
}

// Document-level handlers so + works even if init timing varies
window.bookerAddNotifyExtra = function (btn) {
  try {
    const el = btn && btn.nodeType ? btn : null;
    const wrap = (el && el.closest && el.closest(".notify-extra-wrap")) || document.querySelector(".notify-extra-wrap");
    addNotifyExtraField(wrap);
  } catch (e) {
    console.error("bookerAddNotifyExtra", e);
  }
  return false;
};

window.bookerRemoveNotifyExtra = function (btn) {
  try {
    const wrap = btn && btn.closest && btn.closest(".notify-extra-wrap");
    const row = btn && btn.closest && btn.closest(".notify-extra-row");
    if (row) row.remove();
    if (wrap) {
      renumberNotifyExtras(wrap.querySelector(".notify-extra-fields"));
      refreshNotifyExtrasUI(wrap);
    }
  } catch (e) {
    console.error("bookerRemoveNotifyExtra", e);
  }
  return false;
};

if (!window.__bookerNotifyExtrasBound) {
  window.__bookerNotifyExtrasBound = true;
  document.addEventListener(
    "click",
    (ev) => {
      const addBtn = ev.target && ev.target.closest && ev.target.closest(".notify-add-btn");
      if (addBtn) {
        ev.preventDefault();
        ev.stopPropagation();
        addNotifyExtraField(addBtn.closest(".notify-extra-wrap"));
        return;
      }
      const rm = ev.target && ev.target.closest && ev.target.closest(".notify-remove-btn");
      if (rm) {
        ev.preventDefault();
        ev.stopPropagation();
        const wrap = rm.closest(".notify-extra-wrap");
        const row = rm.closest(".notify-extra-row");
        if (row) row.remove();
        if (wrap) {
          renumberNotifyExtras(wrap.querySelector(".notify-extra-fields"));
          refreshNotifyExtrasUI(wrap);
        }
      }
    },
    true // capture: beat other handlers
  );
}


export async function initBooker(opts) {
  const type = opts.type;
  const visitTitle = opts.title || type;
  const durationEl = el("duration");
  const calGrid = el("cal-grid");
  const calLabel = el("cal-month-label");
  const calPrev = el("cal-prev");
  const calNext = el("cal-next");
  const timesHeader = el("times-header");
  const timesSub = el("times-sub");
  const slotsEl = el("slots");
  const formEl = el("book-form");
  const msgEl = el("msg");
  const modalMsgEl = el("modal-msg");
  const submitBtn = el("submit");
  const selectionChip = el("selection-chip");
  const chipMain = el("chip-main");
  const chipMeta = el("chip-meta");
  const modal = el("details-modal");
  const modalClose = el("details-modal-close");
  const modalBackdrop = el("details-modal-backdrop");
  const pillWhen = el("pill-when");
  const pillDetails = el("pill-details");
  const pillDuration = el("pill-duration");

  let selected = null;
  let duration = opts.defaultDuration;
  let byDay = new Map();
  let availableKeys = [];
  let selectedDay = null;
  let viewYear = null;
  let viewMonth = null;
  let loading = false;
  let booking = false;
  let lastFocus = null;

  function setStepPills(active) {
    const map = [
      [pillDuration, "duration"],
      [pillWhen, "when"],
      [pillDetails, "details"],
    ];
    const order = ["duration", "when", "details"];
    const ai = order.indexOf(active);
    for (const [node, key] of map) {
      if (!node) continue;
      const ki = order.indexOf(key);
      node.classList.toggle("is-active", key === active);
      node.classList.toggle("is-done", ki < ai);
    }
  }

  function openModal() {
    if (!modal) return;
    lastFocus = document.activeElement;
    modal.classList.add("is-open");
    modal.setAttribute("aria-hidden", "false");
    document.body.classList.add("modal-open");
    setStepPills("details");
    const first = formEl && formEl.querySelector("input,textarea,button");
    setTimeout(() => {
      if (first) first.focus();
    }, 80);
  }

  function closeModal() {
    if (!modal) return;
    if (booking) return; // do not dismiss while calendar write is in flight
    modal.classList.remove("is-open");
    modal.classList.remove("is-booking");
    modal.setAttribute("aria-hidden", "true");
    document.body.classList.remove("modal-open");
    setStepPills(selected ? "when" : durationEl ? "duration" : "when");
    if (selected) setStepPills("when");
    if (lastFocus && lastFocus.focus) {
      try {
        lastFocus.focus();
      } catch (e) {}
    }
  }

  function clearTimeSelection() {
    selected = null;
    if (selectionChip) selectionChip.hidden = true;
    if (submitBtn) submitBtn.disabled = true;
    if (slotsEl) {
      for (const x of slotsEl.querySelectorAll(".slot")) x.classList.remove("selected");
    }
    closeModal();
    setStepPills(selectedDay ? "when" : durationEl ? "duration" : "when");
  }

  function showSelection() {
    if (!selected) return;
    if (selectionChip) selectionChip.hidden = false;
    if (chipMain) chipMain.textContent = selected.fullLabel;
    if (chipMeta) {
      const bits = [
        durationPhrase(duration),
        visitTitle,
        priceForDuration(opts, duration),
        opts.location || "Video · meet.drmattbrown.com",
        "America/Chicago",
      ].filter(Boolean);
      chipMeta.textContent = bits.join(" · ");
    }
    // mirror into modal chips if separate
    const mMain = el("modal-chip-main");
    const mMeta = el("modal-chip-meta");
    if (mMain) mMain.textContent = selected.fullLabel;
    if (mMeta) {
      const bits = [
        durationPhrase(duration),
        visitTitle,
        priceForDuration(opts, duration),
        opts.location || "Video · meet.drmattbrown.com",
        "America/Chicago",
      ].filter(Boolean);
      mMeta.textContent = bits.join(" · ");
    }
    if (submitBtn) submitBtn.disabled = false;
    openModal();
  }

  function renderTimes() {
    if (!slotsEl) return;
    slotsEl.innerHTML = "";
    if (loading) {
      if (timesHeader) timesHeader.textContent = "Loading times…";
      if (timesSub) timesSub.textContent = "";
      const p = document.createElement("p");
      p.className = "times-loading";
      p.textContent = "Finding open times…";
      slotsEl.appendChild(p);
      return;
    }
    if (!selectedDay) {
      if (timesHeader) timesHeader.textContent = "Select a date";
      if (timesSub) {
        timesSub.textContent =
          timesSubLabel(opts, duration);
      }
      return;
    }
    if (timesHeader) timesHeader.textContent = formatDayHeading(selectedDay);
    if (timesSub) {
      timesSub.textContent =
        timesSubLabel(opts, duration);
    }
    const list = byDay.get(selectedDay) || [];
    if (!list.length) {
      const empty = document.createElement("p");
      empty.className = "times-empty";
      empty.textContent = "No open times this day.";
      slotsEl.appendChild(empty);
      return;
    }
    for (const s of list) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "slot";
      b.textContent = s.timeLabel;
      b.dataset.start = s.start;
      b.setAttribute("aria-label", s.fullLabel + ", " + durationPhrase(duration));
      if (selected && selected.start === s.start) b.classList.add("selected");
      b.addEventListener("click", () => {
        selected = s;
        for (const x of slotsEl.querySelectorAll(".slot")) x.classList.remove("selected");
        b.classList.add("selected");
        showSelection();
      });
      slotsEl.appendChild(b);
    }
  }

  function renderCalendar() {
    if (!calGrid || viewYear == null) return;
    calGrid.innerHTML = "";
    if (calLabel) calLabel.textContent = formatMonthTitle(viewYear, viewMonth);

    const { startDow, daysInMonth } = monthKeys(viewYear, viewMonth);
    const available = new Set(availableKeys);

    for (let i = 0; i < startDow; i++) {
      const pad = document.createElement("span");
      pad.className = "cal-day is-pad";
      pad.setAttribute("aria-hidden", "true");
      calGrid.appendChild(pad);
    }

    for (let day = 1; day <= daysInMonth; day++) {
      const key =
        viewYear +
        "-" +
        String(viewMonth + 1).padStart(2, "0") +
        "-" +
        String(day).padStart(2, "0");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cal-day";
      btn.textContent = String(day);
      btn.dataset.date = key;

      const has = available.has(key);
      if (!has) {
        btn.disabled = true;
        btn.classList.add("is-muted");
      } else {
        btn.classList.add("is-available");
        btn.addEventListener("click", () => {
          selectedDay = key;
          clearTimeSelection();
          setStepPills("when");
          renderCalendar();
          renderTimes();
        });
      }
      if (selectedDay === key) btn.classList.add("is-selected");

      const todayKey = new Intl.DateTimeFormat("en-CA", {
        timeZone: TZ,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }).format(new Date());
      if (key === todayKey) btn.classList.add("is-today");

      calGrid.appendChild(btn);
    }

    if (calPrev && calNext && availableKeys.length) {
      const first = availableKeys[0];
      const last = availableKeys[availableKeys.length - 1];
      const firstY = parseInt(first.slice(0, 4), 10);
      const firstM = parseInt(first.slice(5, 7), 10) - 1;
      const lastY = parseInt(last.slice(0, 4), 10);
      const lastM = parseInt(last.slice(5, 7), 10) - 1;
      calPrev.disabled =
        viewYear < firstY || (viewYear === firstY && viewMonth <= firstM);
      calNext.disabled =
        viewYear > lastY || (viewYear === lastY && viewMonth >= lastM);
    }
  }

  function setViewFromKey(key) {
    const { y, m } = parseKey(key);
    viewYear = y;
    viewMonth = m - 1;
  }

  async function loadSlots() {
    loading = true;
    clearTimeSelection();
    byDay = new Map();
    availableKeys = [];
    selectedDay = null;
    renderTimes();
    setMsg(msgEl, "", "info");
    setMsg(modalMsgEl, "", "info");

    try {
      const q = new URLSearchParams({ type, duration: String(duration) });
      const data = await fetchJSON("/api/slots?" + q.toString());
      loading = false;
      if (!data.slots || !data.slots.length) {
        setMsg(
          msgEl,
          "No open times for a " + durationPhrase(duration) + " visit right now.",
          "info"
        );
        if (calGrid) calGrid.innerHTML = "";
        if (calLabel) calLabel.textContent = "";
        if (timesHeader) timesHeader.textContent = "No times";
        if (timesSub) timesSub.textContent = timesSubLabel(opts, duration);
        if (slotsEl) slotsEl.innerHTML = "";
        return;
      }
      byDay = groupSlotsByDay(data.slots);
      availableKeys = Array.from(byDay.keys()).sort();
      selectedDay = availableKeys[0];
      setViewFromKey(selectedDay);
      setStepPills("when");
      renderCalendar();
      renderTimes();
    } catch (e) {
      loading = false;
      setMsg(msgEl, e.message || String(e), "err");
      renderTimes();
    }
  }

  if (calPrev) {
    calPrev.addEventListener("click", () => {
      if (viewMonth === 0) {
        viewMonth = 11;
        viewYear -= 1;
      } else {
        viewMonth -= 1;
      }
      renderCalendar();
    });
  }
  if (calNext) {
    calNext.addEventListener("click", () => {
      if (viewMonth === 11) {
        viewMonth = 0;
        viewYear += 1;
      } else {
        viewMonth += 1;
      }
      renderCalendar();
    });
  }

  function applyPrefill() {
    const q = new URLSearchParams(window.location.search);
    const name = (q.get("name") || q.get("n") || "").trim();
    const email = (q.get("email") || q.get("e") || "").trim();
    const phone = (q.get("phone") || q.get("p") || "").trim();
    const durRaw = q.get("duration") || q.get("d") || "";
    const durQ = parseInt(durRaw, 10);
    if (formEl) {
      const nameEl = formEl.querySelector('[name="name"]');
      const emailEl = formEl.querySelector('[name="email"]');
      const phoneEl = formEl.querySelector('[name="phone"]');
      if (nameEl && name) nameEl.value = name;
      if (emailEl && email) emailEl.value = email;
      if (phoneEl && phone) phoneEl.value = phone;
    }
    if (durQ && durationEl) {
      const btn = durationEl.querySelector('[data-duration="' + durQ + '"]');
      if (btn) {
        durationEl.querySelectorAll("[data-duration]").forEach((x) => {
          x.classList.remove("active");
          x.setAttribute("aria-pressed", "false");
        });
        btn.classList.add("active");
        btn.setAttribute("aria-pressed", "true");
        duration = durQ;
      }
    }
    if (name || email) {
      const who = [name, email].filter(Boolean).join(" · ");
      setMsg(msgEl, "Details ready for " + who + " — pick a time to finish.", "info");
    }
  }
  applyPrefill();

  if (durationEl) {
    durationEl.querySelectorAll("[data-duration]").forEach((btn) => {
      btn.addEventListener("click", () => {
        durationEl.querySelectorAll("[data-duration]").forEach((x) => {
          x.classList.remove("active");
          x.setAttribute("aria-pressed", "false");
        });
        btn.classList.add("active");
        btn.setAttribute("aria-pressed", "true");
        duration = parseInt(btn.dataset.duration, 10);
        setStepPills("when");
        loadSlots();
      });
    });
  }

  if (modalClose) modalClose.addEventListener("click", closeModal);
  if (modalBackdrop) modalBackdrop.addEventListener("click", closeModal);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && modal && modal.classList.contains("is-open")) {
      closeModal();
    }
  });

  // re-open modal if chip clicked after close
  if (selectionChip) {
    selectionChip.addEventListener("click", () => {
      if (selected) openModal();
    });
    selectionChip.classList.add("selection-chip-inline");
  }

  if (formEl) {
    formEl.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (!selected) {
        setMsg(modalMsgEl || msgEl, "Pick a date and time first.", "err");
        return;
      }
      if (booking) return;
      const fd = new FormData(formEl);
      booking = true;
      if (modal) modal.classList.add("is-booking");
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = "Booking…";
      }
      // Lock form fields so it is obvious work is in progress
      formEl.querySelectorAll("input,textarea,button").forEach((node) => {
        if (node !== submitBtn) node.disabled = true;
      });
      setMsg(
        modalMsgEl || msgEl,
        "Confirming on the calendar… almost done.",
        "info"
      );
      try {
        const data = await fetchJSON("/api/book", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            event_type: type,
            start: selected.start,
            duration_minutes: duration,
            name: fd.get("name"),
            email: fd.get("email"),
            phone: fd.get("phone") || "",
            notes: fd.get("notes") || "",
            sms_consent: fd.get("sms_consent") === "on" || fd.get("sms_consent") === "true",
            notify_emails: collectNotifyEmails(formEl || document),
          }),
        });
        const params = new URLSearchParams({
          name: fd.get("name"),
          when: selected.fullLabel || selected.label || data.when_label || "",
          type: visitTitle + " (" + duration + " min)",
        });
        const priceLabel = priceForDuration(opts, duration) || data.price_label || "";
        if (priceLabel) params.set("price", priceLabel);
        if (data.cancel_url) params.set("cancel", data.cancel_url);
        if (data.ics_url) params.set("ics", data.ics_url);
        // Hard navigate immediately after calendar write (emails go out in background)
        window.location.replace("/thanks?" + params.toString());
      } catch (e) {
        booking = false;
        if (modal) modal.classList.remove("is-booking");
        formEl.querySelectorAll("input,textarea,button").forEach((node) => {
          node.disabled = false;
        });
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = "Confirm booking";
        }
        setMsg(modalMsgEl || msgEl, e.message || String(e), "err");
        loadSlots();
      }
    });
  }

  if (durationEl) setStepPills("duration");
  else setStepPills("when");
  await loadSlots();
}

window.PsychArtsBooker = { initBooker };
