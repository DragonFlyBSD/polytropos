"""In-call staged retry around llm.complete (poly-4av).

The failure this covers, measured on one atril run: 126 calls succeeded
while 41 failed in the same window, neighbours seconds apart. A single
429 at turn 14 discarded thirteen good turns and burned one of the job's
five lives. Retrying the request in place costs one call instead.
"""

from __future__ import annotations

import pytest

from dportsv3.agent import llm


# --- schedule parsing --------------------------------------------------------


def test_schedule_parses_to_seconds():
    assert llm._parse_schedule("3,3,3,5,5,15") == [3, 3, 3, 5, 5, 15]


def test_schedule_tolerates_whitespace_and_blanks():
    assert llm._parse_schedule(" 3 , ,5,  15 ") == [3, 5, 15]


def test_schedule_drops_garbage_rather_than_raising():
    # A typo in the config must not take the agent down mid-run.
    assert llm._parse_schedule("3,abc,5") == [3, 5]


def test_schedule_drops_negatives_so_a_typo_cannot_make_a_hot_loop():
    assert llm._parse_schedule("3,-4,5") == [3, 5]


def test_schedule_keeps_an_explicit_zero():
    assert llm._parse_schedule("0,5") == [0, 5]


def test_empty_schedule_falls_back_to_the_default():
    assert llm._parse_schedule("") == [3, 3, 3, 5, 5, 15]
    assert llm._parse_schedule("nonsense") == [3, 3, 3, 5, 5, 15]


# --- the ladder --------------------------------------------------------------


def test_waits_follow_the_schedule():
    assert llm._retry_waits(6, [3, 3, 3, 5, 5, 15]) == [3, 3, 3, 5, 5, 15]


def test_last_entry_repeats_and_is_the_ceiling():
    assert llm._retry_waits(9, [3, 5, 15]) == [3, 5, 15, 15, 15, 15, 15, 15, 15]


def test_schedule_longer_than_max_is_truncated():
    assert llm._retry_waits(2, [3, 3, 3, 5, 5, 15]) == [3, 3]


def test_zero_max_disables_retry():
    assert llm._retry_waits(0, [3, 5]) == []


def test_empty_schedule_disables_retry():
    assert llm._retry_waits(6, []) == []


# --- Retry-After -------------------------------------------------------------


class _Resp:
    def __init__(self, headers):
        self.headers = headers


class _Err(Exception):
    def __init__(self, msg="429 Too Many Requests", headers=None, status=429):
        super().__init__(msg)
        self.status_code = status
        if headers is not None:
            self.response = _Resp(headers)


def test_retry_after_seconds_read_from_header():
    assert llm._retry_after_seconds(_Err(headers={"retry-after": "7"})) == 7.0


def test_retry_after_ms_header_converted():
    assert llm._retry_after_seconds(
        _Err(headers={"retry-after-ms": "2500"})) == 2.5


def test_retry_after_absent_is_none():
    assert llm._retry_after_seconds(_Err(headers={})) is None
    assert llm._retry_after_seconds(_Err()) is None


def test_retry_after_http_date_is_ignored_not_fatal():
    # We do not parse HTTP-dates; the schedule is a fine answer.
    assert llm._retry_after_seconds(
        _Err(headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})) is None


# --- complete() retries ------------------------------------------------------


@pytest.fixture
def slept(monkeypatch):
    """Capture the waits instead of performing them."""
    waits: list[float] = []
    monkeypatch.setattr(llm, "_SLEEP", waits.append)
    return waits


@pytest.fixture
def schedule(monkeypatch):
    monkeypatch.setattr(llm, "_retry_settings", lambda: (6, [3, 3, 3, 5, 5, 15]))


def _patch_dispatch(monkeypatch, side_effects):
    """Make complete() dispatch to a stub that walks ``side_effects``."""
    calls = {"n": 0}

    def _fake(*_args, **_kwargs):
        item = side_effects[calls["n"]]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(llm, "_use_openai_sdk", lambda *a, **k: True)
    monkeypatch.setattr(llm, "_complete_openai", _fake)
    return calls


def test_transient_failure_is_retried_and_succeeds(monkeypatch, slept, schedule):
    ok = llm.Response(text="fixed")
    calls = _patch_dispatch(monkeypatch, [_Err(), ok])

    got = llm.complete([{"role": "user", "content": "hi"}], model="m")

    assert got is ok
    assert calls["n"] == 2
    assert slept == [3]


def test_retries_walk_the_staged_schedule(monkeypatch, slept, schedule):
    ok = llm.Response(text="fixed")
    _patch_dispatch(monkeypatch, [_Err()] * 5 + [ok])

    llm.complete([{"role": "user", "content": "hi"}], model="m")

    # 3,3,3 while a flap is likely, then stepping up.
    assert slept == [3, 3, 3, 5, 5]


def test_gives_up_after_max_and_raises_the_last_error(monkeypatch, slept,
                                                      schedule):
    last = _Err("429 final")
    _patch_dispatch(monkeypatch, [_Err()] * 6 + [last])

    with pytest.raises(Exception) as excinfo:
        llm.complete([{"role": "user", "content": "hi"}], model="m")

    assert excinfo.value is last
    assert slept == [3, 3, 3, 5, 5, 15]


def test_terminal_error_is_not_retried(monkeypatch, slept, schedule):
    fatal = _Err("invalid_api_key", status=401)
    calls = _patch_dispatch(monkeypatch, [fatal])

    with pytest.raises(Exception):
        llm.complete([{"role": "user", "content": "hi"}], model="m")

    assert calls["n"] == 1
    assert slept == []


def test_success_resets_the_ladder_for_the_next_call(monkeypatch, slept,
                                                     schedule):
    """The counter is per call: a later request must not inherit a delay
    earned by an earlier one."""
    ok = llm.Response(text="ok")
    _patch_dispatch(monkeypatch,
                    [_Err(), _Err(), ok,      # first call: two retries
                     _Err(), ok])             # second call: starts at 3s again

    llm.complete([{"role": "user", "content": "a"}], model="m")
    llm.complete([{"role": "user", "content": "b"}], model="m")

    assert slept == [3, 3, 3]


def test_retry_after_is_honoured_when_longer_than_the_schedule(
        monkeypatch, slept, schedule):
    ok = llm.Response(text="ok")
    _patch_dispatch(monkeypatch, [_Err(headers={"retry-after": "9"}), ok])

    llm.complete([{"role": "user", "content": "hi"}], model="m")

    assert slept == [9]


def test_retry_after_is_clamped_to_the_ceiling(monkeypatch, slept, schedule):
    """opencode #13591: an unclamped Retry-After became a two-week sleep."""
    ok = llm.Response(text="ok")
    _patch_dispatch(monkeypatch,
                    [_Err(headers={"retry-after": "1209600"}), ok])

    llm.complete([{"role": "user", "content": "hi"}], model="m")

    assert slept == [15]


def test_retry_disabled_raises_on_first_failure(monkeypatch, slept):
    monkeypatch.setattr(llm, "_retry_settings", lambda: (0, [3]))
    calls = _patch_dispatch(monkeypatch, [_Err()])

    with pytest.raises(Exception):
        llm.complete([{"role": "user", "content": "hi"}], model="m")

    assert calls["n"] == 1
    assert slept == []


# --- the two ladders must not multiply ---------------------------------------


def test_sdk_max_retries_defaults_to_zero():
    """complete() owns the ladder. opencode left the SDK's default of 2
    in place and every retry became three requests (issue #30510)."""
    assert llm._sdk_max_retries() == 0


def test_settings_declare_the_retry_knobs():
    from dportsv3 import settings

    assert settings.get("llm.retry_max") == 6
    assert settings.get("llm.retry_backoff_schedule") == "3,3,3,5,5,15"
    assert settings.get("llm.sdk_max_retries") == 0


# --- wall-clock bound (opencode #25041: retries that hang the caller) --------


def test_a_slow_failure_is_not_retried_past_the_total_bound(monkeypatch, slept,
                                                            schedule):
    """A request timeout is transient, so without a wall-clock bound six
    retries at a 300s timeout would sit inside one call for 35 minutes.
    The per-retry ceiling cannot express that; only elapsed time can."""
    clock = {"t": 0.0}
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(llm, "_retry_total_seconds", lambda: 180.0)

    def _slow_fail(*_a, **_k):
        clock["t"] += 300.0          # one request timeout
        raise _Err("Request timed out", status=None)

    monkeypatch.setattr(llm, "_use_openai_sdk", lambda *a, **k: True)
    monkeypatch.setattr(llm, "_complete_openai", _slow_fail)

    with pytest.raises(Exception):
        llm.complete([{"role": "user", "content": "hi"}], model="m")

    assert slept == []               # tried once, never laddered


def test_fast_failures_still_get_the_whole_ladder(monkeypatch, slept, schedule):
    """The bound must not clip the case it was not written for: quick
    429s should still walk the full schedule."""
    clock = {"t": 0.0}
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(llm, "_retry_total_seconds", lambda: 180.0)
    monkeypatch.setattr(llm, "_SLEEP", lambda s: (slept.append(s),
                                                  clock.__setitem__("t", clock["t"] + s)))

    def _fast_fail(*_a, **_k):
        clock["t"] += 2.0
        raise _Err()

    monkeypatch.setattr(llm, "_use_openai_sdk", lambda *a, **k: True)
    monkeypatch.setattr(llm, "_complete_openai", _fast_fail)

    with pytest.raises(Exception):
        llm.complete([{"role": "user", "content": "hi"}], model="m")

    assert slept == [3, 3, 3, 5, 5, 15]


def test_total_bound_is_declared_as_a_setting():
    from dportsv3 import settings

    assert settings.get("llm.retry_total_seconds") == 180.0


# --- the 404 rule is provider-scoped, and the loop must respect that ---------


def test_nvidia_404_is_retried_in_place(monkeypatch, slept, schedule):
    """A bare 404 from NIM means "not routable right now", and recovers.
    Scoped to that provider — see poly-4av."""
    ok = llm.Response(text="ok")
    _patch_dispatch(monkeypatch, [_Err("404 page not found", status=404), ok])

    llm.complete([{"role": "user", "content": "hi"}],
                 model="nvidia/nemotron-3-ultra-550b-a55b")

    assert slept == [3]


def test_404_elsewhere_is_still_terminal(monkeypatch, slept, schedule):
    """Retrying a 404 everywhere would turn a typo'd model into a
    guaranteed wait on every provider that answers correctly."""
    calls = _patch_dispatch(
        monkeypatch, [_Err("404 not found", status=404)])

    with pytest.raises(Exception):
        llm.complete([{"role": "user", "content": "hi"}],
                     model="deepseek/deepseek-v4-pro")

    assert calls["n"] == 1
    assert slept == []
