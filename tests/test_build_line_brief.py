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


def test_the_always_on_line_poses_the_question_and_names_both_answers(env):
    """On 5085 of 5087 ports this is ALL the agent gets.

    Naming the blind spot without naming the lever is not a reason to ask
    anything -- and a port needing its first per-target split is in that
    population.
    """
    brief = _brief(env, SINGLE)
    assert "decide which it is" in brief
    assert "`target @any`" in brief
    assert "`target @main` block" in brief


def test_a_single_scope_port_gets_only_the_one_line(env):
    """The long form is noise on 5085 of 5087 ports."""
    brief = _brief(env, SINGLE)
    assert "per-target blocks" not in brief
    assert "install_patches" not in brief


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
    assert "split it into per-target blocks" in brief


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
    # it legitimately names the target ("if an op is wrong for `@main`...").
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


def test_the_patch_step_actually_injects_it():
    """A helper nothing calls is not a fix."""
    import inspect

    src = inspect.getsource(steps.PatchAttemptStep)
    # Ordering, not presence: a presence grep passed with the call placed
    # anywhere, including after the harness had already run.
    call = src.index("payload += _build_line_brief(_worker, env, patch_origin)")
    assert call < src.index("harness_patch.run(")
    # Method-level, not nested inside the slave-port branch.
    line_start = src.rindex("\n", 0, call) + 1
    assert src[line_start:call] == " " * 8, repr(src[line_start:call])
