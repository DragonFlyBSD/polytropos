// Job-detail page behaviour: live activity refresh + client-side
// column sort. Both self-guard on the elements/state they need, so the
// file is a no-op on idle jobs or jobs without an activity table.

// --- Live activity refresh (active jobs only) ---
// Streams SERVER-RENDERED fragments (one render path, shared with the
// initial page render) and lets the shared dpLive helper own the poll
// loop / pause / stop. Two things update off one poll: the turn-card
// stream, swapped whole because a new tool row belongs INSIDE an existing
// card and cannot be prepended, and the raw table, still prepended row by
// row as it always was.
(function () {
  var indicator = document.getElementById("live-indicator");
  if (!indicator) return;
  if (!indicator.classList.contains("active")) return;
  var jobId = indicator.dataset.jobId;
  var sinceId = parseInt(indicator.dataset.sinceId || "0", 10);
  var stageFilter = indicator.dataset.stageFilter || "";
  var rowLimit = indicator.dataset.limit || "";
  var tbody = document.getElementById("activity-tbody");
  var cardsEl = document.getElementById("turn-cards");
  var barSlot = document.getElementById("now-bar-slot");
  var lastUpdateEl = indicator.querySelector(".last-update");
  var statusText = indicator.querySelector(".status-text");
  var pauseLink = document.getElementById("pause-toggle");
  var TERMINAL = ["done", "dead", "escalated"];

  function fmtAgo(ts) {
    var seconds = Math.round((Date.now() - ts) / 1000);
    if (seconds < 5) return "just now";
    if (seconds < 60) return seconds + "s ago";
    return Math.floor(seconds / 60) + "m ago";
  }

  var poller = window.dpLive({
    intervalMs: 3000,
    url: function () {
      var u = "/api/jobs/" + encodeURIComponent(jobId)
            + "/activity-fragment?since_id=" + sinceId;
      if (stageFilter) u += "&stage_filter=" + encodeURIComponent(stageFilter);
      if (rowLimit) u += "&limit=" + encodeURIComponent(rowLimit);
      return u;
    },
    onData: function (data) {
      if (data.nowbar_html !== undefined && barSlot) {
        barSlot.innerHTML = data.nowbar_html;
        startElapsedClock();
      }
      if (data.cards_html && cardsEl) {
        // Swap the whole stream. Which <details> the operator had open is
        // page state the server can't know, so carry it across by the
        // data-key the template stamps on each one.
        var open = {};
        Array.prototype.forEach.call(
          cardsEl.querySelectorAll("details[data-key]"), function (d) {
            if (d.open) open[d.dataset.key] = true;
          });
        cardsEl.innerHTML = data.cards_html;
        startTailClock();
        Array.prototype.forEach.call(
          cardsEl.querySelectorAll("details[data-key]"), function (d) {
            if (open[d.dataset.key]) d.open = true;
          });
      }
      // The cursor advances whatever the page is showing: the raw table
      // lives on the transcript page now, so tbody is usually absent and
      // the update must not hang off it.
      if (data.since_id) sinceId = data.since_id;
      if (data.html && tbody) {
        // Rows arrive oldest-first; inserting each at the top makes the
        // newest land highest, matching the newest-first static table.
        var frag = document.createElement("tbody");
        frag.innerHTML = data.html;
        Array.prototype.forEach.call(frag.querySelectorAll("tr"), function (tr) {
          tr.classList.add("new-row");
          tbody.insertBefore(tr, tbody.firstChild);
        });
      }
      if (lastUpdateEl) lastUpdateEl.textContent = fmtAgo(Date.now());
      if (TERMINAL.indexOf(data.job_state) >= 0) {
        indicator.classList.remove("active");
        if (statusText) statusText.textContent = "idle (" + data.job_state + ")";
        if (pauseLink) pauseLink.style.display = "none";
        return "stop";
      }
    },
  });

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && statusText && !poller.isPaused()) {
      statusText.textContent = "live";
    }
  });

  if (pauseLink) {
    pauseLink.addEventListener("click", function (ev) {
      ev.preventDefault();
      if (poller.isPaused()) {
        poller.resume();
        pauseLink.textContent = "[pause]";
        if (statusText) statusText.textContent = "live";
      } else {
        poller.pause();
        pauseLink.textContent = "[resume]";
        if (statusText) statusText.textContent = "paused";
      }
    });
  }

  poller.start(3000);
})();

// --- The now-bar's elapsed clock ---
// The bar names the tool running right now; without a clock beside it a
// 44-minute dsynth build and a wedged runner look identical, which is
// the whole reason tool_start exists (poly-qqx9.2, poly-qqx9.3).
function startElapsedClock() {
  if (window._dpElapsedTimer) clearInterval(window._dpElapsedTimer);
  var el = document.getElementById("nb-elapsed");
  var host = el && el.closest("[data-since]");
  if (!el || !host) return;
  var since = Date.parse(host.dataset.since);
  if (isNaN(since)) return;
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function tick() {
    // Two units, never three: at 1h53m the seconds are noise, and at
    // 42s the minutes are a lie about precision.
    var s = Math.max(0, Math.round((Date.now() - since) / 1000));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    el.textContent = h ? h + "h" + pad(m) + "m"
      : m ? m + "m" + pad(s % 60) + "s"
      : s + "s";
  }
  tick();
  window._dpElapsedTimer = setInterval(tick, 1000);
}
startElapsedClock();

// --- Client-side column sort ---
(function () {
  var table = document.getElementById("activity-table");
  if (!table) return;
  var tbody = document.getElementById("activity-tbody");
  var sortLabel = document.getElementById("sort-label");
  var sortReset = document.getElementById("sort-reset");
  var headers = table.querySelectorAll("th.sortable");

  // Snapshot the initial row order so [reset] can restore it.
  var originalOrder = Array.from(tbody.querySelectorAll("tr"));

  var current = { key: null, dir: 0 };  // dir: 0=none, 1=desc, -1=asc

  function applySort(key, dir) {
    var rows = Array.from(tbody.querySelectorAll("tr"));
    rows.sort(function (a, b) {
      var av = parseInt(a.dataset["sort" + key.charAt(0).toUpperCase() + key.slice(1)] || "0", 10);
      var bv = parseInt(b.dataset["sort" + key.charAt(0).toUpperCase() + key.slice(1)] || "0", 10);
      return dir === 1 ? bv - av : av - bv;
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
    headers.forEach(function (h) {
      h.classList.remove("sort-asc", "sort-desc");
      if (h.dataset.sort === key) {
        h.classList.add(dir === 1 ? "sort-desc" : "sort-asc");
      }
    });
    if (sortLabel) sortLabel.textContent = key + " " + (dir === 1 ? "↓" : "↑");
    if (sortReset) sortReset.style.display = "inline";
  }

  function reset() {
    originalOrder.forEach(function (r) { tbody.appendChild(r); });
    headers.forEach(function (h) {
      h.classList.remove("sort-asc", "sort-desc");
    });
    current = { key: null, dir: 0 };
    if (sortLabel) sortLabel.textContent = "chronological";
    if (sortReset) sortReset.style.display = "none";
  }

  headers.forEach(function (h) {
    h.addEventListener("click", function () {
      var key = h.dataset.sort;
      if (current.key !== key) {
        current = { key: key, dir: 1 };       // first click = desc
      } else if (current.dir === 1) {
        current = { key: key, dir: -1 };       // second = asc
      } else {
        reset();
        return;
      }
      applySort(current.key, current.dir);
    });
  });

  if (sortReset) {
    sortReset.addEventListener("click", function (ev) {
      ev.preventDefault();
      reset();
    });
  }
})();

// --- Abandon job (mark dead) ---
(function () {
  var btn = document.getElementById("abandon-btn");
  if (!btn) return;
  var flash = document.getElementById("abandon-flash");
  btn.addEventListener("click", async function () {
    var msg = "Abandon job " + btn.dataset.jobId + " (state="
      + btn.dataset.state + ")? It will be marked dead with "
      + "retire_reason='abandoned' and never picked up again.";
    if (!confirm(msg)) return;
    btn.disabled = true;
    try {
      var resp = await fetch(
        "/api/jobs/" + encodeURIComponent(btn.dataset.jobId) + "/abandon",
        {method: "POST", headers: {"Content-Type": "application/json"}}
      );
      var data = await resp.json().catch(function () { return {}; });
      if (resp.ok) {
        flash.textContent = "Abandoned. Refreshing…";
        flash.style.color = "green";
        setTimeout(function () { window.location.reload(); }, 700);
      } else {
        flash.textContent = (data.detail || ("HTTP " + resp.status));
        flash.style.color = "var(--c-fail, #c00)";
        btn.disabled = false;
      }
    } catch (err) {
      flash.textContent = "Network error: " + err;
      flash.style.color = "var(--c-fail, #c00)";
      btn.disabled = false;
    }
  });
})();

// --- "last line N ago" on a running build's tail ---
// The live badge reports the poll; this reports the JOB. A build that
// has printed nothing for four minutes looks identical to a healthy one
// without it (poly-qqx9.8).
function startTailClock() {
  if (window._dpTailTimer) clearInterval(window._dpTailTimer);
  function tick() {
    document.querySelectorAll(".tool-tail[data-mtime]").forEach(function (el) {
      var badge = el.parentNode.querySelector(".tail-ago");
      var mtime = parseFloat(el.dataset.mtime);
      if (!badge || isNaN(mtime)) return;
      var s = Math.max(0, Math.round(Date.now() / 1000 - mtime));
      badge.textContent = s < 60 ? s + "s"
        : Math.floor(s / 60) + "m" + (s % 60 < 10 ? "0" : "") + (s % 60) + "s";
    });
  }
  tick();
  window._dpTailTimer = setInterval(tick, 1000);
}
startTailClock();
