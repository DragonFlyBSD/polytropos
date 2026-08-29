"""An attempt composes its own starting tree (poly-15l).

Observed on hardware 2026-08-29: a build reported

    packages built: 3   failed: 0

for devel/glib20, which had failed all day. A phantom — the composed
tree it built carried three of the port's five patches, and the two
missing ones were exactly the ones that were failing. A fresh reapply
from the clean baseline produces all five, so the tree was wrong, not
the repo.

The cause is an ordering that spans two modules. ``worker.reset_port``
regenerates the compose tree after a patch job, and its docstring used
to claim it composed "from the now-reset substrate" — but B1 removed
that source reset on the grounds that each job now has its own
worktree, and that worktree is destroyed later, in ``runner.py``. So
the post-job reapply composes the agent's abandoned edits, succeeds,
reports ``reapply_ok=True``, and leaves the shared tree contradicting
the repository. The activity log showed plain success.

A red build gets investigated; a green one gets believed. That is what
makes this worth a preflight rather than a warning.

The fix inverts the dependency: each attempt composes the one origin
before it starts, so it cannot inherit a predecessor's discarded state,
and a compose failure refuses the job rather than proceeding against a
tree nobody can describe.
"""

from __future__ import annotations

import inspect

from dportsv3.agent import steps, worker


def _patch_preflight_source() -> str:
    """The patch step's body, where the preflight lives."""
    return inspect.getsource(steps)


# --- the invariant ----------------------------------------------------------

def test_the_origin_is_composed_before_the_agent_runs() -> None:
    """Composing after the fact makes correctness depend on an ordering
    across two modules, which is what already broke silently."""
    src = _patch_preflight_source()
    compose = src.index("_worker.materialize_dports(env, origin)")
    # `harness_patch.run(` also appears in this module's own docstring,
    # so anchor on the real call site after the compose.
    harness = src.index("result = harness_patch.run(")
    assert compose < harness, (
        "the compose must happen before the agent is handed the env"
    )


def test_the_clean_check_still_comes_first() -> None:
    """Composing a dirty tree would bake a prior run's leftovers into
    the compose output and call it a baseline."""
    src = _patch_preflight_source()
    clean = src.index("_worker.assert_port_clean(env, origin)")
    compose = src.index("_worker.materialize_dports(env, origin)")
    assert clean < compose


def test_a_failed_compose_refuses_the_job() -> None:
    """Best-effort is what let this through the first time. If the
    compose did not work we do not know what is on disk, and the entire
    point is to not work against an unknown tree."""
    src = _patch_preflight_source()
    block = src[src.index("_worker.materialize_dports(env, origin)"):]
    block = block[:block.index("dispatcher = PatchEventDispatcher")]
    assert "patch_preflight_compose_failed" in block
    assert "JobEvent.PATCH_GAVE_UP" in block


def test_the_refusal_explains_why_it_matters() -> None:
    """'compose failed' alone does not tell an operator that the tree is
    shared and that proceeding would use someone else's leftovers."""
    src = _patch_preflight_source()
    block = src[src.index("patch refused: could not compose"):][:600]
    assert "shared across jobs" in block


def test_the_success_path_is_recorded() -> None:
    """A silent precondition is one nobody can confirm ran — and the
    silence of the post-job reset is precisely how this hid."""
    assert "patch_preflight_composed" in _patch_preflight_source()


# --- it seeds the staleness guard -------------------------------------------

def test_composing_at_start_seeds_the_materialize_baseline() -> None:
    """dsynth_build refuses unless a materialize succeeded this attempt.
    Using materialize_dports here rather than a raw reapply means that
    baseline exists from the first turn, so the guard reduces to its
    real question — has the source changed since the compose — instead
    of forcing a ritual call (one measured attempt made four)."""
    src = inspect.getsource(worker.materialize_dports)
    assert "_MATERIALIZE_STATE[(env, origin)] = h" in src
    assert "materialize_dports" in _patch_preflight_source()


# --- the trap that caused it stays documented -------------------------------

def test_reset_port_no_longer_claims_it_composes_from_a_reset_tree() -> None:
    """The old docstring was the bug's alibi: it described a source
    reset that B1 had removed. Anyone reading it would reasonably
    believe the post-job tree matched the repo."""
    # Compare on collapsed whitespace: the docstring is wrapped, so the
    # phrases below span line breaks in the source.
    doc = " ".join((worker.reset_port.__doc__ or "").split())
    assert "from the now-reset substrate" not in doc, (
        "the claim that caused this must not come back"
    )
    assert "does NOT compose from a reset substrate" in doc
    assert "poly-15l" in doc


def test_the_post_reset_log_says_whether_the_reapply_worked() -> None:
    """It logged plain success while leaving a tree contradicting the
    repo. reapply_ok was already in the return value; it just never
    reached anyone."""
    src = _patch_preflight_source()
    assert "reapply_ok=" in src
