"""poly-lt1: two Accepts must not drive the delivery clone at once.

Every delivery takes the same ``provider.clone_dir`` through checkout
-> apply -> commit -> push -> restore. Accept is a sync route, so
uvicorn runs each request on its own threadpool thread; two clicks
seconds apart overlapped in the clone and the loser was refused by
``prepare_clean_branch``'s clean-and-on-base precondition. Observed
2026-09-02: one delivery committed at :48 and restored at :53, and a
second Accept landed at :50 with

    GitWrongBranch: clone is on branch 'agentic/...', expected
    base_branch 'master'

The refusal was the guard working. These tests pin the serialisation
that stops the two from meeting in the first place.
"""

from __future__ import annotations

import threading
import time

import pytest

from dportsv3.delivery import DeliveryError, ReviewRequestResult
from dportsv3.delivery import orchestrator as orch
from dportsv3.delivery.config import DeliveryConfig


def _cfg() -> DeliveryConfig:
    # local-patch so deliver() skips the clone_dir existence check; the
    # provider itself is replaced below, so the type only picks a path.
    return DeliveryConfig(
        provider_type="local-patch",
        repo=None,
        base_branch="master",
        draft=False,
        labels=(),
        branch_template="agentic/{origin_safe}-{target_safe}",
        token=None,
        clone_dir=None,
        outbox="/nonexistent",
    )


class _RecordingProvider:
    """Stands in for GitHubProvider. Records whether the clone lock was
    held while it ran, and how many callers were inside it at once."""

    def __init__(self, barrier: threading.Event | None = None):
        self.locked_during_call: list[bool] = []
        self.concurrent_peak = 0
        self._inside = 0
        self._guard = threading.Lock()
        self._barrier = barrier
        self.entered = threading.Event()

    def create_review_request(self, **kw) -> ReviewRequestResult:
        with self._guard:
            self._inside += 1
            self.concurrent_peak = max(self.concurrent_peak, self._inside)
            self.locked_during_call.append(orch._CLONE_LOCK.locked())
        self.entered.set()
        try:
            if self._barrier is not None:
                # Hold the clone until the test says otherwise. A real
                # delivery holds it for the length of a push.
                self._barrier.wait(timeout=5)
            return ReviewRequestResult(
                provider="local-patch", provider_pr_id="1",
                url=None, branch=kw.get("branch_name") or "b",
                title=kw.get("title") or "t", status="created",
            )
        finally:
            with self._guard:
                self._inside -= 1


@pytest.fixture
def git_timeout(monkeypatch):
    """Override only delivery.git_timeout. _clone_locked imports
    settings inside the function, so orch.settings is not the name it
    reads — patch the module attribute, and delegate every other key to
    the real table so nothing else in deliver() is disturbed."""
    from dportsv3 import settings as settings_mod
    real = settings_mod.get

    def _set(value: float):
        def fake(key, *a, **k):
            if key == "delivery.git_timeout":
                return value
            return real(key, *a, **k)
        monkeypatch.setattr(settings_mod, "get", fake)

    return _set


@pytest.fixture
def stub_queries(monkeypatch):
    """deliver() writes its row through these three; none of them are
    what is under test, so the DB stays out of it entirely."""
    import dportsv3.tracker.agentic_queries as q
    monkeypatch.setattr(q, "find_open_review_request",
                        lambda *a, **k: None)
    monkeypatch.setattr(q, "insert_review_request",
                        lambda *a, **k: 1)
    monkeypatch.setattr(q, "update_review_request_status",
                        lambda *a, **k: None)


@pytest.fixture(autouse=True)
def unlocked():
    """The lock is module state; a test that leaves it held would wedge
    every test after it. Assert clean on the way in and out."""
    assert not orch._CLONE_LOCK.locked()
    yield
    assert not orch._CLONE_LOCK.locked()


def _deliver(cfg, provider, monkeypatch, operator="op"):
    monkeypatch.setattr(orch, "build_provider", lambda c: provider)
    return orch.deliver(
        bundle={"bundle_id": "cat_port-20260902-000000Z",
                "origin": "cat/port", "target": "2026Q3"},
        diff_text="--- a\n+++ b\n",
        cfg=cfg, operator=operator, model=None,
        attempts=None, tokens=None, write_conn=None,
    )


# --- the lock itself -------------------------------------------------

def test_clone_lock_blocks_a_second_holder_and_names_the_wait(git_timeout):
    git_timeout(0.05)
    with orch._clone_locked():
        with pytest.raises(DeliveryError) as exc:
            with orch._clone_locked():
                pytest.fail("second holder must not get in")
    msg = str(exc.value)
    assert "another delivery is using the clone" in msg
    assert "retry" in msg


def test_clone_lock_releases_when_the_body_raises(git_timeout):
    git_timeout(0.05)
    with pytest.raises(RuntimeError):
        with orch._clone_locked():
            raise RuntimeError("provider blew up")
    # A delivery that fails must not wedge every delivery after it.
    assert orch._CLONE_LOCK.acquire(timeout=0)
    orch._CLONE_LOCK.release()


# --- the wiring ------------------------------------------------------

def test_deliver_holds_the_lock_across_the_provider_call(
    monkeypatch, stub_queries, git_timeout,
):
    """The whole provider call, not just its git prologue: the window
    that bit us was between the commit and the restore that runs in
    create_review_request's finally."""
    git_timeout(5.0)
    provider = _RecordingProvider()
    outcome = _deliver(_cfg(), provider, monkeypatch)
    assert outcome.status == "created"
    assert provider.locked_during_call == [True]


def test_two_concurrent_deliveries_never_overlap_in_the_provider(
    monkeypatch, stub_queries, git_timeout,
):
    release = threading.Event()
    provider = _RecordingProvider(barrier=release)
    monkeypatch.setattr(orch, "build_provider", lambda c: provider)
    git_timeout(5.0)
    cfg = _cfg()
    outcomes: list = []

    def run(name):
        outcomes.append(orch.deliver(
            bundle={"bundle_id": f"cat_{name}-20260902-000000Z",
                    "origin": f"cat/{name}", "target": "2026Q3"},
            diff_text="--- a\n+++ b\n",
            cfg=cfg, operator=name, model=None,
            attempts=None, tokens=None, write_conn=None,
        ))

    threads = [threading.Thread(target=run, args=(n,))
               for n in ("first", "second")]
    for t in threads:
        t.start()
    # Wait until one delivery is genuinely inside the provider, then
    # give the other time to arrive and block on the lock, and only
    # then let the first out. Without the lock the second is already
    # inside by now and concurrent_peak reads 2.
    assert provider.entered.wait(timeout=5), "no delivery reached provider"
    time.sleep(0.15)
    release.set()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert provider.concurrent_peak == 1, (
        "two deliveries were inside the provider at once"
    )
    assert [o.status for o in outcomes] == ["created", "created"]


def test_a_busy_clone_is_recorded_as_create_failed_not_an_exception(
    monkeypatch, stub_queries, git_timeout,
):
    """The timeout raises inside deliver()'s existing try, so the
    operator gets the same create_failed row as any provider failure —
    with a message that says to retry, rather than a 500."""
    git_timeout(0.05)
    provider = _RecordingProvider()
    with orch._clone_locked():           # someone else holds it
        outcome = _deliver(_cfg(), provider, monkeypatch)
    assert outcome.status == "create_failed"
    assert "another delivery is using the clone" in (outcome.error or "")
    assert provider.locked_during_call == [], "provider must not have run"


# --- the incident, reproduced against real git -----------------------


def _sh(args, cwd):
    import subprocess
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def real_clone(tmp_path):
    """A working clone with a pushable origin — the shared resource two
    Accepts contend for."""
    remote = tmp_path / "remote.git"
    _sh(["git", "init", "--bare", "-b", "master", str(remote)], tmp_path)
    clone = tmp_path / "clone"
    _sh(["git", "clone", str(remote), str(clone)], tmp_path)
    _sh(["git", "config", "user.email", "t@t"], clone)
    _sh(["git", "config", "user.name", "t"], clone)
    (clone / "README").write_text("baseline\n")
    _sh(["git", "add", "README"], clone)
    _sh(["git", "commit", "-qm", "baseline"], clone)
    _sh(["git", "push", "-u", "origin", "master"], clone)
    return clone


_REAL_DIFF = (
    "--- a/README\n"
    "+++ b/README\n"
    "@@ -1 +1,2 @@\n"
    " baseline\n"
    "+new line\n"
)


class _FakeHttpPerCall:
    """Answers the PR calls the provider makes. Every delivery sees no
    open PR and gets a fresh number."""

    def __init__(self):
        self._n = 0
        self._guard = threading.Lock()

    def get(self, path, *, params=None):
        return []

    def post(self, path, *, json=None):
        with self._guard:
            self._n += 1
            n = self._n
        if path.endswith("/pulls"):
            return {"number": n, "html_url": f"https://x/pull/{n}"}
        return []

    def patch(self, path, *, json=None):
        return {"number": 1, "html_url": "https://x/pull/1"}


def test_two_real_deliveries_do_not_wedge_the_clone(
    monkeypatch, stub_queries, git_timeout, real_clone,
):
    """The 2026-09-02 incident end to end: real git, one clone, two
    Accepts started together. Both must land, and the clone must be
    back on master and clean afterwards."""
    from dportsv3.delivery.github import GitHubProvider

    git_timeout(30.0)
    http = _FakeHttpPerCall()
    monkeypatch.setattr(
        orch, "build_provider",
        lambda c: GitHubProvider(
            token="t", repo="o/r",
            _http_client_factory=lambda headers: http,
        ),
    )
    cfg = DeliveryConfig(
        provider_type="github", repo="o/r", base_branch="master",
        draft=False, labels=(), branch_template="agentic/{origin_safe}",
        token="t", clone_dir=str(real_clone), outbox=None,
    )

    start = threading.Barrier(2)
    outcomes: list = []
    lock = threading.Lock()

    def run(name):
        start.wait(timeout=5)
        out = orch.deliver(
            bundle={"bundle_id": f"cat_{name}-20260902-000000Z",
                    "origin": f"cat/{name}", "target": "2026Q3"},
            diff_text=_REAL_DIFF, cfg=cfg, operator=name, model=None,
            attempts=None, tokens=None, write_conn=None,
        )
        with lock:
            outcomes.append(out)

    threads = [threading.Thread(target=run, args=(n,))
               for n in ("first", "second")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()

    errors = [o.error for o in outcomes if o.error]
    assert errors == [], errors
    assert sorted(o.status for o in outcomes) == ["created", "created"]

    import subprocess
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(real_clone), capture_output=True, text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(real_clone), capture_output=True, text=True,
    ).stdout.strip()
    assert head == "master", f"clone left on {head}"
    assert dirty == "", f"clone left dirty: {dirty}"
