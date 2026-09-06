/* The run view: one build run, live.
 *
 * Replaces the lifted dsynth-progress client. Same data contract --
 * summary.json polled while the run is active, plus NN_history.json chunks
 * bounded by kfiles and accumulated here -- and a different renderer.
 *
 * Two rules the old client broke:
 *
 *  - Nothing is built from HTML strings. Origins, versions and bundle ids
 *    are database values; they go in through textContent and attributes,
 *    never through innerHTML.
 *  - Nothing synthetic is rendered. The tracker has no builder slots, no
 *    per-port duration and no load average, so those columns are gone
 *    rather than shown empty. See progress_adapter's module docstring.
 */

(function () {
  "use strict";

  var POLL_MS = 10000;
  var perPage = 50;

  var rows = [];          // historical entries, accumulated from the chunks
  var builders = [];      // in-flight rows, replaced by every summary poll
  var view = [];          // rows after filter
  var page = 0;
  var query = "";
  var state = "all";
  var kfiles = 0;
  var loaded = 0;
  var active = false;
  var runId = null;

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function link(href, text, cls) {
    var a = el("a", cls, text);
    a.href = href;
    return a;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function setText(id, value) {
    var n = $(id);
    if (n) n.textContent = (value === undefined || value === null) ? "" : String(value).trim();
  }

  function fetchJSON(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
  }

  /* --- state vocabulary ---------------------------------------------------
   * The chunks carry dsynth's words for a finished row. 'building' rows come
   * from summary.builders instead, and 'queued' is a count only -- a run
   * starts with every origin queued, so they are not in this payload at all.
   */
  var DSYNTH_TO_STATE = {
    built: "success", failed: "failure",
    skipped: "skipped", ignored: "ignored"
  };
  var PILL = {
    success: "green", failure: "red", skipped: "amber",
    ignored: "neutral", building: "cyan", queued: "neutral"
  };

  function stateOf(row) {
    return DSYNTH_TO_STATE[row.result] || row.result || "unknown";
  }

  /* --- summary ----------------------------------------------------------- */

  function applySummary(data) {
    kfiles = parseInt(data.kfiles, 10) || 0;
    active = parseInt(data.active, 10) !== 0;

    // The target view follows the newest run. If that changed underneath us,
    // the whole page is describing the wrong build.
    var seen = data.run_id === undefined ? null : data.run_id;
    if (runId === null) runId = seen;
    else if (seen !== null && seen !== runId) { location.reload(); return; }

    var s = data.stats || {};
    setText("s-built", s.built);
    setText("s-failed", s.failed);
    setText("s-skipped", s.skipped);
    setText("s-ignored", s.ignored);
    setText("s-remains", s.remains);
    setText("s-queued", s.in_queue);
    setText("s-building", s.in_progress);
    setText("s-elapsed", s.elapsed);
    setText("s-total", s.queued);

    drawBar(s);
    builders = data.builders || [];
    drawBuilders(s);

    var dot = $("run-live");
    if (dot) {
      dot.className = active ? "live-dot on" : "live-dot";
      dot.textContent = active ? "live · polls every 10s" : "finished";
    }
    var qlink = $("queued-link");
    if (qlink) qlink.hidden = !(s.in_queue > 0);
  }

  function drawBar(s) {
    var bar = $("run-bar");
    if (!bar) return;
    var total = parseInt(s.queued, 10) || 0;
    clear(bar);
    if (!total) return;
    ["success", "failure", "skipped", "ignored", "building"].forEach(function (key) {
      var n = {
        success: s.built, failure: s.failed, skipped: s.skipped,
        ignored: s.ignored, building: s.in_progress
      }[key];
      n = parseInt(n, 10) || 0;
      if (!n) return;
      var seg = el("i", "seg-" + key);
      seg.style.width = (n * 100 / total) + "%";
      bar.appendChild(seg);
    });
    bar.setAttribute(
      "aria-label",
      (s.built || 0) + " built, " + (s.failed || 0) + " failed of " + total + " expected"
    );
  }

  function drawBuilders(s) {
    var body = $("builders-body");
    var panel = $("builders-panel");
    if (!body || !panel) return;
    panel.hidden = !active;
    clear(body);
    setText("builders-count", builders.length);
    if (!builders.length) {
      var tr = el("tr");
      var td = el("td", null, active ? "Nothing is building right now." : "");
      td.colSpan = 2;
      td.style.color = "var(--faint)";
      tr.appendChild(td);
      body.appendChild(tr);
      return;
    }
    builders.forEach(function (b) {
      var tr = el("tr");
      tr.appendChild(originCell(b.origin));
      tr.appendChild(el("td", "tabular", b.version || "—"));
      body.appendChild(tr);
    });
  }

  /* --- history ----------------------------------------------------------- */

  var fetching = false;

  function loadChunks() {
    if (fetching || loaded >= kfiles) return Promise.resolve();
    fetching = true;
    var wanted = [];
    for (var k = loaded + 1; k <= kfiles; k++) wanted.push(k);
    setText("chunk-note", "loading " + wanted.length + " of " + kfiles + " chunks");
    return Promise.all(wanted.map(function (k) {
      var name = (k > 9 ? "" : "0") + k + "_history.json";
      return fetchJSON(name).catch(function () { return null; });
    })).then(function (results) {
      for (var i = 0; i < results.length; i++) {
        // A gap means a chunk is not written yet; stop and retry next poll
        // rather than accepting rows out of order.
        if (results[i] === null) break;
        results[i].forEach(function (r) { rows.push(r); });
        loaded = wanted[i];
      }
      fetching = false;
      render();
    });
  }

  /* --- filter + render ---------------------------------------------------- */

  function applyFilter() {
    var q = query.trim().toLowerCase();
    var source = rows;
    if (state === "building") source = builders.map(function (b) {
      return { origin: b.origin, info: b.version, result: "building",
               recorded_at: null, entry: null };
    });
    else if (state !== "all") {
      source = rows.filter(function (r) { return stateOf(r) === state; });
    }
    view = !q ? source.slice() : source.filter(function (r) {
      return (r.origin || "").toLowerCase().indexOf(q) >= 0 ||
             (r.info || "").toLowerCase().indexOf(q) >= 0;
    });
    if (page * perPage >= view.length) page = 0;
  }

  function originCell(origin) {
    var td = el("td");
    var parts = String(origin || "").split("/");
    if (parts.length === 2 && parts[0] && parts[1]) {
      td.appendChild(link(
        window.DP_RUN.portBase + encodeURIComponent(parts[0]) + "/" +
        encodeURIComponent(parts[1]), origin));
    } else {
      td.textContent = origin || "";
    }
    return td;
  }

  function rowNode(r) {
    var tr = el("tr");
    var st = stateOf(r);

    var no = el("td", "tabular");
    no.style.color = "var(--faint)";
    no.textContent = r.entry === null || r.entry === undefined ? "—" : r.entry;
    tr.appendChild(no);

    var stateTd = el("td");
    stateTd.appendChild(el("span", "pill " + (PILL[st] || "neutral"), st));
    tr.appendChild(stateTd);

    tr.appendChild(originCell(r.origin));
    tr.appendChild(el("td", "tabular", r.info || "—"));
    tr.appendChild(el("td", "tabular", r.recorded_at || "—"));

    var ev = el("td");
    if (r.bundle_id) {
      ev.appendChild(link(
        "/agentic/bundles/" + encodeURIComponent(r.bundle_id) +
        "/artifacts/logs/full.log.gz", "log"));
      ev.appendChild(document.createTextNode(" · "));
      ev.appendChild(link("/agentic/bundles/" + encodeURIComponent(r.bundle_id), "repair"));
    } else {
      ev.appendChild(el("span", null, "—"));
      ev.lastChild.style.color = "var(--faint)";
    }
    tr.appendChild(ev);
    return tr;
  }

  function render() {
    applyFilter();
    var body = $("results-body");
    if (!body) return;
    clear(body);
    var start = page * perPage;
    view.slice(start, start + perPage).forEach(function (r) {
      body.appendChild(rowNode(r));
    });

    var empty = $("results-empty");
    if (empty) empty.hidden = view.length > 0;
    var table = $("results-table-wrap");
    if (table) table.hidden = view.length === 0;

    setText("results-note", view.length
      ? (start + 1) + "–" + Math.min(start + perPage, view.length) + " of " + view.length
      : "no rows match");
    setText("chunk-note", loaded >= kfiles
      ? kfiles + " of " + kfiles + " chunks loaded"
      : loaded + " of " + kfiles + " chunks loaded");

    var prev = $("page-prev"), next = $("page-next");
    if (prev) prev.disabled = page === 0;
    if (next) next.disabled = start + perPage >= view.length;

    document.querySelectorAll("[data-state]").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-state") === state);
      b.setAttribute("aria-pressed", String(b.getAttribute("data-state") === state));
    });
  }

  /* --- wiring ------------------------------------------------------------- */

  function wire() {
    var search = $("results-search");
    if (search) search.addEventListener("input", function () {
      query = this.value; page = 0; render();
    });
    document.querySelectorAll("[data-state]").forEach(function (b) {
      b.addEventListener("click", function () {
        state = b.getAttribute("data-state"); page = 0; render();
      });
    });
    var per = $("per-page");
    if (per) per.addEventListener("change", function () {
      perPage = parseInt(this.value, 10) || 50; page = 0; render();
    });
    var prev = $("page-prev"), next = $("page-next");
    if (prev) prev.addEventListener("click", function () { if (page > 0) { page--; render(); } });
    if (next) next.addEventListener("click", function () {
      if ((page + 1) * perPage < view.length) { page++; render(); }
    });
  }

  function poll() {
    fetchJSON("summary.json").then(function (data) {
      applySummary(data);
      loadChunks();
      if (active) setTimeout(poll, POLL_MS);
    }).catch(function () {
      setTimeout(poll, POLL_MS / 2);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    wire();
    render();
    poll();
  });
})();
