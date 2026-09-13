// The Repairs queue keeps your place.
//
// The rail is its own scroll container and every queue row is a plain link
// to ?occ=<bundle>, so selecting an occurrence is a full navigation and the
// queue would start from the top again -- once per item, down a list that
// caps at 500 issues (poly-x3pg.6).
//
// None of this is load-bearing. Without it every row is still a link, the
// group holding the selection still renders open because the server opened
// it, and the selection is still marked. This is only about where the queue
// is looking when the new page arrives.
(function () {
  const rail = document.querySelector('.repair-rail');
  if (!rail) { return; }

  const SCROLL_KEY = 'dportsv3.repairs.scroll';
  const OPEN_KEY = 'dportsv3.repairs.open';

  function read(key) {
    try { return window.sessionStorage.getItem(key); } catch (e) { return null; }
  }
  function write(key, value) {
    // Private windows throw on write rather than degrading, and losing the
    // scroll position is not worth an exception that stops the rest.
    try { window.sessionStorage.setItem(key, value); } catch (e) { /* no-op */ }
  }
  function openSet() {
    try { return new Set(JSON.parse(read(OPEN_KEY) || '[]')); }
    catch (e) { return new Set(); }
  }

  // --- groups the operator opened by hand ---------------------------------
  // The server opens the one holding the selection. These are the others,
  // and they are worth keeping: comparing two issues means having both
  // expanded, and a navigation between them used to close both.
  const opened = openSet();
  rail.querySelectorAll('.wl-group[data-issue]').forEach(function (group) {
    if (!opened.has(group.dataset.issue)) { return; }
    group.classList.add('open');
    // The chevron says whether the group is open, so restoring one without
    // it leaves the row claiming to be collapsed to anything not looking.
    const chev = group.querySelector('.wl-chev');
    if (chev) { chev.setAttribute('aria-expanded', 'true'); }
  });
  window.dpWlRemember = function (group) {
    const key = group.dataset.issue;
    if (!key) { return; }
    const set = openSet();
    if (group.classList.contains('open')) { set.add(key); } else { set.delete(key); }
    write(OPEN_KEY, JSON.stringify(Array.from(set)));
  };

  // --- where the queue was looking ----------------------------------------
  // Restored only when the page carries a selection: arriving at /agentic
  // from the nav is "show me the queue", and that starts at the top.
  const current = rail.querySelector('.wl-row.current, .wl-child.current');
  if (current) {
    const saved = parseInt(read(SCROLL_KEY) || '', 10);
    if (!isNaN(saved)) { rail.scrollTop = saved; }
    // A deep link has nothing saved, and a saved position from a queue that
    // has re-sorted since is wrong. Either way the row the right pane is
    // showing has to be on screen, so pull it in when it isn't.
    const row = current.getBoundingClientRect();
    const box = rail.getBoundingClientRect();
    if (row.top < box.top || row.bottom > box.bottom) {
      rail.scrollTop += (row.top - box.top) - (box.height - row.height) / 2;
    }
  }
  // pagehide rather than unload: it fires on the back/forward cache too, and
  // every operator action in the right pane ends in location.reload().
  window.addEventListener('pagehide', function () {
    write(SCROLL_KEY, String(rail.scrollTop));
  });
})();
