"""The Verify env picker offers only envs the runner would run it in (poly-ti0f).

The runner refuses a verify whose env composes another build line than the
bundle's target (poly-7pwa.14). Before this, the picker listed every env and
fell back to the first one when no deployment default was set -- 2026Q3 for
a @main bundle on the builder, so two clicks queued two refused requests.
"""

from __future__ import annotations

from dportsv3.tracker.fix_state import verify_env_choices


def _row(env, target=None, runner="r1"):
    detail = {"checks": []}
    if target is not None:
        detail["target"] = target
    return {"runner_id": runner, "env": env, "detail": detail}


ROWS = [_row("2026Q3", "@2026Q3"), _row("main", "@main")]


def test_an_env_of_another_line_is_not_offered():
    choices = verify_env_choices(ROWS, "@main", None)
    assert choices.offered == ("main",)
    assert choices.default == "main"
    assert choices.excluded == ("2026Q3",)


def test_the_samurai_case_no_default_env_and_the_other_line_listed_first():
    # The exact shape on the builder: no deployment default, 2026Q3 first.
    choices = verify_env_choices(ROWS, "@main", None)
    assert choices.default != "2026Q3"


def test_the_active_env_is_preselected_when_it_matches():
    rows = ROWS + [_row("main-b", "@main")]
    assert verify_env_choices(rows, "@main", "main-b").default == "main-b"


def test_an_active_env_of_another_line_is_not_preselected():
    choices = verify_env_choices(ROWS, "@2026Q3", "main")
    assert choices.offered == ("2026Q3",)
    assert choices.default == "2026Q3"


def test_an_env_with_no_recorded_line_stays_but_a_known_match_wins():
    # Only a known mismatch is a mismatch: rows written before the runner
    # recorded targets must not empty the picker.
    rows = [_row("old"), _row("main", "@main")]
    choices = verify_env_choices(rows, "main", "old")
    assert choices.offered == ("old", "main")
    assert choices.default == "main"


def test_with_no_line_known_anywhere_the_old_behaviour_holds():
    rows = [_row("a"), _row("b")]
    assert verify_env_choices(rows, "@main", "b").default == "b"
    assert verify_env_choices(rows, "@main", None).default == "a"


def test_a_bundle_without_a_target_offers_every_env():
    choices = verify_env_choices(ROWS, None, "main")
    assert choices.offered == ("2026Q3", "main")
    assert choices.default == "main"
    assert choices.excluded == ()


def test_nothing_composes_the_line():
    choices = verify_env_choices([_row("2026Q3", "@2026Q3")], "@main", None)
    assert choices.offered == ()
    assert choices.default is None
    assert choices.excluded == ("2026Q3",)


def test_one_env_on_two_builders_is_listed_once():
    rows = [_row("main", "@main", "r1"), _row("main", None, "r2")]
    assert verify_env_choices(rows, "@main", None).offered == ("main",)
