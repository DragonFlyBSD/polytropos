"""Which provider failures are worth retrying, and is the model even there.

Both halves of poly-4av's foundation. The classification decides whether
a job dies or waits; the preflight is what makes the NVIDIA 404 entry in
``_PROVIDER_TRANSIENT_STATUS`` safe, by ruling out the one other thing
that produces an identical "404 page not found" — a name that is simply
wrong. openclaw#71552 hit exactly that body from a stripped prefix.
"""

from __future__ import annotations

import pytest

from dportsv3.agent import llm


class _Status(Exception):
    """An API error carrying an HTTP status, like the SDK's."""

    def __init__(self, status: int, message: str = ""):
        super().__init__(message or f"Error code: {status}")
        self.status_code = status


class _Named(Exception):
    """Timeouts and connection drops carry no status, only a type."""


# --- transient vs terminal ---------------------------------------------------

@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504, 524])
def test_the_usual_transient_statuses_retry(status):
    assert llm.is_transient(_Status(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_terminal_by_default(status):
    """Retrying our own bad request just spends the budget twice."""
    assert llm.is_transient(_Status(status)) is False


def test_nvidia_404_is_transient_but_only_for_nvidia():
    """NVIDIA answers an unroutable model with a bare 404. Measured: the
    same model 404s and succeeds seconds apart, stays listed in
    /v1/models throughout, and recovers on its own."""
    assert llm.is_transient(_Status(404), provider="nvidia") is True
    assert llm.is_transient(_Status(404), provider="deepseek") is False
    assert llm.is_transient(_Status(404)) is False


@pytest.mark.parametrize("text", [
    "ResourceExhausted: All workers are busy, please retry later",
    "Service temporarily overloaded",
])
def test_overload_text_retries_without_a_status(text):
    """pi#6364: NIM reports a full worker pool this way and the status
    does not always come with it."""
    assert llm.is_transient(_Named(text)) is True


def test_a_quota_error_is_never_transient():
    """Even wearing a 429. Retrying an exhausted quota cannot succeed."""
    assert llm.is_transient(_Status(429, "insufficient_quota: you exceeded")) is False


@pytest.mark.parametrize("name", ["APITimeoutError", "APIConnectionError"])
def test_timeouts_and_dropped_connections_retry(name):
    exc = type(name, (Exception,), {})("boom")
    assert llm.is_transient(exc) is True


def test_an_unrecognised_error_is_terminal():
    """Conservative on purpose: a wrong 'transient' turns one failure
    into several, so anything unfamiliar stops."""
    assert llm.is_transient(Exception("something new")) is False


# --- is the model actually offered -------------------------------------------

def _models(monkeypatch, ids):
    monkeypatch.setattr(llm, "list_models", lambda **kw: ids)


def test_a_listed_model_passes(monkeypatch):
    _models(monkeypatch, {"nvidia/nemotron-3-ultra-550b-a55b"})
    assert llm.validate_model("nvidia/nemotron-3-ultra-550b-a55b",
                              api_base=None, api_key="k",
                              provider="nvidia") is None


def test_a_typo_is_caught_with_a_suggestion(monkeypatch):
    _models(monkeypatch, {"nvidia/nemotron-3-ultra-550b-a55b",
                          "nvidia/nemotron-3-super-120b-a12b"})
    problem = llm.validate_model("nvidia/nemotron-3-ultra-550b-a55c",
                                 api_base=None, api_key="k",
                                 provider="nvidia")
    assert problem is not None
    assert "not offered" in problem
    assert "nemotron-3-ultra-550b-a55b" in problem


def test_an_unreachable_endpoint_is_not_a_verdict(monkeypatch):
    """None from list_models means 'could not ask'. Treating that as
    'model missing' would break every startup behind a blip."""
    _models(monkeypatch, None)
    assert llm.validate_model("anything", api_base=None, api_key="k",
                              provider="nvidia") is None


def test_a_routing_prefix_is_stripped_before_comparing(monkeypatch):
    """The endpoint lists what it serves; deepseek/ is our routing
    prefix, not part of the id it advertises."""
    _models(monkeypatch, {"deepseek-v4-pro"})
    assert llm.validate_model("deepseek/deepseek-v4-pro", api_base=None,
                              api_key="k", provider="deepseek") is None
