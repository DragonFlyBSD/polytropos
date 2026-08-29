"""``dportsv3 config`` — see, check and migrate the settings.

Three questions an operator has about configuration, and none of them
used to have an answer without reading source:

* what is this actually set to, and where did that come from?
  ``config show``
* did my edit take, or did I misspell a key?
  ``config check``
* I have the old ``.conf`` and ``.env`` files — now what?
  ``config migrate``

``config get`` is the fourth, and it is for scripts rather than people:
the rc.d prestart needs a path or two before it can create directories,
and asking the tool is better than keeping a second copy of the value in
a shell file where the two can disagree.
"""

from __future__ import annotations

import os
import re
import sys
from argparse import Namespace
from pathlib import Path

from dportsv3 import paths, settings


def cmd_config(args: Namespace) -> int:
    action = getattr(args, "config_action", None)
    if action == "show":
        return _show(args)
    if action == "get":
        return _get(args)
    if action == "check":
        return _check(args)
    if action == "migrate":
        return _migrate(args)
    if action == "sample":
        print(settings.sample_text(), end="")
        return 0
    print("usage: dportsv3 config {show|get|check|migrate|sample}",
          file=sys.stderr)
    return 2


# --------------------------------------------------------------------------
# show / get
# --------------------------------------------------------------------------

def _render(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return f"<{len(value)} entries>"
    return str(value)


def _show(args: Namespace) -> int:
    """Every setting, its value, and where the value came from.

    The source column is the point. "It is set to 3" does not help
    someone whose file says 5; "it is 3, from the default" tells them the
    file is not being read, and "3, from $DP_HARNESS_..." tells them
    something in their environment is winning.
    """
    sch = settings.schema()
    where = sch.file or settings.config_path()
    print(f"# settings file: {where or '(none — $DPORTSV3_CONFIG_DIR is unset)'}")
    if where is not None and not Path(where).is_file():
        print("# that file does not exist; every value below is a default")
    print()

    rows = []
    for item in sch.all_resolved():
        if item.setting.secret:
            resolved = settings.secret_path(item.setting.path)
            shown = str(resolved) if resolved else _render(item.value)
            # Never the credential itself, only whether one was found.
            state = "present" if settings.read_secret(item.setting.path) else "absent"
            rows.append((item.setting.path, f"{shown} ({state})", item.source))
            continue
        rows.append((item.setting.path, _render(item.value), item.source))

    if getattr(args, "changed", False):
        rows = [r for r in rows if r[2] != "default"]
        if not rows:
            print("every setting is at its default")
            return 0

    width = max((len(r[0]) for r in rows), default=0)
    vwidth = min(48, max((len(r[1]) for r in rows), default=0))
    for path, value, source in rows:
        print(f"{path:<{width}}  {value:<{vwidth}}  {source}")
    return 0


def _get(args: Namespace) -> int:
    """One value, bare, for a shell to capture.

    No label, no quoting, no trailing commentary — this exists so
    ``polytropos_queue_root=$(dportsv3 config get paths.queue_root)``
    works, which is what lets the rc.d prestart create the queue
    directories without a second copy of the path in polytropos.conf.
    """
    try:
        print(_render(settings.get(args.setting)))
    except settings.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------

def _check(args: Namespace) -> int:
    """Validate the file without starting a service.

    Every value is resolved, so a bad type is reported here rather than
    at three in the morning when the runner claims its first job.
    """
    directory = paths.config_dir()
    if directory is None:
        print("error: $DPORTSV3_CONFIG_DIR is unset, so no settings file "
              "can be found. The services set it from polytropos_config_dir.",
              file=sys.stderr)
        return 1

    path = settings.config_path()
    problems = 0
    try:
        sch = settings.schema()
    except settings.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if path is not None and not Path(path).is_file():
        print(f"note: {path} does not exist; all settings are at their "
              f"defaults. `dportsv3 config sample` prints a starting point.")

    for item in sch.settings:
        try:
            sch.resolve(item.path)
        except settings.ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            problems += 1

    for key in sch.unknown_keys(claimed={"dev_env"}):
        print(f"warning: {key} is not a setting anything reads "
              f"(check the spelling)", file=sys.stderr)
        problems += 1

    for item in sch.settings:
        if not item.secret:
            continue
        target = settings.secret_path(item.path)
        if target is None or not target.is_file():
            continue
        mode = target.stat().st_mode & 0o777
        want, grouped = settings.SECRET_MODES.get(item.path, (0o600, False))
        if mode & 0o007:
            print(f"warning: {target} is world-readable (mode {mode:04o}); "
                  f"it holds a credential", file=sys.stderr)
            problems += 1
        elif mode & 0o070 and not grouped:
            # The reader is the root runner, so group access buys nothing
            # and hands the key to the unprivileged tracker, which has no
            # authentication and an API that can spend LLM credit.
            print(f"warning: {target} is group-readable (mode {mode:04o}) "
                  f"but only the root queue runner reads it; "
                  f"{oct(want)[2:]} is enough", file=sys.stderr)
            problems += 1

    if problems:
        print(f"\n{problems} problem(s) found", file=sys.stderr)
        return 1
    print("settings are valid")
    return 0


# --------------------------------------------------------------------------
# migrate
# --------------------------------------------------------------------------

#: ``old shell variable -> new setting path``. The shell files carried
#: exports; the .conf carried ``: ${name:="value"}``. Both are read.
ENV_TO_SETTING = {
    "DP_HARNESS_TRIAGE_MODEL": "llm.triage.model",
    "DP_HARNESS_TRIAGE_API_BASE": "llm.triage.api_base",
    "DP_HARNESS_TRIAGE_PROVIDER": "llm.triage.provider",
    "DP_HARNESS_TRIAGE_REASONING": "llm.triage.reasoning",
    "DP_HARNESS_TIMEOUT": "llm.triage.timeout",
    "DP_HARNESS_PATCH_MODEL": "llm.patch.model",
    "DP_HARNESS_PATCH_API_BASE": "llm.patch.api_base",
    "DP_HARNESS_PATCH_PROVIDER": "llm.patch.provider",
    "DP_HARNESS_PATCH_REASONING": "llm.patch.reasoning",
    "DP_HARNESS_PATCH_TIMEOUT": "llm.patch.timeout",
    "DP_HARNESS_CHAT_MODEL": "llm.chat.model",
    "DP_HARNESS_CHAT_API_BASE": "llm.chat.api_base",
    "DP_HARNESS_CHAT_PROVIDER": "llm.chat.provider",
    "DP_HARNESS_CHAT_TIMEOUT": "llm.chat.timeout",
    "DP_HARNESS_CHAT_CONTEXT_CAP": "llm.chat.context_cap",
    "DP_HARNESS_LLM_BACKEND": "llm.backend",
    "DP_HARNESS_MAX_PATCH_ATTEMPTS": "runner.max_patch_attempts",
    "DP_HARNESS_ATTEMPT_WINDOW_HOURS": "runner.attempt_window_hours",
    "DP_HARNESS_MAX_SNIPPET_ROUNDS": "runner.max_snippet_rounds",
    "DP_HARNESS_BUNDLE_BACKSTOP": "runner.bundle_backstop",
    "DP_HARNESS_SIGNATURE_STICKINESS": "runner.signature_stickiness",
    "DP_HARNESS_HEALTH_CACHE_SECONDS": "runner.health_cache_seconds",
    "DP_HARNESS_MIN_ATTEMPT_BUDGET_FRACTION": "runner.min_attempt_budget_fraction",
    "DP_HARNESS_DUMP_SESSION": "runner.dump_session",
    "DP_HARNESS_DUMP_SESSION_CAP": "runner.dump_session_cap",
    "DP_HARNESS_CONTEXT_FILE_CAP": "runner.context_file_cap",
    "DP_ACTIVITY_LOG_MAX": "runner.activity_log_max",
    "DPORTSV3_GIT_TIMEOUT": "delivery.git_timeout",
}

#: ``polytropos.conf`` shell variable -> setting. Only the ones that are
#: really Python's; the rc-only knobs (``polytropos_cmd``,
#: ``polytropos_user`` and friends) stay in the .conf, because rc needs
#: them before any Python runs.
CONF_TO_SETTING = {
    "polytropos_logs_root": "paths.logs_root",
    "polytropos_state_db": "paths.state_db",
    "polytropos_artifact_root": "paths.artifact_root",
    "polytropos_queue_root": "paths.queue_root",
    "polytropos_tracker_bind": "tracker.bind",
    "polytropos_tracker_port": "tracker.port",
    "polytropos_tracker_url": "tracker.url",
    "polytropos_runner_dev_env": "runner.dev_env",
}

#: ``old credential variable -> the setting naming its file``.
KEY_TO_SECRET = {
    "DP_HARNESS_TRIAGE_API_KEY": "llm.triage.api_key_file",
    "DP_HARNESS_PATCH_API_KEY": "llm.patch.api_key_file",
    "DP_HARNESS_CHAT_API_KEY": "llm.chat.api_key_file",
}

_EXPORT = re.compile(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=(.*)$')
_CONF = re.compile(r'^\s*:\s*\$\{([a-z_]+):?=(.*)\}\s*$')


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def parse_shell_assignments(text: str) -> dict[str, str]:
    """Values from a ``.env`` (``export X=y``) or a ``.conf``
    (``: ${x:="y"}``).

    Deliberately not a shell parser: these files are generated from our
    own samples and hold literal assignments. A line this does not
    recognise is reported rather than guessed at, because quietly
    dropping a setting during a migration is the one thing that must not
    happen.
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _CONF.match(line)
        if m:
            found[m.group(1)] = _unquote(m.group(2))
            continue
        m = _EXPORT.match(line)
        if m:
            found[m.group(1)] = _unquote(m.group(2))
    return found


def plan_migration(legacy: dict[str, str]) -> tuple[dict[str, object],
                                                    dict[str, str],
                                                    list[str]]:
    """Split old values into settings, secrets, and things left behind.

    Returns ``(settings, secrets, unmapped)`` where ``settings`` maps a
    dotted setting path to its value, ``secrets`` maps a ``*_file``
    setting to the credential that has to be written there, and
    ``unmapped`` names anything recognised as an assignment but not as a
    setting — so the operator is told rather than left to discover it.
    """
    values: dict[str, object] = {}
    secrets: dict[str, str] = {}
    unmapped: list[str] = []
    for name, raw in legacy.items():
        if not raw or raw == "replace-me":
            continue
        if name in KEY_TO_SECRET:
            secrets[KEY_TO_SECRET[name]] = raw
            continue
        path = ENV_TO_SETTING.get(name) or CONF_TO_SETTING.get(name)
        if path is None:
            # rc's own knobs are expected to stay put, not a migration gap.
            if not name.startswith("polytropos_"):
                unmapped.append(name)
            continue
        values[path] = raw
    return values, secrets, unmapped


def _typed(path: str, raw: str) -> object:
    """Coerce a shell string to the setting's declared type."""
    setting = next(s for s in settings.SETTINGS if s.path == path)
    if setting.kind == "int":
        return int(raw)
    if setting.kind == "float":
        return float(raw)
    if setting.kind == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if setting.kind == "list":
        return [w for w in raw.replace(",", " ").split() if w]
    return raw


def _set_in(document: dict, path: str, value: object) -> None:
    node = document
    parts = path.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _already_set(document: dict, path: str) -> bool:
    node: object = document
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def merge_settings(target: Path, values: dict[str, object], *,
                   force: bool = False) -> tuple[list[str], list[str]]:
    """Write ``values`` into ``target``, leaving what is already there.

    Merging rather than replacing is what makes this safe to run on an
    install that already has a settings file — which every install does,
    because ``deploy install`` lays one down from the sample. Refusing on
    "the file exists" left the operator with an install that told them to
    run a migration the migration then declined to do.

    Returns ``(written, skipped)``: paths taken, and paths left alone
    because the file already set them.
    """
    import tomli_w
    import tomllib

    document: dict = {}
    if target.is_file():
        try:
            document = tomllib.loads(target.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigParseError(f"{target}: not valid TOML: {exc}") from exc

    written, skipped = [], []
    for path, value in sorted(values.items()):
        if not force and _already_set(document, path):
            skipped.append(path)
            continue
        _set_in(document, path, value)
        written.append(path)
    if written:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(tomli_w.dumps(document))
    return written, skipped


class ConfigParseError(Exception):
    """The settings file exists and cannot be read."""


def typed_values(raw: dict[str, str]) -> dict[str, object]:
    """Coerce migrated shell strings to their settings' declared types."""
    out: dict[str, object] = {}
    for path, value in raw.items():
        try:
            out[path] = _typed(path, str(value))
        except ValueError:
            continue
    return out


def _migrate(args: Namespace) -> int:
    """Fold polytropos.conf, harness.env and chat.env into the settings.

    Non-destructive on purpose: the originals stay exactly where they
    are. A migration that deletes the file holding your API keys before
    you have confirmed the new one works is not a migration, it is a
    gamble.
    """
    directory = paths.config_dir()
    if directory is None:
        print("error: $DPORTSV3_CONFIG_DIR is unset; nothing to migrate into",
              file=sys.stderr)
        return 1

    sources = [
        Path(getattr(args, "conf", None) or "/usr/local/etc/polytropos.conf"),
        directory / "harness.env",
        directory / "chat.env",
    ]
    legacy: dict[str, str] = {}
    read: list[Path] = []
    for source in sources:
        if not source.is_file():
            continue
        try:
            legacy.update(parse_shell_assignments(source.read_text()))
        except OSError as exc:
            print(f"error: {source}: {exc}", file=sys.stderr)
            return 1
        read.append(source)

    if not read:
        print("nothing to migrate: no polytropos.conf, harness.env or "
              "chat.env found")
        return 0

    values, secrets, unmapped = plan_migration(legacy)
    target = directory / settings.CONFIG_FILENAME


    legacy_delivery = directory / "delivery.toml"
    if legacy_delivery.is_file():
        print(f"note: {legacy_delivery} is still read and still wins over "
              f"[delivery] here; fold it in by hand when you are ready")

    print(f"read: {', '.join(str(s) for s in read)}")
    print(f"write: {target}")
    for path in sorted(values):
        print(f"  {path} = {values[path]!r}")
    for path, _value in sorted(secrets.items()):
        print(f"  {path} -> {settings.secret_path(path)} (credential)")
    for name in sorted(unmapped):
        print(f"  note: ${name} has no setting; it is not carried over")

    if getattr(args, "dry_run", False):
        print("\ndry run; nothing was written")
        return 0

    try:
        written, skipped = merge_settings(
            target, typed_values(values),
            force=getattr(args, "force", False),
        )
    except ConfigParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if target.is_file():
        target.chmod(0o644)
    for path in skipped:
        print(f"  kept: {path} is already set in {target.name}")
    if not written and not secrets:
        print("\nnothing to do: everything is already in place")
        return 0

    for path, value in secrets.items():
        destination = settings.secret_path(path)
        if destination is None:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(value + "\n")
        # The mode follows the reader — 0600 for the root runner's keys,
        # 0640 for the ones the unprivileged tracker needs. A blanket
        # 0640 would hand the patch key to the tracker. Only root can set
        # the group, so that part is the installer's job.
        mode, _grouped = settings.SECRET_MODES.get(path, (0o600, False))
        os.chmod(destination, mode)

    print("\ndone. The old files are untouched; check "
          "`dportsv3 config show` and remove them when you are satisfied.")
    return 0
