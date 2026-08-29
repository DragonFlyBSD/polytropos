"""Every setting this tool has, declared once.

Configuring an install used to mean editing six files in five formats and
exporting environment variables that in many cases had nowhere to be set:
a shell ``polytropos.conf`` for rc, two shell ``.env`` files that existed
only so ``export`` could carry values into Python, a TOML for delivery, a
JSON for policy, and a raw file for the token. Underneath sat 67
environment variables, 39 of which no sample or script mentioned at all.

The cause was structural rather than careless. ``os.environ`` was the
only channel any of these settings had into the code, so a shell file
that exports was the only transport available, and adding a variable cost
one line while documenting one was a separate act of will that nothing
enforced.

This table is the fix. Everything else is derived from it: the value a
caller gets, the generated sample, ``config show``'s provenance column,
and the warning about a key nobody claims. A setting that is not here
cannot be read, so "read by the code, settable nowhere" stops being
possible rather than being periodically cleaned up.

**Environment variables are opt-in, per setting.** A blanket
"env overrides file" layer would have been shorter to write and would
have rebuilt the same problem under a new name. The test applied to every
candidate was: *is this value knowable when the file is written?* If yes,
it lives in the file and nowhere else. The survivors are a per-run debug
switch, two temporary knobs tied to a specific dependency version, an
escape hatch, a credential a secret store may inject, and a root that
already has a command-line flag. Six, against sixty-seven.

Three kinds of environment variable are deliberately *not* here:

* ``$DPORTSV3_CONFIG_DIR`` — it names this file, so it cannot live in it.
* Inter-process arguments spelled as environment: ``$DPORTSV3_CMD``,
  ``$DPORTS_DEV_ENV_CMD``, ``$DPORTS_DEV_TOOL_ROOT``,
  ``$DPORTSV3_NO_BOOTSTRAP``, ``$DPORTSV3_TRACKER_TARGET``. A parent
  tells a child something about *this* exec. They would be better as
  command-line flags, but they are not configuration.
* ``$DP_TEST_*``, which the test suite owns.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from dports_dev_env.confschema import (  # noqa: F401 — re-exported
    ConfigError,
    Resolved,
    Schema,
    Setting,
    render_sample,
)

from dportsv3 import paths
# The tracker URL has exactly one default, and endpoints.py owns it:
# that module is the stdlib-only one a chroot can import, so it
# cannot read this table. Importing the constant keeps the two in
# step without a second literal.
from dportsv3.common.endpoints import DEFAULT_TRACKER_URL


_LOG = logging.getLogger(__name__)

#: The operator's file, inside ``$DPORTSV3_CONFIG_DIR``.
CONFIG_FILENAME = "polytropos.toml"

#: HOME for the service account, and the root the installer creates. The
#: installer owns creating it; the settings table owns it because
#: delivery.clone_dir defaults to a directory inside it and the two must
#: not drift into separate copies of the same path.
SERVICE_HOME = Path("/var/db/polytropos")

#: Where secret files live, relative to the config dir. One value per
#: file: a TOML syntax error cannot then take out the whole credential
#: set, rotation is a single write, and the mode can follow each
#: secret's own reader.
SECRETS_SUBDIR = "secrets"

#: ``secret setting -> (mode, readable by the service group)``.
#:
#: The mode follows the READER, which is the whole argument for keeping
#: credentials in files rather than in the settings. The queue runner is
#: root and reads the triage and patch keys; the tracker drops to the
#: unprivileged account and reads the chat key and the delivery token. A
#: blanket 0640 would hand the expensive patch key to the tracker, which
#: has no authentication and an API that can spend LLM credit — the
#: exact thing the split exists to prevent.
SECRET_MODES: dict[str, tuple[int, bool]] = {
    "llm.triage.api_key_file": (0o600, False),
    "llm.patch.api_key_file": (0o600, False),
    "llm.chat.api_key_file": (0o640, True),
    "delivery.token_file": (0o640, True),
}


SETTINGS: list[Setting] = [
    # ---------------------------------------------------------------- paths
    Setting(
        "paths.logs_root", "path", Path("/build/synth/logs"),
        "The evidence tree. The three paths below default to positions\n"
        "inside it, so on a stock layout this is the only one to set.",
    ),
    Setting(
        "paths.state_db", "path", Path("/build/synth/logs/evidence/state.db"),
        "The tracker's database. Both services write it, which is why the\n"
        "evidence tree belongs to the service account.",
    ),
    Setting(
        "paths.artifact_root", "path", Path("/build/synth/logs/evidence"),
        "Where bundles and their artifacts are stored.",
    ),
    Setting(
        "paths.queue_root", "path", Path("/build/synth/logs/evidence/queue"),
        "Where the dsynth hooks drop .job files for the runner to claim.",
    ),
    Setting(
        "paths.delta_root", "path", None,
        "The DeltaPorts checkout. Empty means 'use the current directory',\n"
        "which is checked for a ports/ or special/ subdirectory before use.",
        env="DPORTS_DELTA_ROOT",
    ),

    # -------------------------------------------------------------- tracker
    Setting(
        "tracker.bind", "str", "0.0.0.0",
        "Listen address. The tracker has no authentication and its API can\n"
        "start builds and spend LLM credit; 0.0.0.0 assumes a trusted\n"
        "network. Use 127.0.0.1 and an ssh tunnel if that is not your\n"
        "situation. A --bind flag overrides this.",
    ),
    Setting("tracker.port", "int", 8080,
            "Listen port. A --port flag overrides this."),
    Setting(
        "tracker.url", "str", DEFAULT_TRACKER_URL,
        "How the runner reaches the tracker. Keep in step with the two\n"
        "settings above.\n"
        "\n"
        "This one keeps an environment override because it has to cross\n"
        "into a chroot: artifact_store_client runs from the dsynth hooks\n"
        "inside a build environment, where dportsv3 is not importable and\n"
        "common/endpoints.py is deliberately stdlib-only.",
        env="DPORTSV3_TRACKER_URL",
    ),

    # --------------------------------------------------------------- runner
    Setting(
        "runner.dev_env", "str", "",
        "Default dev-env for jobs that do not name one. Empty is right on a\n"
        "single-env host: the runner auto-picks when exactly one exists.\n"
        "With several and nothing set it holds, rather than running with the\n"
        "dsynth-busy gate off.",
    ),
    Setting("runner.max_patch_attempts", "int", 3,
            "How many patch attempts one signature gets inside the window."),
    Setting("runner.attempt_window_hours", "int", 2,
            "The window the attempt cap is counted over."),
    Setting("runner.max_snippet_rounds", "int", 5,
            "How many rounds of log-snippet requests triage may make."),
    Setting(
        "runner.bundle_backstop", "int", 10,
        "Hard ceiling on attempts per bundle, independent of the window.\n"
        "The last line of defence against a loop that keeps resetting the\n"
        "signature.",
    ),
    Setting(
        "runner.signature_stickiness", "int", 3,
        "Identical consecutive failures before the runner stops treating a\n"
        "signature as worth another attempt.",
    ),
    Setting("runner.health_cache_seconds", "int", 60,
            "How long a dev-env health probe is trusted before re-running."),
    Setting(
        "runner.min_attempt_budget_fraction", "float", 0.25,
        "A retry needs this fraction of the budget still available to be\n"
        "worth starting. Clamped to 0.0-1.0.",
    ),
    Setting("runner.activity_log_max", "int", 5000,
            "Rows kept in the activity log. The floor is 50."),
    Setting(
        "runner.stale_queued_max_age_seconds", "int", 3600,
        "How old a QUEUED row whose .job file has vanished must be\n"
        "before it is reaped. Catches a row recording a path this runner\n"
        "never scans, which otherwise blocks every later job for that\n"
        "origin indefinitely.",
    ),
    Setting(
        "runner.dump_session", "bool", False,
        "Write the full LLM session for each attempt as a bundle artifact.\n"
        "Large, and the single most useful thing to have when an attempt\n"
        "goes wrong.",
        env="DP_HARNESS_DUMP_SESSION",
    ),
    Setting("runner.dump_session_cap", "int", 16 * 1024,
            "Bytes kept per tool result in a session dump. The floor is 1024."),
    Setting(
        "runner.context_file_cap", "int", 32768,
        "Characters of any one file inlined into a prompt before it is\n"
        "truncated head-and-tail. Unbounded inlining produced 250K-token\n"
        "prompts where the classifier needed a log and a snippet; the\n"
        "patch agent has get_file for the rest. The floor is 2048, below\n"
        "which the head+tail split has no room around the marker.",
    ),

    Setting(
        "runner.confirm_green_threshold", "int", 2,
        "Consecutive green confirm builds before an issue resolves. A\n"
        "single green can come from an unrelated transient, so one is not\n"
        "enough to close an issue. The floor is 1.",
    ),
    Setting(
        "runner.confirm_max_failures", "int", 3,
        "Confirm builds that produce NO verdict — dev-env gone, nothing\n"
        "to replay — before the issue goes back to a human with a\n"
        "handoff explaining why the build never ran. The floor is 1.",
    ),
    Setting(
        "runner.confirm_backoff_seconds", "int", 60,
        "Wait after the first verdictless confirm attempt, doubling each\n"
        "time. The floor is 1.",
    ),
    Setting(
        "runner.confirm_backoff_max_seconds", "int", 3600,
        "Ceiling on that wait, so a passing outage is not mistaken for a\n"
        "permanent one. The floor is 1.",
    ),

    # ------------------------------------------------------------------ llm
    Setting(
        "llm.backend", "str", "auto",
        "'auto' uses the openai SDK for providers that speak the OpenAI\n"
        "wire format and litellm otherwise. 'litellm' forces the old path.\n"
        "TEMPORARY escape hatch — see poly-r1g.",
        env="DP_HARNESS_LLM_BACKEND",
    ),
    Setting("llm.triage.model", "str", "",
            "Triage is called on every failure, so it wants to be cheap."),
    Setting("llm.triage.api_key_file", "path",
            Path("secrets/triage.key"),
            "File holding the API key, relative to the config directory.\n"
            "Mode 0600 root: the queue runner reads it and runs as root.",
            secret=True),
    Setting("llm.triage.api_base", "str", "",
            "Custom endpoint. Empty uses the provider's own."),
    Setting("llm.triage.provider", "str", "",
            "Force a provider code path. Empty infers it from the model."),
    Setting("llm.triage.timeout", "int", 120, "Seconds per request."),
    Setting(
        "llm.triage.reasoning", "str", "none",
        "none | low | high | max. Triage classifies against a fixed schema\n"
        "and does not need to think. TEMPORARY — see poly-r1g.",
        env="DP_HARNESS_TRIAGE_REASONING",
    ),
    Setting("llm.patch.model", "str", "",
            "The expensive one. Empty falls back to the triage model."),
    Setting("llm.patch.api_key_file", "path", Path("secrets/patch.key"),
            "File holding the API key. Mode 0600 root, as above.\n"
            "Missing falls back to the triage key.",
            secret=True),
    Setting("llm.patch.api_base", "str", "",
            "Empty falls back to the triage endpoint."),
    Setting("llm.patch.provider", "str", "",
            "Empty falls back to the triage provider."),
    Setting("llm.patch.timeout", "int", 600, "Seconds per request."),
    Setting(
        "llm.patch.reasoning", "str", "low",
        "low rather than off, so a quality regression is a smaller step to\n"
        "walk back. TEMPORARY — see poly-r1g.",
        env="DP_HARNESS_PATCH_REASONING",
    ),
    Setting(
        "llm.chat.model", "str", "",
        "The tracker's fix-chat panel. This is the feature gate: leave it\n"
        "empty and the endpoint answers 503 and the panel stays hidden.",
    ),
    Setting(
        "llm.chat.api_key_file", "path", Path("secrets/chat.key"),
        "File holding the API key. Mode 0640 root:<service group> — the\n"
        "TRACKER reads this one, and it does not run as root.",
        secret=True,
    ),
    Setting("llm.chat.api_base", "str", "", "Custom endpoint."),
    Setting("llm.chat.provider", "str", "", "Force a provider code path."),
    Setting("llm.chat.timeout", "int", 120, "Seconds per request."),
    Setting(
        "llm.chat.context_cap", "int", 96 * 1024,
        "Bound on the assembled artifact and transcript context. The\n"
        "default suits a 128K-context model. The floor is 8192.",
    ),

    # --------------------------------------------------------------- policy
    Setting(
        "policy.file", "path", None,
        "An agentic-policy.json to use instead of the [policy] tables\n"
        "below. Empty uses them. Mostly a way to try an alternative policy\n"
        "for one run without editing this file.",
        env="DP_HARNESS_POLICY",
    ),
    Setting(
        "policy.tiers", "table",
        {
            "AUTO": {"max_iterations": 2, "max_tokens": 30000},
            "ASSIST": {"max_iterations": 4, "max_tokens": 120000},
            "MANUAL": {},
            "CONVERT": {"max_iterations": 2, "max_tokens": 150000},
        },
        "Iteration and token budget per tier. MANUAL is empty: it never\n"
        "runs the agent.",
    ),
    Setting(
        "policy.classification_to_tier", "table",
        {
            "plist-error": "ASSIST",
            "fetch-checksum": "AUTO",
            "pkg-format": "ASSIST",
            "compile-error": "ASSIST",
            "patch-error": "ASSIST",
            "link-error": "ASSIST",
            "configure-error": "ASSIST",
            "missing-dep": "MANUAL",
            "fetch-error": "MANUAL",
            "runtime-error": "MANUAL",
            "dependency-conflict": "MANUAL",
            "unknown": "MANUAL",
        },
        "Which tier each triage classification lands in.",
    ),
    Setting(
        "policy.confidence_floor", "table",
        {"AUTO": "high", "ASSIST": "medium"},
        "Minimum triage confidence for a tier to run at all.",
    ),

    # ------------------------------------------------------------- delivery
    Setting(
        "delivery.type", "str", "",
        "github | gitlab | gitea | local-patch. EMPTY MEANS DELIVERY IS\n"
        "OFF, and that is the shipped default: Accept stays a pure\n"
        "tracker-side action and logs skip_reason=no_config.\n"
        "\n"
        "Start with local-patch. It writes the diff to delivery.outbox\n"
        "instead of pushing, which proves the whole Accept path with no\n"
        "credentials and no network.",
    ),
    Setting("delivery.repo", "str", "",
            "'owner/name'. Required for everything but local-patch."),
    Setting(
        "delivery.clone_dir", "path", SERVICE_HOME / "DeltaPorts",
        "The local DeltaPorts checkout the tracker pushes from. Must be a\n"
        "clean git working tree on base_branch, writable by the account\n"
        "running the TRACKER, with an origin it may push to. Checked at\n"
        "startup rather than at the first Accept.\n"
        "\n"
        "The default sits in the service account's home because that is\n"
        "the one directory the installer already creates and already\n"
        "chowns to that account — which is this setting's hard\n"
        "requirement, since delivery resets and commits in the tree and\n"
        "the tracker is not root. Nothing clones it for you.",
    ),
    Setting("delivery.outbox", "path", None,
            "Where local-patch writes diffs. Required for that type only."),
    Setting("delivery.base_branch", "str", "master",
            "The branch the pull request targets upstream."),
    Setting("delivery.draft", "bool", True,
            "Open as a draft, so a human un-drafts after review."),
    Setting(
        "delivery.labels", "list", ["agentic-fix", "needs-review"],
        "Applied best-effort once the request exists. A label failure never\n"
        "fails the delivery.",
    ),
    Setting(
        "delivery.branch_template", "str",
        "agentic/{origin_safe}-{target_safe}-{signature_short}",
        "Placeholders: {origin}, {origin_safe}, {target}, {target_safe},\n"
        "{bundle_id}, {bundle_short}, {signature_short}. The default keys\n"
        "on the error signature, so retries against the same root cause\n"
        "converge on one branch and one request.",
    ),
    Setting("delivery.committer_name", "str", "Fred [bot]",
            "Applied per invocation with `git -c`, never written to the\n"
            "clone's git config."),
    Setting("delivery.committer_email", "str", "github@dragonflybsd.org",
            "Also used for the Signed-off-by trailer."),
    Setting(
        "delivery.token_file", "path", Path("secrets/delivery.token"),
        "File holding the forge credential, relative to the config\n"
        "directory. Mode 0640 root:<service group>: the TRACKER delivers\n"
        "and does not run as root, so a 0400 root token is unreadable by\n"
        "the only process that wants it. local-patch needs no token.",
        env="DPORTSV3_DELIVERY_TOKEN", secret=True,
    ),
    Setting(
        "delivery.target", "table", {},
        "Per-target overrides, keyed by the bundle's target exactly,\n"
        "leading '@' included. Any field omitted falls back to the\n"
        "settings above:\n"
        "\n"
        "    [delivery.target.\"@2026Q2\"]\n"
        "    base_branch = \"2026Q2\"\n"
        "    repo = \"owner/ports-2026Q2\"",
    ),
    Setting(
        "delivery.git_timeout", "float", 60.0,
        "Seconds before a git subprocess is abandoned. A hung remote would\n"
        "otherwise block the Accept request thread indefinitely.",
    ),
]


SECTION_HELP = {
    "paths": "Where things live. Everything else is derived from logs_root.",
    "tracker": "The web UI and read API.",
    "runner": "The agent queue runner: how hard it tries, and how often.",
    "llm": "Model configuration, per role. Keys are NOT here — each role\n"
           "names a file, so the mode can follow the reader.",
    "llm.triage": "Called on every failure. Cheap and fast.",
    "llm.patch": "The expensive one. Unset fields fall back to triage.",
    "llm.chat": "The tracker's fix-chat panel. Optional.",
    "policy": "Which triage result earns which tier and budget.",
    "delivery": "What Accept does with a fix. Off unless delivery.type is set.",
}

SAMPLE_HEADER = """\
# Polytropos — the one settings file.
#
# Installed as <prefix>/etc/polytropos/polytropos.toml, which both
# services read as $DPORTSV3_CONFIG_DIR. Every line below is commented
# out and shows the built-in default: uncomment only what you mean to
# change, so this file records your decisions rather than restating every
# default, and an improved default reaches you instead of being pinned by
# a copy of its old value.
#
# This file is world-readable. Credentials are NOT in it — each one lives
# in its own file under secrets/, named by a *_file setting, so the mode
# can follow whichever service reads it:
#
#   secrets/triage.key       0600 root                (the runner is root)
#   secrets/patch.key        0600 root
#   secrets/chat.key         0640 root:<service group> (the tracker is not)
#   secrets/delivery.token   0640 root:<service group>
#
# `dportsv3 config show` prints every resolved value and where it came
# from. `dportsv3 config check` validates this file without starting
# anything.
#
# Generated from dportsv3/settings.py. Do not hand-edit the sample; edit
# the table and regenerate, so the two cannot drift.\
"""


# --------------------------------------------------------------------------
# The process-wide schema
# --------------------------------------------------------------------------

_schema: Schema | None = None
_schema_path: Path | None = None


def schema() -> Schema:
    """The loaded schema, reading the operator's file on first use.

    Loaded once rather than per call. Nothing is lost by that: the
    previous arrangement re-read ``os.environ`` at each call site, but a
    service's environment is fixed at exec, so a value could never change
    under a running process anyway.
    """
    global _schema, _schema_path
    current = config_path()
    # Reload when the config directory moves under us. In a service that
    # never happens — the environment is fixed at exec — but a test that
    # points $DPORTSV3_CONFIG_DIR at its own tmp_path would otherwise get
    # whichever file the previous test loaded, which is the kind of
    # cross-test bleed that is very hard to read back from a failure.
    if _schema is None or _schema_path != current:
        _schema = Schema(SETTINGS, name="polytropos")
        _schema.load(current)
        _schema_path = current
        _warn_about_unknown_keys(_schema)
    return _schema


def reset() -> None:
    """Drop the loaded schema so the next read re-reads the file.

    For tests, and for ``config`` subcommands that point at a file other
    than the live one.
    """
    global _schema, _schema_path
    _schema = None
    _schema_path = None


def config_path() -> Path | None:
    """The operator's settings file, or None when there is no config dir.

    Deliberately not ``paths.config_file``: that falls back to a bundled
    ``.sample``, which is right for a policy file that ships a usable
    default and wrong here. Every setting already has a default in the
    table, so a missing file means stock behaviour, and loading a sample
    on the operator's behalf would only make the source of a value harder
    to explain.
    """
    directory = paths.config_dir()
    return None if directory is None else directory / CONFIG_FILENAME


def _warn_about_unknown_keys(sch: Schema) -> None:
    """A misspelled key is otherwise perfectly silent — the file parses,
    the setting keeps its default, and nothing says why the edit had no
    effect."""
    unknown = sch.unknown_keys(claimed={"dev_env"})
    if not unknown:
        return
    _LOG.warning(
        "%s: %d setting(s) nothing reads: %s. Check the spelling against "
        "`dportsv3 config show`.",
        sch.file or CONFIG_FILENAME, len(unknown), ", ".join(unknown),
    )


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def get(path: str, *, env: dict[str, str] | None = None) -> Any:
    """One setting's value."""
    return schema().get(path, env=env)


def resolve(path: str, *, env: dict[str, str] | None = None) -> Resolved:
    """One setting's value and where it came from."""
    return schema().resolve(path, env=env)


def get_str(path: str) -> str:
    """A string setting, with empty normalised away.

    Most string settings here mean "unset" by being empty — an api_base
    nobody overrode, a model that falls back to another role's. Returning
    ``""`` and ``None`` from different call sites for the same idea is
    how the previous code grew three spellings of the same check.
    """
    value = str(get(path) or "").strip()
    return value


def get_opt(path: str) -> str | None:
    """A string setting, or None when it is empty."""
    return get_str(path) or None


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------

#: Environment variables that used to hold credentials directly, still
#: honoured so an install that has not run ``config migrate`` keeps
#: working. Each logs once when used. Remove once no deployment sources a
#: ``.env`` file — see poly-cu8.
LEGACY_KEY_ENV = {
    "llm.triage.api_key_file": "DP_HARNESS_TRIAGE_API_KEY",
    "llm.patch.api_key_file": "DP_HARNESS_PATCH_API_KEY",
    "llm.chat.api_key_file": "DP_HARNESS_CHAT_API_KEY",
}

_legacy_warned: set[str] = set()


def secret_path(path: str) -> Path | None:
    """Absolute path of the file a ``*_file`` setting names.

    Relative values resolve against the config directory, so the shipped
    default ``secrets/patch.key`` needs no absolute path in the sample
    and an operator can still name one somewhere else entirely — a
    tmpfs, a secret store's mount point.
    """
    raw = get(path)
    if raw is None or str(raw) == "" or str(raw) == ".":
        return None
    p = Path(raw)
    if p.is_absolute():
        return p
    directory = paths.config_dir()
    return None if directory is None else directory / p


def read_secret(path: str, *, env: dict[str, str] | None = None) -> str | None:
    """The credential a ``*_file`` setting names, or None.

    Order: the setting's own env override if it declares one, then the
    file, then the legacy environment variable that used to carry it.

    That last tier is transitional and says so when it fires. Without it,
    upgrading a host would take the runner's API keys away the moment the
    rc script stopped sourcing ``harness.env`` — a silent, total failure
    of every job, for a change that is supposed to be a tidy-up.
    """
    setting = schema().settings
    declared = next((s for s in setting if s.path == path), None)
    if declared is None:
        raise ConfigError(f"no such setting {path!r}")
    environ = os.environ if env is None else env

    if declared.env:
        direct = (environ.get(declared.env) or "").strip()
        if direct:
            return direct

    resolved = secret_path(path)
    if resolved is not None and resolved.is_file():
        try:
            text = resolved.read_text().strip()
        except OSError as exc:
            raise ConfigError(f"{resolved}: cannot be read: {exc}") from exc
        if text:
            return text

    legacy = LEGACY_KEY_ENV.get(path)
    if legacy:
        value = (environ.get(legacy) or "").strip()
        if value:
            if legacy not in _legacy_warned:
                _legacy_warned.add(legacy)
                _LOG.warning(
                    "$%s still carries a credential. Move it into %s and "
                    "run `dportsv3 config migrate`; this fallback goes away "
                    "once no deployment sources a .env file.",
                    legacy, resolved or path,
                )
            return value
    return None


def sample_text() -> str:
    """The commented sample, rendered from the table."""
    return render_sample(
        Schema(SETTINGS, name="polytropos"),
        header=SAMPLE_HEADER,
        section_help=SECTION_HELP,
        # Secrets name a file whose default is already right; showing them
        # invites an operator to write a credential into a world-readable
        # file, which is the one mistake this layout exists to prevent.
        include=lambda s: not s.secret,
    )
