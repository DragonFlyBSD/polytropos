// Runner-status page: live-refresh the status cells in place via the shared
// dpLive poller. The initial values render server-side; this only updates
// them.
//
// The toggle stops THIS PAGE refreshing. It says so: it used to read
// [pause] and sat beside a Status cell that reads "paused" when the runner
// pauses itself, so the two were indistinguishable (poly-0e02.8).
(function () {
  var indicator = document.getElementById("live-indicator");
  var toggle = document.getElementById("live-toggle");
  var statusText = indicator ? indicator.querySelector(".status-text") : null;

  function setText(id, value) {
    var el = document.getElementById(id);
    if (el) el.textContent = value == null ? "—" : value;
  }
  // `status` survives the runner process dying -- one killed mid-job leaves
  // "processing" on the row forever -- so the heartbeat is what says whether
  // to believe it.
  function setLive(live) {
    var el = document.getElementById("runner-live");
    if (!el) return;
    if (live === false) {
      el.innerHTML = '<span class="pill neutral">not running</span>';
    } else {
      el.textContent = "";
    }
  }
  function setJob(jobId) {
    var el = document.getElementById("runner-job");
    if (!el) return;
    if (jobId) {
      el.innerHTML = '<a href="/agentic/jobs/' + encodeURIComponent(jobId)
        + '">' + jobId + "</a>";
    } else {
      el.textContent = "—";
    }
  }

  var poller = window.dpLive({
    intervalMs: 4000,
    url: function () { return "/api/runner-status"; },
    onData: function (d) {
      setText("runner-status", d.status);
      setLive(d.live);
      setJob(d.job_id);
      setText("runner-stage", d.current_stage);
      setText("runner-started", d.started_at);
      setText("runner-updated", d.updated_at);
      var extra = document.getElementById("runner-extra");
      if (extra && d.extra_json) extra.textContent = d.extra_json;
    },
  });

  if (toggle) {
    toggle.addEventListener("click", function () {
      if (poller.isPaused()) {
        poller.resume();
        if (indicator) indicator.classList.add("active");
        if (statusText) statusText.textContent = "live";
        toggle.textContent = "stop refreshing";
      } else {
        poller.pause();
        if (indicator) indicator.classList.remove("active");
        if (statusText) statusText.textContent = "paused";
        toggle.textContent = "resume refreshing";
      }
    });
  }

  poller.start(4000);
})();
