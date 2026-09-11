"""poly-lt5q: a slave port is patched at its master, not refused.

The framework already says where a slave's fix belongs — bsd.port.mk
derives MASTER_PORT from MASTERDIR, and DFLY_PATCHDIR (being
``${PATCHDIR:H}/dragonfly`` over a PATCHDIR that defaults to
``${MASTERDIR}/files``) resolves into the master's directory. These
cover the resolution, the invariants that have to widen with it, and
the guards that stop a patch landing where nothing reads it.
"""
from __future__ import annotations

import subprocess

import pytest

from dportsv3.agent import decision as decision_mod
from dportsv3.agent import worker
from dportsv3.agent.policy import Policy, Tier


# --------------------------------------------------------------------
# relation resolution
# --------------------------------------------------------------------

COMPOSE = "/work/artifacts/compose/@2026Q3"


def _relation(origin, slave, master, patchdir, dfly):
    return worker._relation_from_vars(origin, slave, master, patchdir, dfly)


def test_a_slave_whose_dfly_patchdir_points_at_the_master_patches_there():
    # The live mysql84-client values, verified against @2026Q3.
    rel = _relation(
        "databases/mysql84-client",
        "yes", "databases/mysql84-server",
        f"{COMPOSE}/databases/mysql84-client/../mysql84-server/files",
        f"{COMPOSE}/databases/mysql84-client/../mysql84-server/dragonfly",
    )
    assert rel["slave_port"] is True
    assert rel["own_patchdir"] is False
    assert rel["patch_origin"] == "databases/mysql84-server"


def test_a_slave_whose_master_uses_the_curdir_idiom_keeps_its_own_dir():
    # lang/php82 sets PATCHDIR= ${.CURDIR}/files, so every extension
    # slave resolves into its own directory and patches belong there.
    rel = _relation(
        "net/php82-sockets",
        "yes", "lang/php82",
        f"{COMPOSE}/net/php82-sockets/files",
        f"{COMPOSE}/net/php82-sockets/dragonfly",
    )
    assert rel["slave_port"] is True
    assert rel["own_patchdir"] is True
    assert rel["patch_origin"] == "net/php82-sockets"


def test_krb5_resolves_to_the_master_that_actually_fixed_it():
    # Live @2026Q3 values. security/krb5-122 is where the hand fix went
    # and it turned three ports green — this is the case the bead is
    # named after.
    rel = _relation(
        "security/krb5", "yes", "security/krb5-122",
        f"{COMPOSE}/security/krb5/../krb5-122/files",
        f"{COMPOSE}/security/krb5/../krb5-122/dragonfly",
    )
    assert rel["patch_origin"] == "security/krb5-122"


def test_smake_resolves_to_its_master():
    # Live @2026Q3 values. The one DeltaPorts overlay confirmed to ship
    # a dragonfly/ payload the build cannot read.
    rel = _relation(
        "devel/smake", "yes", "devel/schilybase",
        f"{COMPOSE}/devel/smake/../../devel/schilybase/files",
        f"{COMPOSE}/devel/smake/../../devel/schilybase/dragonfly",
    )
    assert rel["own_patchdir"] is False
    assert rel["patch_origin"] == "devel/schilybase"


def test_a_non_slave_is_its_own_patch_origin():
    rel = _relation(
        "devel/gperf", "no", "",
        f"{COMPOSE}/devel/gperf/files",
        f"{COMPOSE}/devel/gperf/dragonfly",
    )
    assert rel["slave_port"] is False
    assert rel["patch_origin"] == "devel/gperf"


def test_a_slave_with_an_unresolvable_dfly_patchdir_falls_back_to_the_master():
    rel = _relation("x/slave", "yes", "x/master", "", "")
    assert rel["patch_origin"] == "x/master"


def test_a_probe_failure_degrades_to_the_ports_own_origin(monkeypatch):
    # Fail-open: an unresolvable probe must behave exactly as every
    # non-slave already does, never route a patch somewhere unverified.
    monkeypatch.setattr(
        worker, "_exec",
        lambda *a, **k: subprocess.CompletedProcess(a, 2, "", "boom"),
    )
    rel = worker.probe_port_relation("e", "devel/foo", use_cache=False)
    assert rel["ok"] is False
    assert rel["patch_origin"] == "devel/foo"
    assert rel["slave_port"] is False


def test_the_probe_asks_for_all_four_variables_in_order(monkeypatch):
    seen = {}

    def fake_exec(env, *argv, **kw):
        seen["cmd"] = argv[-1]
        return subprocess.CompletedProcess(
            argv, 0,
            "yes\ndatabases/mysql84-server\n/p/files\n/p/dragonfly\n", "",
        )

    monkeypatch.setattr(worker, "_exec", fake_exec)
    rel = worker.probe_port_relation(
        "e", "databases/mysql84-client", use_cache=False,
    )
    assert "-V SLAVE_PORT -V MASTER_PORT -V PATCHDIR -V DFLY_PATCHDIR" in seen["cmd"]
    assert rel["master_port"] == "databases/mysql84-server"


def test_a_short_make_v_response_is_not_guessed_at(monkeypatch):
    monkeypatch.setattr(
        worker, "_exec",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "yes\n", ""),
    )
    rel = worker.probe_port_relation("e", "x/y", use_cache=False)
    assert rel["ok"] is False
    assert "expected 4" in rel["error"]


# --------------------------------------------------------------------
# the invariants that widen with it
# --------------------------------------------------------------------

def test_origin_set_dedupes_and_keeps_order():
    assert worker.origin_set("a/b", ["a/b", "c/d", ""]) == ["a/b", "c/d"]


def test_assert_port_clean_looks_at_the_patch_origin_too(monkeypatch):
    seen = {}

    def fake_exec(env, *argv, **kw):
        seen["cmd"] = argv[-1]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(worker, "_exec", fake_exec)
    worker.assert_port_clean(
        "e", "databases/mysql84-client",
        also=["databases/mysql84-server"],
    )
    assert "ports/databases/mysql84-client" in seen["cmd"]
    assert "ports/databases/mysql84-server" in seen["cmd"]


def test_a_dirty_master_makes_the_slave_report_dirty(monkeypatch):
    monkeypatch.setattr(
        worker, "_exec",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, " M ports/databases/mysql84-server/overlay.dops\n", "",
        ),
    )
    clean = worker.assert_port_clean(
        "e", "databases/mysql84-client",
        also=["databases/mysql84-server"],
    )
    assert clean["ok"] is False
    assert clean["dirty_paths"] == [
        "ports/databases/mysql84-server/overlay.dops",
    ]


def test_the_subtree_hash_changes_when_the_master_changes(tmp_path, monkeypatch):
    class Paths:
        deltaports = tmp_path

    monkeypatch.setattr(worker, "env_paths", lambda env: Paths())
    slave = tmp_path / "ports" / "db" / "client"
    master = tmp_path / "ports" / "db" / "server"
    slave.mkdir(parents=True)
    master.mkdir(parents=True)
    (slave / "overlay.dops").write_text("port db/client\n")
    (master / "overlay.dops").write_text("port db/server\n")

    before = worker._port_subtree_hash("e", "db/client", also=["db/server"])
    # Edit only the MASTER. Scoped to the slave this is invisible, which
    # is how a stale compose tree used to pass the dsynth guard.
    (master / "overlay.dops").write_text("port db/server\nmk set X 1\n")
    after = worker._port_subtree_hash("e", "db/client", also=["db/server"])
    unscoped = worker._port_subtree_hash("e", "db/client")

    assert before != after
    assert unscoped == worker._port_subtree_hash("e", "db/client")


def test_a_missing_patch_origin_invalidates_the_baseline(tmp_path, monkeypatch):
    class Paths:
        deltaports = tmp_path

    monkeypatch.setattr(worker, "env_paths", lambda env: Paths())
    (tmp_path / "ports" / "db" / "client").mkdir(parents=True)
    assert worker._port_subtree_hash(
        "e", "db/client", also=["db/server"],
    ) == ""


def test_commit_port_changes_covers_the_patch_origin(monkeypatch):
    seen = {}

    def fake_exec(env, *argv, **kw):
        seen["cmd"] = argv[-1]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(worker, "_exec", fake_exec)
    res = worker.commit_port_changes(
        "e", "db/client", "msg", also=["db/server"],
    )
    assert "ports/db/client" in seen["cmd"]
    assert "ports/db/server" in seen["cmd"]
    assert res["paths_changed"] == ["ports/db/client", "ports/db/server"]


def test_an_explicit_paths_argument_still_commits_only_that_path(monkeypatch):
    seen = {}

    def fake_exec(env, *argv, **kw):
        seen["cmd"] = argv[-1]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(worker, "_exec", fake_exec)
    worker.commit_port_changes(
        "e", "db/client", "msg",
        paths="ports/db/server/overlay.dops", also=["db/other"],
    )
    assert "ports/db/other" not in seen["cmd"]


# --------------------------------------------------------------------
# building the set
# --------------------------------------------------------------------

def test_dsynth_build_passes_every_origin_to_one_dsynth_run(monkeypatch):
    seen = {}

    def fake_exec(env, *argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(worker, "_exec", fake_exec)
    monkeypatch.setattr(worker, "_dsynth_log_candidates", lambda e, o: [])
    monkeypatch.setattr(worker, "_dsynth_log_path", lambda o: "/tmp/l")
    monkeypatch.setattr(worker, "_port_subtree_hash", lambda e, o, **k: "h")
    monkeypatch.setitem(worker._MATERIALIZE_STATE, ("e", "db/client"), "h")

    res = worker.dsynth_build("e", "db/client", also=["db/server"])
    assert list(seen["argv"][-2:]) == ["db/client", "db/server"]
    assert res["built_origins"] == ["db/client", "db/server"]


def test_slave_siblings_confirms_grep_candidates_with_the_framework(monkeypatch):
    def fake_exec(env, *argv, **kw):
        # the narrowing grep
        return subprocess.CompletedProcess(
            argv, 0, "databases/mysql84-client\ndatabases/mysql84-clientx\n", "",
        )

    monkeypatch.setattr(worker, "_exec", fake_exec)
    monkeypatch.setattr(
        worker, "probe_port_relation",
        lambda env, o, **k: {
            "master_port": (
                "databases/mysql84-server"
                if o == "databases/mysql84-client" else "databases/other"
            ),
        },
    )
    siblings = worker.slave_siblings(
        "e", "databases/mysql84-server", use_cache=False,
    )
    # The basename grep cannot tell these apart; make -V can.
    assert siblings == ["databases/mysql84-client"]


def test_an_unenumerable_sibling_set_is_empty_not_partial(monkeypatch):
    monkeypatch.setattr(
        worker, "_exec",
        lambda *a, **k: subprocess.CompletedProcess(a, 2, "", ""),
    )
    assert worker.slave_siblings("e", "db/server", use_cache=False) == []


# --------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------

def _policy():
    return Policy(
        tiers={
            "ASSIST": Tier(name="ASSIST", max_iterations=4, max_tokens=120000),
            "MANUAL": Tier(name="MANUAL", max_iterations=0, max_tokens=0),
        },
        classification_to_tier={"compile-error": "ASSIST"},
        confidence_floor={"ASSIST": "medium"},
    )


class _History:
    failed_patch_attempts = 0
    recent_failures = 0
    has_fresh_user_context = False
    signature_repeat_count = 0


def test_a_slave_is_no_longer_refused_out_of_hand():
    dec = decision_mod.decide(
        classification="compile-error",
        confidence="high",
        history=_History(),
        env_health=None,
        policy=_policy(),
        is_slave=True,
    )
    assert dec.action == "auto_patch"
    assert "slave_port_unsupported" not in dec.reason


def test_slaveness_is_still_recorded_on_the_decision():
    dec = decision_mod.decide(
        classification="compile-error",
        confidence="high",
        history=_History(),
        env_health=None,
        policy=_policy(),
        is_slave=True,
    )
    assert dec.extra.get("is_slave") is True


def test_a_non_slave_decision_carries_no_slave_marker():
    dec = decision_mod.decide(
        classification="compile-error",
        confidence="high",
        history=_History(),
        env_health=None,
        policy=_policy(),
        is_slave=False,
    )
    assert "is_slave" not in dec.extra


def test_the_slave_refusal_is_gone_from_the_source():
    import inspect
    src = inspect.getsource(decision_mod.decide)
    assert "slave_port_unsupported" not in src


# --------------------------------------------------------------------
# the guards that keep a patch where the build reads it
# --------------------------------------------------------------------

def test_install_patches_redirects_to_the_patch_origin(tmp_path, monkeypatch):
    class Paths:
        writable = tmp_path / "w"
        deltaports = tmp_path / "d"

    src = Paths.writable / "work" / "genpatch-out"
    src.mkdir(parents=True)
    (src / "patch-foo").write_text(
        "--- a/foo\n+++ b/foo\n@@ -1 +1 @@\n-a\n+b\n"
    )
    monkeypatch.setattr(worker, "env_paths", lambda env: Paths())
    monkeypatch.setattr(
        worker, "patch_origin_for",
        lambda env, o: "databases/mysql84-server",
    )

    res = worker.install_patches("e", "databases/mysql84-client")

    assert res["patch_origin"] == "databases/mysql84-server"
    assert "mysql84-server/dragonfly" in res["destination"]
    assert "mysql84-client/dragonfly" not in res["destination"]
    assert "slave port" in res["note"]
    assert (
        Paths.deltaports / "ports" / "databases" / "mysql84-server"
        / "dragonfly" / "patch-foo"
    ).is_file()


def test_install_patches_says_nothing_extra_for_a_normal_port(tmp_path, monkeypatch):
    class Paths:
        writable = tmp_path / "w"
        deltaports = tmp_path / "d"

    src = Paths.writable / "work" / "genpatch-out"
    src.mkdir(parents=True)
    (src / "patch-foo").write_text(
        "--- a/foo\n+++ b/foo\n@@ -1 +1 @@\n-a\n+b\n"
    )
    monkeypatch.setattr(worker, "env_paths", lambda env: Paths())
    monkeypatch.setattr(worker, "patch_origin_for", lambda env, o: o)

    res = worker.install_patches("e", "devel/gperf")
    assert "note" not in res
    assert res["patch_origin"] == "devel/gperf"


# --------------------------------------------------------------------
# the post-compose assertion
# --------------------------------------------------------------------

def test_literal_expectations_drops_make_expressions():
    from dportsv3.engine.oracle import literal_expectations

    kept = literal_expectations({
        "USE_GCC_VERSION": "13",
        "PATCHDIR": "${.CURDIR}/dragonfly",
        "EMPTY": "",
    })
    assert kept == {"USE_GCC_VERSION": "13"}


def test_the_oracle_reports_a_value_the_master_clobbered(tmp_path):
    from dportsv3.engine.oracle import run_bmake_oracle

    (tmp_path / "Makefile").write_text("# port\n")

    def fake_run(command, cwd):
        if "-V" in command:
            # the master's plain assignment won
            return subprocess.CompletedProcess(command, 0, "12\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = run_bmake_oracle(
        tmp_path, profile="local", run_command=fake_run,
        bmake_path="/usr/bin/bmake",
        expect={"USE_GCC_VERSION": "13"},
    )
    assert result.ok is False
    assert "did not survive" in result.failures[0]
    assert "'12'" in result.failures[0]


def test_the_oracle_is_quiet_when_the_value_survived(tmp_path):
    from dportsv3.engine.oracle import run_bmake_oracle

    (tmp_path / "Makefile").write_text("# port\n")
    result = run_bmake_oracle(
        tmp_path, profile="local",
        run_command=lambda c, cwd: subprocess.CompletedProcess(c, 0, "13\n", ""),
        bmake_path="/usr/bin/bmake",
        expect={"USE_GCC_VERSION": "13"},
    )
    assert result.ok is True
    assert result.failures == []


# --------------------------------------------------------------------
# verify
# --------------------------------------------------------------------

def test_verify_builds_the_master_and_its_other_slaves(monkeypatch):
    from dportsv3 import verify_fix

    monkeypatch.setattr(
        worker, "patch_origin_for",
        lambda env, o: "databases/mysql84-server",
    )
    monkeypatch.setattr(
        worker, "slave_siblings",
        lambda env, o, **k: ["databases/mysql84-client", "databases/mysql84-other"],
    )
    also = verify_fix.sibling_origins_for("e", "databases/mysql84-client")
    # the master, plus the sibling that is not the origin itself
    assert also == ["databases/mysql84-server", "databases/mysql84-other"]


def test_verify_falls_back_to_a_single_origin_build_when_probing_fails(monkeypatch):
    from dportsv3 import verify_fix

    def boom(*a, **k):
        raise RuntimeError("chroot gone")

    monkeypatch.setattr(worker, "patch_origin_for", boom)
    assert verify_fix.sibling_origins_for("e", "devel/gperf") == []


# --------------------------------------------------------------------
# apply-and-build takes the set
# --------------------------------------------------------------------

def test_apply_and_build_accepts_repeatable_also():
    from dports_dev_env.cli import build_parser

    args = build_parser().parse_args([
        "apply-and-build", "env", "db/client",
        "--also", "db/server", "--also", "db/other",
    ])
    assert args.also == ["db/server", "db/other"]


def test_apply_and_build_defaults_also_to_none():
    from dports_dev_env.cli import build_parser

    args = build_parser().parse_args(["apply-and-build", "env", "db/client"])
    assert args.also is None


def test_a_relative_dfly_patchdir_means_the_ports_own_directory():
    # editors/pico-alpine sets PATCHDIR= (empty) deliberately, so
    # ${PATCHDIR:H} degenerates to "." and DFLY_PATCHDIR comes back as
    # "./dragonfly". do-patch resolves that against make's cwd, which
    # is the port's own directory — verified on the @2026Q3 tree.
    rel = _relation(
        "editors/pico-alpine", "yes", "mail/alpine", "", "./dragonfly",
    )
    assert rel["own_patchdir"] is True
    assert rel["patch_origin"] == "editors/pico-alpine"


# --------------------------------------------------------------------
# triage wiring
# --------------------------------------------------------------------

def test_triage_bootstraps_the_overlay_at_the_patch_origin():
    # The header overlay has to be written where DFLY_PATCHDIR points,
    # or the patch agent authors a body the build never reads.
    import inspect

    from dportsv3.agent import steps

    src = inspect.getsource(steps.TriageStep.run)
    assert "origin=patch_origin," in src
    # and the old blanket skip is gone
    assert "if not is_slave and services.ensure_overlay_or_abort" not in src


def test_the_patch_preflight_checks_the_patch_origin():
    import inspect

    from dportsv3.agent import steps

    src = inspect.getsource(steps.PatchAttemptStep.run)
    assert "patch_origin_for(env, origin)" in src
    assert 'overlay_rel = f"ports/{patch_origin}/overlay.dops"' in src


def test_the_probe_reads_the_last_four_lines_not_the_first(monkeypatch):
    # A Makefile that prints before bsd.port.mk is read would otherwise
    # shift every value by one and yield a wrong patch origin.
    monkeypatch.setattr(
        worker, "_exec",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0,
            "noise from the port\n"
            "yes\ndatabases/mysql84-server\n/p/files\n/p/dragonfly\n",
            "",
        ),
    )
    rel = worker.probe_port_relation(
        "e", "databases/mysql84-client", use_cache=False,
    )
    assert rel["slave_port"] is True
    assert rel["master_port"] == "databases/mysql84-server"


def test_compose_drops_the_cached_relation(monkeypatch):
    # An overlay can add MASTERDIR or redirect PATCHDIR, which moves the
    # patch origin. Compose is when that takes effect.
    worker._RELATION_CACHE[("e", "db/client")] = {"patch_origin": "stale"}
    monkeypatch.setattr(
        worker, "invariant_origins", lambda env, o: ["db/client"],
    )
    monkeypatch.setattr(
        worker, "_exec",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""),
    )
    monkeypatch.setattr(worker, "_port_subtree_hash", lambda e, o, **k: "h")
    worker.materialize_dports("e", "db/client")
    assert ("e", "db/client") not in worker._RELATION_CACHE
