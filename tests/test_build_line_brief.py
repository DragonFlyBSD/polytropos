"""The brief must name the build line it is asking about (poly-7pwa.5).

The job's compose target reached the model only as a directory component
in a sentence about where to read files. Nothing said "this job is for
@main", and nothing said that which build line it is can change what the
fix should look like -- so the model had no reason to ask.

It is not blind: get_effective_overlay returns each op's scoped `src`, and
get_file can read the raw overlay. It is UNINFORMED, which is why the fix
is a statement in the brief rather than a new view.

Volume costs attention, so the long form only appears for a port whose
overlay already carries per-target blocks -- 2 ports in the tree today.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dportsv3.agent import steps, worker

HEAD = 'port devel/thing\ntype port\nreason "fixture"\n'

SINGLE = HEAD + 'target @any\nmk set A "1"\n'

MULTI = HEAD + (
    "target @any\nmk set A \"1\"\n"
    "target @2026Q3\n"
    "file materialize dragonfly/@2026Q3/patch-x -> dragonfly/patch-x\n"
    "target @main\n"
    "file materialize dragonfly/@main/patch-x -> dragonfly/patch-x\n"
)

#: Per-target blocks but no scoped payload -- the mk-only shape.
MULTI_NO_PAYLOAD = HEAD + (
    'target @any\nmk set A "1"\n'
    'target @2026Q3\nmk set V "1.96.1"\n'
    'target @main\nmk set V "1.98.1"\n'
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    port = tmp_path / "DeltaPorts" / "ports" / "devel" / "thing"
    port.mkdir(parents=True)
    monkeypatch.setattr(
        worker, "env_paths",
        lambda e: SimpleNamespace(deltaports=tmp_path / "DeltaPorts"),
    )
    yield SimpleNamespace(port=port, name="brief-env")
    worker.set_env_target("brief-env", None)


def _brief(env, overlay_text, target="@main"):
    if overlay_text is not None:
        (env.port / "overlay.dops").write_text(overlay_text)
    worker.set_env_target("brief-env", target)
    return steps._build_line_brief(worker, "brief-env", "devel/thing")


def test_the_brief_names_the_target(env):
    brief = _brief(env, SINGLE)
    assert "`@main`" in brief
    assert "one build line" in brief


def test_it_says_nothing_here_can_check_the_others(env):
    """The fact that makes the target decision-relevant rather than trivia."""
    brief = _brief(env, SINGLE)
    assert "no other" in brief
    assert "whether a change also works on the rest" in brief


def test_a_port_with_an_overlay_keeps_the_change_on_its_line(env):
    """On almost every port this is ALL the agent gets (poly-7pwa.27).

    It used to say "@any unless you can show it is this line's alone",
    which the agent could never show: devel/glib20's @main patch went to
    @any and would have broken @2026Q3, whose older glib it did not fit.
    """
    brief = _brief(env, SINGLE)
    assert "`target @main` block" in brief
    assert "Do not edit or delete an `@any` op" in brief
    assert "override it in that block" in brief
    assert "Put a change in `target @any`" not in brief
    assert "unless you can show" not in brief


def test_a_port_without_an_overlay_gets_any(env):
    """Row 1: nothing was built with an overlay on any line."""
    brief = _brief(env, None)
    assert "no overlay ops yet" in brief
    assert "`target @any`" in brief
    assert "`target @main` block" not in brief


def test_a_bootstrap_header_is_still_row_1(env):
    """The preflight can commit triage's header before the attempt: a file
    with no ops is still a port nothing was built with."""
    brief = _brief(env, HEAD + "target @any\n")
    assert "no overlay ops yet" in brief
    assert "`target @main` block" not in brief


def test_the_always_on_brief_stays_small(env):
    """It rides on every turn of every patch attempt.

    About 620 characters with the longest build line name; the table it
    states replaced a shorter line that sent every fix to @any.
    """
    assert len(_brief(env, None)) <= 320  # before any overlay is written
    assert len(_brief(env, SINGLE, target="@2026Q3")) <= 640


def test_a_single_scope_port_gets_only_the_short_form(env):
    """The long form is noise on all but a handful of ports."""
    brief = _brief(env, SINGLE)
    assert "per-target blocks" not in brief
    assert "last `target` block" not in brief


def test_a_multi_target_port_is_told_where_an_appended_op_lands(env):
    brief = _brief(env, MULTI)
    assert "per-target blocks" in brief
    assert "last `target` block" in brief
    assert "get_effective_overlay" in brief


def test_a_multi_target_port_is_told_which_scopes_exist(env):
    brief = _brief(env, MULTI)
    assert "`@2026Q3`" in brief and "`@main`" in brief


def test_it_forbids_deleting_the_other_build_lines_ops(env):
    """The repair that makes THIS build pass and breaks an untestable one."""
    brief = _brief(env, MULTI)
    assert "Do not delete them" in brief
    assert "no env here can verify a replacement" in brief
    # And it says what to do instead: a prohibition alone gets improvised on.
    # Splitting an @any op into per-target blocks drops it from later lines.
    assert "split it into per-target blocks" not in brief
    # Row 4: another line's op that is exactly what this line needs is
    # shared by widening its target list in place, not copied.
    assert "share it rather than copy it" in brief
    assert "`target <its line>,@main`" in brief


MAKEFILE = "PORTNAME=\tthing\n\n.include <bsd.port.mk>\n"


def _apply(tmp_path, body, target):
    from dportsv3.engine.api import apply_dsl

    root = tmp_path / target.lstrip("@")
    root.mkdir()
    (root / "Makefile").write_text(MAKEFILE)
    result = apply_dsl(HEAD + body, source_path=None, port_root=root,
                       target=target, oracle_profile="off")
    return result, (root / "Makefile").read_text()


def test_a_target_block_op_overrides_an_any_op_that_applies(tmp_path):
    """The first half of the remedy: the @any op stays for every other line.

    @2099Q1 stands for a line branched later, which no block names.
    """
    body = (
        'target @any\nmk set FOO "shared"\n'
        'target @main\nmk set FOO "main"\n'
    )
    for target, want in (("@main", "FOO= main"), ("@2026Q3", "FOO= shared"),
                         ("@2099Q1", "FOO= shared")):
        result, text = _apply(tmp_path, body, target)
        assert result.ok, (target, result.diagnostics)
        foo = [ln for ln in text.splitlines() if ln.startswith("FOO")]
        assert foo == [want], (target, foo)


def test_a_target_block_op_cannot_rescue_an_any_op_that_fails(tmp_path):
    """The second half: a failed @any op fails the port whatever runs after."""
    body = (
        'target @any\n'
        'text replace-once file Makefile from "NOT THERE" to "x"\n'
        'target @main\nmk set FOO "main"\n'
    )
    result, _text = _apply(tmp_path, body, "@main")
    assert result.ok is False
    assert [row.status for row in result.op_results] == ["failed", "applied"]


def test_the_do_not_apply_bullet_excludes_the_target_being_built(env):
    """The old version could tell the model its OWN line's ops did not apply.

    Honest about what this pins: after the rewrite the target's ops go into
    the effective set rather than into `elsewhere`, so the exclusion is
    structural and this test DOCUMENTS the invariant rather than guarding a
    mutation -- I checked, and removing the redundant `scope != target`
    guard changes nothing. The live risk is a scope that appears only in a
    comma list with the target; that one is guarded below.
    """
    brief = _brief(env, MULTI, target="@main")
    # Just the LIST of scopes, not the whole bullet: the remedy clause after
    # it legitimately names the target ("If an `@any` op applies here but
    # is wrong for `@main`...").
    listed = brief[
        brief.index("- Ops under ") + len("- Ops under "):
        brief.index(" do **not** apply here")
    ]
    assert "@2026Q3" in listed
    assert "@main" not in listed, listed


def test_a_comma_list_block_is_not_two_build_lines(env):
    """`target @2026Q3,@main` expands to one op per target.

    Naive scope collection reported both and then told the model that ops
    which DO apply here do not. lang/rust:88 is exactly that line -- and
    this is the same false positive poly-7pwa.1's review rejected in code
    before it reappeared here as prose.
    """
    comma = HEAD + 'target @2026Q3,@main\nmk add MAKE_ENV "LDVER=ld.bfd"\n'
    brief = _brief(env, comma, target="@main")
    assert "do **not** apply here" not in brief, brief


def test_a_port_with_flat_payload_gets_no_lane_paragraph(env):
    """pkg's real shape: per-target blocks, but this patch is flat @any.

    The previous negative fixture was mk-only, so it could not tell a
    lane gate keyed on `dragonfly/@` from one keyed on `dragonfly/`.
    """
    flat_alongside = HEAD + (
        "target @any\n"
        "file materialize dragonfly/patch-x -> dragonfly/patch-x\n"
        'target @main\nmk set V "1"\n'
    )
    brief = _brief(env, flat_alongside)
    assert "per-target blocks" in brief
    assert "patch sources are scoped" not in brief


def test_an_any_payload_dir_is_not_a_build_line(env):
    """`dragonfly/@any/` is an established path-preserving shape (.9)."""
    any_lane = HEAD + (
        "target @any\n"
        "file materialize dragonfly/@any/pkg-descr -> pkg-descr\n"
        'target @main\nmk set V "1"\n'
    )
    brief = _brief(env, any_lane)
    assert "patch sources are scoped" not in brief


def test_the_scoped_payload_paragraph_only_appears_when_there_is_one(env):
    """A port can have per-target mk blocks and no scoped patches."""
    with_payload = _brief(env, MULTI)
    assert "dragonfly/@<target>/" in with_payload
    (env.port / "overlay.dops").write_text(MULTI_NO_PAYLOAD)
    without = steps._build_line_brief(worker, "brief-env", "devel/thing")
    assert "per-target blocks" in without
    assert "dragonfly/@<target>/" not in without


def test_no_target_means_no_section(env):
    """Better silent than asserting a build line we do not know."""
    worker.set_env_target("brief-env", None)
    (env.port / "overlay.dops").write_text(MULTI)
    assert steps._build_line_brief(worker, "brief-env", "devel/thing") == ""


def test_a_missing_overlay_still_names_the_target(env):
    """A port being converted from nothing is still on one build line."""
    brief = _brief(env, None)
    assert "`@main`" in brief
    assert "per-target blocks" not in brief


def test_an_unparseable_overlay_degrades_to_the_one_liner(env):
    brief = _brief(env, HEAD + "this is not dops\n")
    assert "`@main`" in brief
    assert "per-target blocks" not in brief


def test_it_never_raises(monkeypatch):
    """It decorates a prompt; a failure must not cost an attempt."""
    broken = SimpleNamespace(
        peek_env_target=lambda e: (_ for _ in ()).throw(RuntimeError("nope")),
    )
    assert steps._build_line_brief(broken, "e", "devel/thing") == ""


class _HarnessReached(BaseException):
    """Stops the step at the harness call.

    BaseException, so run()'s "except Exception" salvage path lets it out.
    """


def test_the_patch_payload_carries_the_build_line_brief(
    env, tmp_path, monkeypatch, set_setting,
):
    """A helper nothing calls is not a fix: drive the step to the harness."""
    from dportsv3.agent import patch as harness_patch
    from dportsv3.agent import runner
    from dportsv3.agent.policy import Tier
    from dportsv3.agent.step import StepCtx

    (env.port / "overlay.dops").write_text(SINGLE)
    monkeypatch.setattr(worker, "patch_origin_for", lambda e, o: o)
    monkeypatch.setattr(worker, "assert_port_clean",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(worker, "ensure_bootstrap_overlay",
                        lambda *a, **k: {})
    monkeypatch.setattr(worker, "materialize_dports",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(runner, "take_operator_notes", lambda job_id: [])
    captured: dict = {}

    def _stub(payload, **kw):
        captured["payload"] = payload
        raise _HarnessReached

    monkeypatch.setattr(harness_patch, "run", _stub)
    set_setting("llm.patch.model", "test/stub")
    worker.set_env_target("brief-env", "@main")

    def _noop(*a, **k):
        return None

    services = steps.PatchServices(
        read_bundle_text=_noop, write_error_note=_noop,
        write_patch_audit=_noop, write_tool_trace=_noop,
        write_changes_diff=_noop,
        looks_env_suspicious=lambda *a, **k: False,
        invalidate_health_cache=_noop,
        cached_health_broken=lambda *a, **k: False,
        summarize_tool_call=lambda *a, **k: "",
        activity_log=_noop, log=_noop, load_port_history=_noop,
    )
    ctx = StepCtx(
        job_id="j1", job={"origin": "devel/thing", "target": "@main"},
        queue_root=tmp_path,
    )
    ctx.state.update(
        services=services, job_path=tmp_path / "j1.job",
        origin="devel/thing", payload="BASE PAYLOAD", model="test/stub",
        env="brief-env", tier=Tier(name="AUTO", max_tokens=1000),
    )
    with pytest.raises(_HarnessReached):
        steps.PatchAttemptStep().run(ctx)
    payload = captured["payload"]
    assert payload.startswith("BASE PAYLOAD")
    assert "## You are building one build line" in payload
    assert "`@main`" in payload
