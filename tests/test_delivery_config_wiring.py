"""Delivery has to be reachable on a deployed host, and off until asked (poly-cu8.1).

Two failures, one cause. Nothing ever set ``$DPORTSV3_CONFIG_DIR`` for the
services — neither rc script did, and the packaged console script cannot —
so no operator file could be found. And ``resolve_config`` treated the
*bundled sample* as a live default, which is what made the first failure
loud instead of silent:

    type = "github"
    repo = "DragonFlyBSD/DeltaPorts"
    clone_dir = <a path that exists on no host>

That resolved, loaded, failed its token lookup, and raised — so every single
bundle Accept reported ``create_failed`` against the real upstream repo
rather than taking the ``no_config`` skip immediately below it, and
``delivery_sync`` swallowed the same exception and quietly stopped
reconciling merges.

The fix is a deliberate asymmetry with ``paths.config_file``:
``agentic-policy.json.sample`` is a usable default for any host, so falling
back to it is right; a delivery config names one repo, one clone and one
credential, so there is nothing safe to fall back to. Missing means off.
"""

from __future__ import annotations

import logging

import pytest

from dportsv3 import paths
from dportsv3.commands import deploy as dep
from dportsv3.delivery import DeliveryConfigError, preflight
from dportsv3.delivery.orchestrator import resolve_config


DEPLOY_SRC = paths._PKG.parent / "deploy"


def _write_toml(d, body: str):
    p = d / "delivery.toml"
    p.write_text(body)
    return p


_GITHUB = """
[provider]
type = "github"
repo = "example/ports"
clone_dir = "{clone}"
"""


# --- the resolver no longer invents a configuration -------------------------

def test_no_config_dir_means_delivery_is_off(tmp_path) -> None:
    """The whole bug. An unconfigured host must resolve to None, which the
    Accept path renders as skip_reason=no_config."""
    assert resolve_config(env={}) is None


def test_the_bundled_sample_is_never_loaded(tmp_path) -> None:
    """It exists, it parses, and it names a real repo — so the only thing
    keeping it out of a delivery is that nothing looks at it."""
    sample = paths.BUNDLED_CONFIG_DIR / "delivery.toml.sample"
    assert sample.is_file(), "the template must still ship"
    assert resolve_config(env={}) is None
    assert resolve_config(env={"DPORTSV3_CONFIG_DIR": str(sample.parent)}) is None, (
        "a directory holding only the .sample is still an unconfigured host"
    )


def test_a_config_dir_without_delivery_toml_is_off(tmp_path) -> None:
    (tmp_path / "agentic-policy.json").write_text("{}")
    assert resolve_config(env={"DPORTSV3_CONFIG_DIR": str(tmp_path)}) is None


def test_the_operator_file_is_loaded_when_it_exists(tmp_path) -> None:
    _write_toml(tmp_path, _GITHUB.format(clone=tmp_path / "clone"))
    cfg = resolve_config(env={
        "DPORTSV3_CONFIG_DIR": str(tmp_path),
        "DPORTSV3_DELIVERY_TOKEN": "t",
    })
    assert cfg is not None and cfg.repo == "example/ports"


def test_the_explicit_path_override_still_wins(tmp_path) -> None:
    other = tmp_path / "elsewhere.toml"
    other.write_text(_GITHUB.format(clone=tmp_path / "clone"))
    cfg = resolve_config(env={
        "DPORTSV3_DELIVERY_CONFIG": str(other),
        "DPORTSV3_DELIVERY_TOKEN": "t",
    })
    assert cfg is not None and cfg.repo == "example/ports"


def test_a_malformed_config_still_raises(tmp_path) -> None:
    """Silence is only correct for 'not configured'. A file the operator
    wrote and got wrong has to say so."""
    _write_toml(tmp_path, "this is not toml [[[")
    with pytest.raises(DeliveryConfigError):
        resolve_config(env={"DPORTSV3_CONFIG_DIR": str(tmp_path)})


def test_the_docstring_matches_the_code() -> None:
    """It used to promise a third, repo-anchored tier that the code did not
    implement, which is how the bundled fallback stayed unnoticed."""
    doc = " ".join((resolve_config.__doc__ or "").split())
    assert "<repo-root>/config/delivery.toml" not in doc
    assert "only two entries" in doc
    assert "deliberately" in doc, "the asymmetry has to be stated, not implied"


# --- the services can find their config dir ---------------------------------

@pytest.mark.parametrize("script", ["polytropos_tracker", "polytropos_runner"])
def test_both_rc_scripts_export_the_config_dir(script) -> None:
    """Without this the operator file is installed somewhere nothing looks."""
    text = (DEPLOY_SRC / "rc.d" / script).read_text()
    assert 'polytropos_config_dir:="/usr/local/etc/polytropos"' in text
    assert "DPORTSV3_CONFIG_DIR=${polytropos_config_dir}" in text


def test_the_shared_conf_declares_the_knob() -> None:
    text = (DEPLOY_SRC / "polytropos.conf.sample").read_text()
    assert ": ${polytropos_config_dir:=" in text


# --- the installer puts the template where the services look ----------------

def test_deploy_installs_the_delivery_template() -> None:
    entry = next(
        (f for f in dep.OPERATOR_FILES if "delivery" in f[0]), None,
    )
    assert entry is not None, "deploy install must place the delivery template"
    sample, dest, mode, group_owned = entry
    assert (DEPLOY_SRC / sample).is_file()
    assert dest.endswith(".sample"), (
        "installing it under the live name would enable delivery on a host "
        "nobody configured — the file existing is what makes it load"
    )
    assert mode == 0o640
    assert group_owned, "the tracker delivers, and it does not run as root"


def test_the_installed_template_is_not_the_live_name() -> None:
    """Belt and braces on the one that matters: no OPERATOR_FILES entry may
    land a file at a path resolve_config would read."""
    live = {f[1] for f in dep.OPERATOR_FILES}
    assert "polytropos/delivery.toml" not in live


def test_the_deploy_template_parses_and_is_inert(tmp_path) -> None:
    """A template that does not load is useless; one that needs a token to
    load is a worse first run. local-patch gives an operator a working
    delivery with no credentials."""
    from dportsv3.delivery.config import load_delivery_config

    dst = tmp_path / "delivery.toml"
    dst.write_text((DEPLOY_SRC / "delivery.toml.sample").read_text())
    cfg = load_delivery_config(dst, env={})
    assert cfg.provider_type == "local-patch"
    assert cfg.outbox, "local-patch is only useful with somewhere to write"
    assert cfg.token is None, "the smoke-test provider must need no credential"


# --- the token's mode follows its reader ------------------------------------

def test_the_token_mode_is_documented_for_the_process_that_reads_it() -> None:
    """0400 root is unreadable by the tracker, which is the only thing that
    delivers. The docs said 0400 and the deployment obeyed them."""
    from dportsv3.delivery import config as delivery_config

    doc = " ".join((delivery_config.__doc__ or "").split())
    # 0400 still appears — as the thing being corrected. What matters is
    # which mode is prescribed and that the reason is the reader.
    assert "0640 root" in doc
    assert "not 0400 root" in doc
    assert "tracker" in doc.lower()


def test_the_config_readme_says_the_same() -> None:
    readme = (paths._PKG.parent / "config" / "README.md").read_text()
    assert "0640" in readme
    assert "clone_dir" in readme


# --- the preflight answers at startup, not at Accept ------------------------

def test_preflight_calls_out_an_unset_config_dir(tmp_path) -> None:
    """Distinct from 'no delivery.toml'. An unset config dir means nothing
    operator-owned resolves at all, policy included, and on a packaged
    install that is a deployment fault rather than a decision."""
    findings = preflight.check(env={})
    assert [f.level for f in findings] == ["warn"]
    assert "DPORTSV3_CONFIG_DIR" in findings[0].detail


def test_preflight_reports_a_configured_dir_with_no_delivery_as_ok(tmp_path) -> None:
    findings = preflight.check(env={"DPORTSV3_CONFIG_DIR": str(tmp_path)})
    assert [f.level for f in findings] == ["ok"]
    assert "not configured" in findings[0].detail
    assert str(tmp_path) in findings[0].detail, "say where to put the file"


def test_preflight_warns_about_a_world_readable_token(tmp_path) -> None:
    """The mode nobody enforces, on the file that most needs one."""
    _write_toml(tmp_path, _GITHUB.format(clone=tmp_path / "clone"))
    (tmp_path / "clone" / ".git").mkdir(parents=True)
    token = tmp_path / "delivery.token"
    token.write_text("ghp_x")
    token.chmod(0o644)

    findings = preflight.check(env={"DPORTSV3_CONFIG_DIR": str(tmp_path)})
    warns = [f for f in findings if f.level == "warn"]
    assert any("world-readable" in f.detail for f in warns), findings


def test_preflight_accepts_a_correctly_moded_token(tmp_path) -> None:
    _write_toml(tmp_path, _GITHUB.format(clone=tmp_path / "clone"))
    (tmp_path / "clone" / ".git").mkdir(parents=True)
    token = tmp_path / "delivery.token"
    token.write_text("ghp_x")
    token.chmod(0o640)

    findings = preflight.check(env={"DPORTSV3_CONFIG_DIR": str(tmp_path)})
    assert not any("world-readable" in f.detail for f in findings)


def test_preflight_names_a_missing_clone_dir_and_the_account(tmp_path) -> None:
    _write_toml(tmp_path, _GITHUB.format(clone=tmp_path / "absent"))
    findings = preflight.check(env={
        "DPORTSV3_CONFIG_DIR": str(tmp_path),
        "DPORTSV3_DELIVERY_TOKEN": "t",
    })
    errs = [f for f in findings if f.level == "error"]
    assert errs, "a clone_dir that does not exist must be an error"
    assert "absent" in errs[0].detail, "the report has to name the path"


def test_preflight_rejects_a_clone_dir_that_is_not_a_git_tree(tmp_path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    _write_toml(tmp_path, _GITHUB.format(clone=clone))
    findings = preflight.check(env={
        "DPORTSV3_CONFIG_DIR": str(tmp_path),
        "DPORTSV3_DELIVERY_TOKEN": "t",
    })
    assert any(f.level == "error" and "git" in f.detail for f in findings)


def test_preflight_checks_the_outbox_for_local_patch(tmp_path) -> None:
    outbox = tmp_path / "out"
    outbox.mkdir()
    _write_toml(
        tmp_path,
        f'[provider]\ntype = "local-patch"\noutbox = "{outbox}"\n',
    )
    findings = preflight.check(env={"DPORTSV3_CONFIG_DIR": str(tmp_path)})
    assert all(f.level == "ok" for f in findings), findings


def test_preflight_accepts_an_outbox_that_does_not_exist_yet(tmp_path) -> None:
    """LocalPatchProvider creates it on first use, so its absence is not a
    fault — and an operator watching for a file needs to be told that."""
    _write_toml(
        tmp_path,
        f'[provider]\ntype = "local-patch"\n'
        f'outbox = "{tmp_path / "not-yet"}"\n',
    )
    findings = preflight.check(env={"DPORTSV3_CONFIG_DIR": str(tmp_path)})
    assert all(f.level == "ok" for f in findings), findings


def test_preflight_turns_a_broken_config_into_a_finding(tmp_path) -> None:
    """It runs at startup. Raising here would mean a stale credential could
    stop the tracker from serving, which is worse than the credential."""
    _write_toml(tmp_path, "not toml [[[")
    findings = preflight.check(env={"DPORTSV3_CONFIG_DIR": str(tmp_path)})
    assert [f.level for f in findings] == ["error"]


def test_preflight_never_raises_whatever_resolve_config_does(monkeypatch) -> None:
    from dportsv3.delivery import orchestrator

    def _boom(**_kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(orchestrator, "resolve_config", _boom)
    findings = preflight.check(env={})
    assert [f.level for f in findings] == ["error"]
    assert "disk on fire" in findings[0].detail


def test_the_report_is_loggable() -> None:
    pairs = preflight.format_report(preflight.check(env={}))
    assert pairs and all(len(p) == 2 for p in pairs)
    assert all(m.startswith("delivery preflight: ") for _lvl, m in pairs)


def test_the_tracker_logs_the_preflight_at_startup(caplog) -> None:
    """The finding is only worth producing if it reaches the operator's log."""
    from dportsv3.tracker import server

    with caplog.at_level(logging.INFO, logger=server.__name__):
        server._log_delivery_preflight()
    assert any("delivery preflight" in r.message for r in caplog.records)


# --- merge reconciliation says why it stopped -------------------------------

def _probe_with_config(monkeypatch, config_dir):
    from dportsv3.tracker import delivery_sync

    delivery_sync._PROBE_FAILURES_LOGGED.clear()
    monkeypatch.setenv("DPORTSV3_CONFIG_DIR", str(config_dir))
    return delivery_sync


def test_merge_reconciliation_logs_why_it_degraded(
    tmp_path, caplog, monkeypatch,
) -> None:
    """It used to swallow the exception whole, so a config error meant PRs
    silently stopped being marked merged. Degrading is right; degrading
    without a word is not."""
    _write_toml(tmp_path, "not toml [[[")
    sync = _probe_with_config(monkeypatch, tmp_path)

    with caplog.at_level(logging.WARNING, logger=sync.__name__):
        assert sync._resolve_merge_probe(None) is None

    assert any("merge reconciliation is off" in r.message
               for r in caplog.records)


def test_the_degradation_is_logged_once_not_per_page_view(
    tmp_path, caplog, monkeypatch,
) -> None:
    """_resolve_merge_probe runs on page routes, so an unconditional warning
    would put one line in the log per view of a bundle."""
    _write_toml(tmp_path, "not toml [[[")
    sync = _probe_with_config(monkeypatch, tmp_path)

    with caplog.at_level(logging.WARNING, logger=sync.__name__):
        for _ in range(5):
            sync._resolve_merge_probe(None)

    hits = [r for r in caplog.records
            if "merge reconciliation is off" in r.message]
    assert len(hits) == 1, f"logged {len(hits)} times"


def test_an_unconfigured_host_logs_nothing_at_all(
    tmp_path, caplog, monkeypatch,
) -> None:
    """No config is not an error, and must not warn on a host that never
    opted into delivery."""
    sync = _probe_with_config(monkeypatch, tmp_path)

    with caplog.at_level(logging.WARNING, logger=sync.__name__):
        assert sync._resolve_merge_probe(None) is None

    assert not caplog.records


# --- the findings have to actually reach the log ----------------------------

def test_the_package_logger_gets_a_handler_at_serve_time() -> None:
    """Measured on hardware: the preflight ran at startup and produced
    nothing. uvicorn attaches handlers to its own loggers and leaves the
    root bare, so every INFO record this package emits was dropped — which
    also swallowed the delivery-outcome lines bundle_actions documents as
    'one activity row + one daemon log line per outcome'."""
    from dportsv3.commands import tracker as tracker_cmd

    log = logging.getLogger("dportsv3")
    saved_handlers, saved_level, saved_prop = (
        list(log.handlers), log.level, log.propagate,
    )
    try:
        log.handlers.clear()
        tracker_cmd._configure_app_logging()
        assert log.handlers, "nothing would be written anywhere"
        assert log.isEnabledFor(logging.INFO)
        assert log.propagate is False, (
            "handled here and propagating would double every line"
        )
    finally:
        log.handlers[:] = saved_handlers
        log.setLevel(saved_level)
        log.propagate = saved_prop


def test_configuring_the_logger_twice_does_not_double_it() -> None:
    from dportsv3.commands import tracker as tracker_cmd

    log = logging.getLogger("dportsv3")
    saved_handlers, saved_level, saved_prop = (
        list(log.handlers), log.level, log.propagate,
    )
    try:
        log.handlers.clear()
        tracker_cmd._configure_app_logging()
        tracker_cmd._configure_app_logging()
        assert len(log.handlers) == 1
    finally:
        log.handlers[:] = saved_handlers
        log.setLevel(saved_level)
        log.propagate = saved_prop


def test_serve_configures_logging_before_the_app_starts() -> None:
    """create_app registers the startup hook that runs the preflight, so
    configuring afterwards would still lose the first messages."""
    import inspect

    from dportsv3.commands import tracker as tracker_cmd

    src = inspect.getsource(tracker_cmd._cmd_serve)
    assert src.index("_configure_app_logging()") < src.index("create_app(db_path)")


def test_the_handler_writes_to_a_line_buffered_stream() -> None:
    """stdout is block-buffered once daemon(8) points it at a file, so a
    startup error on an idle server would sit unflushed — the one case
    this reporting exists for. uvicorn's own default handler uses stderr
    for the same reason."""
    import sys

    from dportsv3.commands import tracker as tracker_cmd

    log = logging.getLogger("dportsv3")
    saved = list(log.handlers), log.level, log.propagate
    try:
        log.handlers.clear()
        tracker_cmd._configure_app_logging()
        assert log.handlers[0].stream is sys.stderr
    finally:
        log.handlers[:] = saved[0]
        log.setLevel(saved[1])
        log.propagate = saved[2]
