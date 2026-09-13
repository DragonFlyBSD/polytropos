// The operator guide: six chapters and a glossary over a blurred page.
//
// The tracker taught nothing about its own model -- issue, occurrence and
// job are three different things with three different state machines, and
// all of it was written down only in Python docstrings (poly-9u7).
//
// Two ways in: the ? in the Repairs subnav, any time; and one auto-open on
// a first visit. Bump KEY when the model changes and every operator gets
// the new guide once.
(function () {
  var KEY = 'dportsv3.tour.v1';
  var scrim = document.getElementById('tour');
  var dialog = document.getElementById('t-dialog');
  var pane = document.getElementById('t-pane');
  var rail = document.getElementById('t-rail');
  var dots = document.getElementById('t-dots');
  var count = document.getElementById('t-count');
  var progress = document.getElementById('t-progress');
  var checkWrap = document.querySelector('.t-check');
  var dontShow = document.getElementById('t-dontshow');
  var backBtn = document.getElementById('t-back');
  var nextBtn = document.getElementById('t-next');
  var panels = [].slice.call(pane.querySelectorAll('.t-panel'));
  var chapters = panels.filter(function (p) { return !p.dataset.ref; });
  var mode = 'tour';
  var at = 0;
  var visited = {};

  /* localStorage is best-effort: a sandboxed frame may refuse it. */
  function seen() { try { return localStorage.getItem(KEY) === 'done'; } catch (e) { return false; } }
  function markSeen(v) { try { v ? localStorage.setItem(KEY, 'done') : localStorage.removeItem(KEY); } catch (e) {} }

  /* rail */
  panels.forEach(function (panel, i) {
    if (panel.dataset.ref && !rail.querySelector('.t-rail-sep')) {
      var sep = document.createElement('li');
      sep.className = 't-rail-sep';
      sep.textContent = 'Reference';
      rail.appendChild(sep);
    }
    var li = document.createElement('li');
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 't-rail-btn';
    b.innerHTML = '<span class="num">' + (panel.dataset.ref ? '·' : String(i + 1).padStart(2, '0')) +
                  '</span><span>' + panel.dataset.title + '</span>';
    b.addEventListener('click', function () { go(i); });
    li.appendChild(b);
    rail.appendChild(li);
    var d = document.createElement('i');
    if (!panel.dataset.ref) dots.appendChild(d);
  });
  var railBtns = [].slice.call(rail.querySelectorAll('.t-rail-btn'));

  function go(i) {
    at = Math.max(0, Math.min(panels.length - 1, i));
    visited[at] = true;
    panels.forEach(function (p, n) { p.hidden = n !== at; });
    railBtns.forEach(function (b, n) {
      b.setAttribute('aria-current', n === at ? 'true' : 'false');
      b.classList.toggle('done', visited[n] && n !== at);
    });
    [].slice.call(dots.children).forEach(function (d, n) {
      d.className = n === at ? 'on' : (visited[n] ? 'seen' : '');
    });
    if (at < chapters.length) count.textContent = (at + 1) + ' of ' + chapters.length;
    else count.textContent = 'reference';
    backBtn.disabled = at === 0;
    nextBtn.textContent = mode === 'help' ? 'Close'
      : (at === chapters.length - 1 ? 'Open the worklist' : 'Next');
    pane.scrollTop = 0;
  }

  function open(kind) {
    mode = kind;
    document.body.classList.add('tour-open');
    scrim.hidden = false;
    var help = kind === 'help';
    progress.style.display = help ? 'none' : '';
    checkWrap.style.display = help ? 'none' : '';
    backBtn.style.display = help ? 'none' : '';
    nextBtn.dataset.close = help ? '1' : '';
    if (!help) visited = {};
    go(help ? at : 0);
    dontShow.checked = seen();
    dialog.focus();
  }

  function close() {
    scrim.hidden = true;
    document.body.classList.remove('tour-open');
  }

  nextBtn.addEventListener('click', function () {
    if (nextBtn.dataset.close === '1' || at >= chapters.length - 1) { close(); return; }
    go(at + 1);
  });
  backBtn.addEventListener('click', function () { go(at - 1); });
  document.getElementById('t-close').addEventListener('click', close);
  scrim.addEventListener('mousedown', function (e) { if (e.target === scrim) close(); });
  dontShow.addEventListener('change', function () { markSeen(dontShow.checked); });

  document.addEventListener('keydown', function (e) {
    if (scrim.hidden) return;
    if (e.key === 'Escape') { close(); return; }
    if (e.key === 'ArrowRight' && at < panels.length - 1) { go(at + 1); return; }
    if (e.key === 'ArrowLeft' && at > 0) { go(at - 1); return; }
    if (e.key !== 'Tab') return;
    var f = [].slice.call(dialog.querySelectorAll('button, input, [href], [tabindex]:not([tabindex="-1"])'))
             .filter(function (el) { return el.offsetParent !== null && !el.disabled; });
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
      last.focus(); e.preventDefault();
    } else if (!e.shiftKey && document.activeElement === last) {
      first.focus(); e.preventDefault();
    }
  });
  dialog.tabIndex = -1;
  var help = document.getElementById('open-help');
  if (help) {
    help.addEventListener('click', function () { open('help'); });
  }

  // The first visit only, and after a beat so the page behind has painted
  // -- the blur is the point, and blurring a blank page is not.
  if (!seen()) { setTimeout(function () { open('tour'); }, 420); }
})();
