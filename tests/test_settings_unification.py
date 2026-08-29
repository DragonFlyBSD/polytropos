"""One settings file, and an environment surface that cannot grow back (poly-cu8.2).

Configuring an install used to mean six files in five formats — a shell
``polytropos.conf``, two shell ``.env`` files, a TOML, a JSON, and a raw
token — under 69 environment variables, of which 39 were read by the code
and named in no sample or script anywhere. On a packaged install there
was simply nowhere to put a value for those 39.

The cause was structural. ``os.environ`` was the only channel a setting
had into the code, so a shell file that exports was the only transport,
and adding a variable cost one line while documenting one was a separate
act of will that nothing enforced.

The fix is a table. Everything derives from it — the value, the sample,
the provenance, the unknown-key warning — so the interesting tests here
are the ones that pin the *structure*, not individual values:
``test_no_module_reads_an_undeclared_environment_variable`` is the one
that keeps the surface from growing back, and it is why the count is
allowed to be small rather than merely documented.

Two of the 69 were found by these tests rather than by the audit that
preceded them, both for the same reason: ``DP_HARNESS_CONTEXT_FILE_CAP``
and ``DPORTSV3_STALE_QUEUED_MAX_AGE_SECONDS`` put ``os.environ.get(`` and
the name on different lines, so a grep for the call and the name together
saw neither. That is the argument for checking this mechanically instead
of periodically.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from dports_dev_env import config as dev_env_config
from dports_dev_env.confschema import ConfigError, Schema, Setting
from dportsv3 import paths, settings
from dportsv3.commands import config as config_cmd


REPO = Path(__file__).resolve().parents[1]


# --- the surface cannot grow back -------------------------------------------

#: Variables that are deliberately not settings, each with a reason that
#: is not "we forgot". Anything outside this list and the schema tables
#: is a new environment variable, which is what this file exists to stop.
ALLOWED_NON_SETTINGS = {
    # Names the settings file, so it cannot live inside it.
    "DPORTSV3_CONFIG_DIR",
    # Inter-process arguments spelled as environment: a parent telling a
    # child about this one exec. Better as CLI flags, but not config.
    "DPORTSV3_CMD",
    "DPORTSV3_NO_BOOTSTRAP",
    "DPORTSV3_TRACKER_TARGET",
    "DPORTS_DEV_ENV_CMD",
    "DPORTS_DEV_TOOL_ROOT",
    "DPORTS_DEV_ENV",
    "DPORTS_DEV_ENV_QUIET",
    "DPORTS_DEV_RUNTIME_PROFILE",
    "DPORTS_DEV_DELTA_ROOT",
    # The legacy credential variables, still honoured so an install that
    # has not run `config migrate` keeps working. Each warns when used.
    *config_cmd.KEY_TO_SECRET,
    # An explicit delivery.toml path, kept while the standalone file is
    # still read.
    "DPORTSV3_DELIVERY_CONFIG",
    # Shell variables in the helper scripts dev-env generates INSIDE a
    # chroot, and the environment the dsynth hooks run with. They are
    # arguments to a shell script, not values this code reads — and a
    # chroot cannot see a settings file on the host anyway.
    "DPORTSV3_BIN", "DPORTS_TARGET", "DPORTS_ORIGIN",
    "DPORTS_COMPOSE_ROOT", "DPORTS_LOCK_ROOT", "DPORTS_DSYNTH_ROOT",
    "DPORTS_DSYNTH_PROFILE", "DPORTS_TOUCHED_ORIGINS_FILE",
    "DPORTS_HELPER_BIN", "DPORTS_ORACLE_PROFILE",
    "DPORTS_DOC_USER_GUIDE", "DPORTS_DOC_DEV_ENV",
    # Not ours.
    "TERM", "HOME", "PATH",
}

#: The migration table has to name every old variable — that is its whole
#: job — so it is the one file exempt from the literal scan below.
MIGRATION_TABLE = "dportsv3/commands/config.py"

_ENV_READ = re.compile(
    r'(?:os\.environ(?:\.get)?|os\.getenv|env\.get)\s*\(\s*["\']'
    r'([A-Z][A-Z0-9_]*)["\']',
)


def _source_files() -> list[Path]:
    out: list[Path] = []
    for root in (REPO / "dportsv3", REPO / "dev-env" / "dports_dev_env"):
        out.extend(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    return out


def _declared_env_names() -> set[str]:
    names = {s.env for s in settings.SETTINGS if s.env}
    names |= {s.env for s in dev_env_config.SETTINGS if s.env}
    return {n for n in names if n}


def test_no_module_reads_an_undeclared_environment_variable() -> None:
    """The structural guarantee. A variable that is not in a schema table
    and not in the short exemption list above does not exist, so "read by
    the code, settable nowhere" cannot come back — it is caught here
    rather than in an audit somebody has to remember to run."""
    allowed = ALLOWED_NON_SETTINGS | _declared_env_names()
    offenders: dict[str, list[str]] = {}
    for path in _source_files():
        for name in set(_ENV_READ.findall(path.read_text())):
            if name in allowed or name.startswith("DP_TEST_"):
                continue
            offenders.setdefault(name, []).append(
                str(path.relative_to(REPO)))
    assert not offenders, (
        f"environment variables read but not declared as settings: "
        f"{offenders}"
    )


def test_no_environment_variable_name_appears_as_a_bare_literal() -> None:
    """The strongest form, and the one that actually holds.

    Matching on ``os.environ.get("NAME")`` only catches the direct shape.
    Four confirm-loop knobs hid from exactly that check by passing the
    name to a helper — ``_env_int("DP_CONFIRM_MAX_FAILURES", 3)`` — so
    the name never appeared beside ``os.environ`` anywhere in the file.
    Two more hid by putting the call and the name on different lines.

    So this looks for the *name*, however it is reached: any string
    literal that is exactly a DP_/DPORTS_ variable name. Prose in a
    docstring does not match, because the whole literal has to be the
    name and a docstring is one long string.
    """
    allowed = ALLOWED_NON_SETTINGS | _declared_env_names()
    # A trailing underscore means a prefix being built up, not a name.
    pattern = re.compile(r"^(DP_|DPORTS)[A-Z0-9_]*[A-Z0-9]$")
    offenders: dict[str, list[str]] = {}
    for path in _source_files():
        if str(path.relative_to(REPO)) == MIGRATION_TABLE:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Constant):
                continue
            value = node.value
            if not isinstance(value, str) or not pattern.match(value):
                continue
            if value in allowed or value.startswith("DP_TEST_"):
                continue
            offenders.setdefault(value, []).append(
                str(path.relative_to(REPO)))
    assert not offenders, (
        f"environment variable names present as literals but not declared "
        f"as settings: {offenders}"
    )


def _literal(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(
        node.value, str) else None


def test_the_override_list_stays_short() -> None:
    """Not a style rule. A blanket "env overrides file" layer is three
    lines and would rebuild the old surface under a new name; the whole
    value of the change is that each override had to be argued for
    individually. If this number climbs, that argument stopped happening."""
    overrides = [s for s in settings.SETTINGS if s.env]
    assert len(overrides) <= 10, [s.env for s in overrides]
    assert not [s for s in dev_env_config.SETTINGS if s.env], (
        "every dev-env value is a cache path, a URL or a package list — "
        "all knowable when the file is written"
    )


@pytest.mark.parametrize("setting_path,reason", [
    ("runner.dump_session", "per-run debug switch"),
    ("llm.backend", "documented escape hatch, temporary"),
    ("llm.triage.reasoning", "temporary, tied to a litellm version"),
    ("llm.patch.reasoning", "temporary, tied to a litellm version"),
    ("policy.file", "point one run at a different policy"),
    ("delivery.token_file", "a secret store may inject it"),
    ("paths.delta_root", "already has a command-line flag"),
    ("tracker.url", "has to cross into a chroot"),
])
def test_each_override_is_one_of_the_argued_cases(setting_path, reason) -> None:
    setting = next(s for s in settings.SETTINGS if s.path == setting_path)
    assert setting.env, f"{setting_path} should keep an override: {reason}"


def test_the_confirm_loop_knobs_are_settings() -> None:
    """These four hid from the direct-shape audit by going through a
    helper that took the name as a parameter, so the name never appeared
    beside os.environ. They are why the literal scan above exists."""
    known = {s.path for s in settings.SETTINGS}
    for name in ("runner.confirm_green_threshold",
                 "runner.confirm_max_failures",
                 "runner.confirm_backoff_seconds",
                 "runner.confirm_backoff_max_seconds"):
        assert name in known


def test_a_bad_confirm_value_falls_back_rather_than_stopping_the_runner(
    set_setting,
) -> None:
    """An operator typo must not take the runner down, and a zero must
    not disable the very bound the knob exists to set — which would read
    as configured while being off."""
    from dportsv3.agent import runner

    set_setting("runner.confirm_green_threshold", 0)
    assert runner._setting_int("runner.confirm_green_threshold", 2) == 1
    assert runner._setting_int("runner.no_such_setting", 7) == 7


# --- the engine lives on the right side of the dependency -------------------

def test_the_shared_engine_lives_in_the_dev_env_distribution() -> None:
    """dportsv3 imports dports_dev_env and nothing imports back. Putting
    the engine in the generator and importing it from the dev-env would
    invert that and reintroduce exactly the coupling paths.py exists to
    prevent — the expensive-to-reverse decision in this change."""
    engine = REPO / "dev-env" / "dports_dev_env" / "confschema.py"
    assert engine.is_file()
    source = engine.read_text()
    assert "import dportsv3" not in source
    assert "from dportsv3" not in source


def test_nothing_in_the_dev_env_imports_the_generator() -> None:
    for path in (REPO / "dev-env" / "dports_dev_env").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text()
        assert "import dportsv3" not in text, path
        assert "from dportsv3" not in text, path


def test_the_two_schemas_share_one_file() -> None:
    """Separate tables, one document. The dev-env does not know what a
    bundle is and must not learn; sharing the reader rather than the
    table is what keeps that true."""
    assert settings.CONFIG_FILENAME == "polytropos.toml"
    assert str(dev_env_config.settings_path() or "polytropos.toml").endswith(
        "polytropos.toml")


# --- the engine's own behaviour ---------------------------------------------

def _schema(**kw) -> Schema:
    return Schema([
        Setting("a.n", "int", 3, "count", env="DP_TEST_N"),
        Setting("a.p", "path", None, "optional path"),
        Setting("a.b", "bool", False, "flag"),
        Setting("a.l", "list", ["x"], "words"),
    ], name="t")


def test_an_unset_optional_path_is_none_not_the_current_directory() -> None:
    """Path("") normalises to Path("."), so returning it would turn "no
    policy file configured" into "read the current directory" — which is
    precisely what happened, and produced `Is a directory: '.'` from the
    policy loader."""
    s = _schema()
    s.load_from({})
    assert s.get("a.p") is None
    s.load_from({"a": {"p": "  "}})
    assert s.get("a.p") is None


def test_an_empty_environment_value_means_unset() -> None:
    """Clearing a variable has to fall back, not force an empty string on
    a caller that wanted a number."""
    s = _schema()
    s.load_from({"a": {"n": 9}})
    assert s.resolve("a.n", env={"DP_TEST_N": "  "}).source == "file"
    assert s.resolve("a.n", env={"DP_TEST_N": "11"}).value == 11


def test_a_wrong_type_is_an_error_naming_the_setting() -> None:
    """Silently falling back to the default is the failure this whole
    change exists to remove: the edit has no effect and nothing says why."""
    s = _schema()
    s.load_from({"a": {"n": "nine"}})
    with pytest.raises(ConfigError, match="a.n"):
        s.resolve("a.n")


def test_a_key_nobody_reads_is_reported() -> None:
    s = _schema()
    s.load_from({"a": {"n": 1, "typo": 2}, "elsewhere": {"x": 1}})
    assert "a.typo" in s.unknown_keys()
    assert "elsewhere" in s.unknown_keys()
    assert "elsewhere" not in s.unknown_keys(claimed={"elsewhere"})


def test_a_missing_file_is_not_an_error() -> None:
    """Every setting has a default, so no file is a working install."""
    s = _schema()
    s.load(REPO / "does-not-exist.toml")
    assert s.get("a.n") == 3


def test_a_file_that_exists_and_does_not_parse_is_an_error(tmp_path) -> None:
    bad = tmp_path / "polytropos.toml"
    bad.write_text("this is not toml [[[")
    s = _schema()
    with pytest.raises(ConfigError, match="not valid TOML"):
        s.load(bad)


# --- the sample is generated, so it cannot drift ----------------------------

def test_the_shipped_sample_matches_the_table() -> None:
    shipped = (REPO / "deploy" / "polytropos.toml.sample").read_text()
    assert shipped == settings.sample_text(), (
        "regenerate: dportsv3 config sample > deploy/polytropos.toml.sample"
    )


def test_every_non_secret_setting_appears_in_the_sample() -> None:
    """The 39-settings-nobody-could-set problem, made structurally
    impossible rather than periodically cleaned up."""
    sample = settings.sample_text()
    for setting in settings.SETTINGS:
        if setting.secret:
            continue
        assert f"#{setting.key} = " in sample, setting.path
        assert setting.help.splitlines()[0] in sample, setting.path


def test_no_secret_setting_appears_in_the_sample() -> None:
    """Showing them invites writing a credential into a world-readable
    file, which is the one mistake this layout exists to prevent."""
    sample = settings.sample_text()
    for setting in settings.SETTINGS:
        if setting.secret:
            assert f"#{setting.key} = " not in sample, setting.path


def test_the_sample_is_entirely_commented(tmp_path) -> None:
    """It is installed under its live name, so its contents are what a
    fresh host gets. Uncommented values would also pin today's defaults
    into every install, so an improved default would never reach anyone."""
    import tomllib

    # Section headers are emitted uncommented so the file reads as TOML;
    # what must be absent is every *value*.
    document = tomllib.loads(settings.sample_text())
    leaves = []

    def walk(node, prefix=""):
        for key, value in node.items():
            if isinstance(value, dict):
                walk(value, f"{prefix}{key}.")
            else:
                leaves.append(f"{prefix}{key}")

    walk(document)
    assert leaves == [], f"the sample sets values: {leaves}"


def test_the_sample_documents_every_override() -> None:
    sample = settings.sample_text()
    for setting in settings.SETTINGS:
        if setting.env and not setting.secret:
            assert setting.env in sample, setting.path


# --- secrets ----------------------------------------------------------------

def test_each_secret_is_its_own_file(set_setting) -> None:
    """One value per file: a TOML syntax error cannot take out the whole
    credential set, rotation is a single write, and the mode can follow
    each secret's own reader."""
    named = {s.path: settings.get(s.path)
             for s in settings.SETTINGS if s.secret}
    assert len(set(str(v) for v in named.values())) == len(named), (
        f"two settings name the same file: {named}"
    )
    for value in named.values():
        assert str(value).startswith("secrets/"), value


def test_a_secret_resolves_relative_to_the_config_dir(set_setting, tmp_path) -> None:
    set_setting("llm.patch.model", "x")
    key = tmp_path / "config" / "secrets" / "patch.key"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("sk-from-file\n")
    assert settings.read_secret("llm.patch.api_key_file") == "sk-from-file"


def test_an_absolute_secret_path_is_taken_as_given(set_setting, tmp_path) -> None:
    """So an operator can point at a tmpfs or a secret store's mount."""
    elsewhere = tmp_path / "vault" / "key"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("sk-elsewhere\n")
    set_setting("llm.patch.api_key_file", str(elsewhere))
    assert settings.read_secret("llm.patch.api_key_file") == "sk-elsewhere"


def test_the_legacy_credential_variable_still_works(set_setting, caplog) -> None:
    """Upgrading stops the rc script sourcing harness.env. Without this
    fallback the runner would come back up with no API keys and every job
    would fail — a total outage produced by a tidy-up."""
    import logging

    settings._legacy_warned.clear()
    set_setting("llm.patch.model", "x")
    with caplog.at_level(logging.WARNING, logger=settings.__name__):
        got = settings.read_secret(
            "llm.patch.api_key_file",
            env={"DP_HARNESS_PATCH_API_KEY": "sk-legacy"},
        )
    assert got == "sk-legacy"
    assert any("config migrate" in r.message for r in caplog.records), (
        "a transitional fallback that says nothing never gets removed"
    )


def test_the_file_beats_the_legacy_variable(set_setting, tmp_path) -> None:
    set_setting("llm.patch.model", "x")
    key = tmp_path / "config" / "secrets" / "patch.key"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("sk-from-file\n")
    assert settings.read_secret(
        "llm.patch.api_key_file",
        env={"DP_HARNESS_PATCH_API_KEY": "sk-legacy"},
    ) == "sk-from-file"


# --- migration --------------------------------------------------------------

def test_shell_assignments_are_read_from_both_formats() -> None:
    got = config_cmd.parse_shell_assignments(
        'export DP_HARNESS_TRIAGE_MODEL="a/b"\n'
        '#export DP_HARNESS_PATCH_MODEL="commented"\n'
        ': ${polytropos_tracker_port:="9090"}\n'
        'DP_ACTIVITY_LOG_MAX=99\n'
    )
    assert got["DP_HARNESS_TRIAGE_MODEL"] == "a/b"
    assert got["polytropos_tracker_port"] == "9090"
    assert got["DP_ACTIVITY_LOG_MAX"] == "99"
    assert "DP_HARNESS_PATCH_MODEL" not in got


def test_migration_separates_settings_from_credentials() -> None:
    values, secrets, unmapped = config_cmd.plan_migration({
        "DP_HARNESS_TRIAGE_MODEL": "a/b",
        "DP_HARNESS_TRIAGE_API_KEY": "sk-secret",
        "polytropos_tracker_port": "9090",
        "polytropos_cmd": "/usr/local/bin/dportsv3",
        "DP_HARNESS_MADE_UP": "x",
    })
    assert values == {"llm.triage.model": "a/b", "tracker.port": "9090"}
    assert secrets == {"llm.triage.api_key_file": "sk-secret"}
    assert unmapped == ["DP_HARNESS_MADE_UP"], (
        "anything not carried over has to be named, not dropped silently"
    )
    assert "polytropos_cmd" not in unmapped, (
        "rc's own knobs stay in the .conf; that is not a migration gap"
    )


def test_migration_skips_the_placeholder_credential() -> None:
    """The shipped sample said replace-me. Carrying that into a secret
    file would look like a configured key that fails at the first call."""
    _values, secrets, _unmapped = config_cmd.plan_migration(
        {"DP_HARNESS_PATCH_API_KEY": "replace-me"})
    assert secrets == {}


def test_every_migratable_name_maps_to_a_real_setting() -> None:
    known = {s.path for s in settings.SETTINGS}
    for name, path in {**config_cmd.ENV_TO_SETTING,
                       **config_cmd.CONF_TO_SETTING,
                       **config_cmd.KEY_TO_SECRET}.items():
        assert path in known, f"{name} maps to {path}, which is not a setting"


# --- what the operator can see ----------------------------------------------

def test_config_show_reports_provenance(set_setting, capsys) -> None:
    """"It is 3" does not help someone whose file says 5. "3, from the
    default" tells them the file is not being read."""
    from argparse import Namespace

    set_setting("runner.max_patch_attempts", 7)
    config_cmd.cmd_config(Namespace(config_action="show", changed=True))
    out = capsys.readouterr().out
    assert "runner.max_patch_attempts" in out
    assert "7" in out and "file" in out


def test_config_show_never_prints_a_credential(set_setting, capsys, tmp_path) -> None:
    from argparse import Namespace

    set_setting("llm.patch.model", "x")
    key = tmp_path / "config" / "secrets" / "patch.key"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("sk-do-not-print\n")

    config_cmd.cmd_config(Namespace(config_action="show", changed=False))
    out = capsys.readouterr().out
    assert "sk-do-not-print" not in out
    assert "present" in out, "it should still say whether one was found"


def test_config_get_prints_a_bare_value(set_setting, capsys) -> None:
    """The rc prestart captures this, so no label and no quoting."""
    from argparse import Namespace

    set_setting("paths.queue_root", "/somewhere/queue")
    config_cmd.cmd_config(Namespace(config_action="get",
                                    setting="paths.queue_root"))
    assert capsys.readouterr().out.strip() == "/somewhere/queue"


def test_config_check_reports_a_misspelled_key(set_setting, capsys) -> None:
    from argparse import Namespace

    path = set_setting("runner.max_patch_attempts", 3)
    # A plausible typo, in an otherwise valid file.
    path.write_text(path.read_text().replace(
        "max_patch_attempts = 3", "max_patch_attempts = 3\nmax_patch_attemps = 4"))
    settings.reset()
    code = config_cmd.cmd_config(Namespace(config_action="check"))
    err = capsys.readouterr().err
    assert code == 1
    assert "max_patch_attemps" in err


# --- policy came along too --------------------------------------------------

def test_the_policy_tables_are_settings(set_setting) -> None:
    from dportsv3.agent import policy

    resolved = policy.load_policy(None)
    assert resolved.tiers["ASSIST"].max_tokens == 120000
    assert resolved.classification_to_tier["fetch-checksum"] == "AUTO"
    assert resolved.confidence_floor == {"AUTO": "high", "ASSIST": "medium"}


def test_the_settings_policy_matches_the_json_it_replaced() -> None:
    """The tables moved format; they must not have moved value."""
    import json

    from dportsv3.agent import policy

    shipped = json.loads(
        (paths.BUNDLED_CONFIG_DIR / "agentic-policy.json.sample").read_text())
    from_json = policy._from_tables(
        shipped["tiers"], shipped["classification_to_tier"],
        shipped["confidence_floor"])
    from_settings = policy.load_policy(None)
    assert from_settings == from_json


def test_an_explicit_policy_file_still_wins(set_setting, tmp_path) -> None:
    import json

    from dportsv3.agent import policy

    other = tmp_path / "other-policy.json"
    other.write_text(json.dumps({
        "tiers": {"AUTO": {"max_iterations": 99, "max_tokens": 1}},
        "classification_to_tier": {"x": "AUTO"},
        "confidence_floor": {},
    }))
    assert policy.load_policy(str(other)).tiers["AUTO"].max_iterations == 99
