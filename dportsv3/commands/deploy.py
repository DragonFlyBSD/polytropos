"""Install the service files onto a host.

The rc.d scripts, the shared config and the credential stubs live in the
checkout's ``deploy/`` tree; this puts them where ``rc.subr`` and the
operator will look for them, creates the service account, and hands the
evidence tree to it.

Two kinds of file, and the difference is the whole safety story:

* **tool-owned** — the rc.d scripts. Overwritten every time, because an
  upgrade that leaves an old script in place is worse than one that
  replaces it.
* **operator-owned** — ``polytropos.conf`` and ``polytropos.toml``.
  Written once, from the ``.sample``, and never touched again. The same
  sample/live split ``paths.config_file`` already uses.

An install that still has the old ``harness.env`` / ``chat.env`` pair is
migrated on the way past — see :func:`_migrate_legacy_config`. Doing it
here rather than leaving it to the operator matters because the rc
scripts stop sourcing those files in the same upgrade: skip the step and
the runner comes back up with no API keys and every job fails.

Planning is separate from doing: :func:`plan` decides everything and
touches nothing, so ``--dry-run`` shows exactly what ``install`` would
do, and the decisions are testable without root.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path

from dportsv3 import paths

#: Copied to ``<prefix>/etc/rc.d`` and made executable.
RC_SCRIPTS = ("polytropos_tracker", "polytropos_runner")

#: ``(sample name, destination relative to <prefix>/etc, mode, group-owned)``.
#: Group-owned files are readable by the service account; the rest stay
#: root-only. Never overwritten once they exist.
OPERATOR_FILES = (
    ("polytropos.conf.sample", "polytropos.conf", 0o644, False),
    # The one settings file, replacing harness.env, chat.env,
    # delivery.toml and agentic-policy.json. Installed under its live
    # name rather than as a .sample because every setting in it is
    # commented out: the file existing changes nothing until an operator
    # uncomments a line. Credentials are not in it — they live one per
    # file under secrets/, so each mode can follow its own reader.
    ("polytropos.toml.sample", "polytropos/polytropos.toml", 0o644, False),
    # /etc/newsyslog.conf includes this directory already. Operator-owned
    # like the rest: retention is a local policy question.
    ("newsyslog.conf.sample", "newsyslog.conf.d/polytropos.conf", 0o644, False),
)

QUEUE_SUBDIRS = ("pending", "inflight", "done", "failed")

#: Linked into ``<prefix>/bin`` and checked for after an install. The first
#: two are what the rc.d scripts default to; ``artifact-store-client`` is
#: what the dsynth hooks call on every failed build, and a packaged install
#: that lacks it drops the evidence silently.
EXPECTED_COMMANDS = ("dportsv3", "dports-dev-env", "artifact-store-client")

#: Where the software itself goes, relative to the prefix. A venv rather than
#: the prefix's own site-packages: this is two distributions plus their
#: dependencies, and mixing them into a pkg-managed tree invites conflicts.
VENV_RELATIVE = Path("lib") / "polytropos"

#: Installed in this order and no other. dports-dev-env is a sibling source
#: tree rather than a PyPI package, so it has to be present before the
#: generator asks for it — otherwise pip reaches for an index and fails.
DISTRIBUTIONS = (("dev-env", "the dev-env manager"),
                 (".[tracker]", "the generator, with the tracker extra"))

#: HOME for the service account. `daemon -u` lowers the uid but leaves HOME
#: as it found it, so the two unprivileged services would otherwise run with
#: HOME=/root. It has to exist and be theirs.
SERVICE_HOME = Path("/var/db/polytropos")


@dataclass(frozen=True)
class Action:
    """One step.

    ``detail`` is for a human to read; ``target`` is what :func:`apply`
    acts on. Keeping them apart matters — an earlier version re-derived
    the target by string-matching the detail, which is the kind of thing
    that works until a path gains a space.
    """
    kind: str
    detail: str
    target: Path | str | None = None
    skipped: str | None = None


def _user_exists(name: str) -> bool:
    try:
        import pwd  # noqa: PLC0415
        pwd.getpwnam(name)
        return True
    except (ImportError, KeyError):
        return False


def _group_exists(name: str) -> bool:
    try:
        import grp  # noqa: PLC0415
        grp.getgrnam(name)
        return True
    except (ImportError, KeyError):
        return False


def plan(
    *,
    deploy: Path,
    prefix: Path,
    user: str,
    group: str,
    logs_root: Path,
    source: Path | None = None,
    user_exists=None,
    group_exists=None,
) -> list[Action]:
    """Decide every step. Reads the filesystem; changes nothing.

    ``source`` is the checkout to install the software *from*. Without it
    the software steps are skipped and only the host wiring is done —
    which is the right behaviour for a packaged install, where the
    software is already there and a port owns it.
    """
    user_exists = _user_exists if user_exists is None else user_exists
    group_exists = _group_exists if group_exists is None else group_exists
    actions: list[Action] = []

    venv = prefix / VENV_RELATIVE
    if source is None:
        actions.append(Action(
            "venv", f"{venv}", venv,
            skipped="no source tree; assuming the software is installed"))
    else:
        actions.append(Action(
            "venv", f"{venv} (--system-site-packages)", venv,
            skipped="already exists" if (venv / "bin").is_dir() else None))
        for target, what in DISTRIBUTIONS:
            spec = (str(source / "dev-env") if target == "dev-env"
                    else f"{source}[tracker]")
            # Never skipped: re-running IS the upgrade path.
            actions.append(Action("pip", f"install {what} from {source}", spec))
        for command in EXPECTED_COMMANDS:
            actions.append(Action(
                "link", f"{prefix / 'bin' / command} -> {venv / 'bin' / command}",
                command))

    if group_exists(group):
        actions.append(Action("group", f"group {group}", group,
                              skipped="already exists"))
    else:
        actions.append(Action("group", f"create group {group}", group))
    if user_exists(user):
        actions.append(Action("user", f"user {user}", user,
                              skipped="already exists"))
    else:
        actions.append(Action("user", f"create user {user}", user))

    rc_dir = prefix / "etc" / "rc.d"
    actions.append(Action("mkdir", f"{rc_dir}", rc_dir,
                          skipped="already exists" if rc_dir.is_dir() else None))
    for script in RC_SCRIPTS:
        src = deploy / "rc.d" / script
        if not src.is_file():
            raise paths.MissingInput(f"missing rc script in the checkout: {src}")
        # Tool-owned: always replaced, even when present.
        actions.append(Action("rc_script", f"{src} -> {rc_dir / script} (0755)",
                              script))

    etc = prefix / "etc"
    for sub in ("polytropos", "polytropos/secrets", "newsyslog.conf.d"):
        actions.append(Action(
            "mkdir", f"{etc / sub}", etc / sub,
            skipped="already exists" if (etc / sub).is_dir() else None))
    for sample, rel, mode, group_owned in OPERATOR_FILES:
        src = deploy / sample
        dst = etc / rel
        if not src.is_file():
            # An upgrade runs the INSTALLED planner against the NEW
            # checkout, so a sample this version expects may simply not
            # exist any more. When the destination is already in place
            # there is nothing to do and nothing wrong — say so and move
            # on, rather than failing the whole install with a message
            # about a file the operator never asked for. A missing sample
            # with no destination is still a broken checkout.
            if dst.exists():
                actions.append(Action(
                    "operator_file", f"{dst}", rel,
                    skipped=f"{sample} is no longer shipped; leaving the "
                            f"existing file alone"))
                continue
            raise paths.MissingInput(f"missing sample in the checkout: {src}")
        owner = f"root:{group}" if group_owned else "root"
        actions.append(Action(
            "operator_file", f"{dst} ({oct(mode)[2:]}, {owner})", rel,
            skipped="exists; left as it is" if dst.exists() else None))

    actions.append(Action(
        "mkdir_owned", f"{SERVICE_HOME} (owned by {user}:{group})", SERVICE_HOME,
        skipped="already exists" if SERVICE_HOME.is_dir() else None))

    evidence = logs_root / "evidence"
    for sub in QUEUE_SUBDIRS:
        d = evidence / "queue" / sub
        actions.append(Action("mkdir", f"{d}", d,
                              skipped="already exists" if d.is_dir() else None))
    # Never skipped on the grounds that the tree is absent: the mkdir steps
    # above create it, as root, moments later. Deciding from what exists at
    # PLAN time meant a fresh logs root was always left root-owned, and the
    # services then fail on their first write.
    # chown is idempotent, so running it unconditionally costs nothing.
    actions.append(Action(
        "chown", f"{logs_root} -> {user}:{group}, recursively", logs_root))
    return actions


def _python_for_venv() -> str:
    """The interpreter to build the venv with.

    Not sys.executable: this command runs inside the checkout's own venv,
    and building a venv from a venv inherits it. Take the system one.
    """
    for name in ("python3.11", "python3"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("no python3 on PATH to build the venv with")


def _run(argv: list[str]) -> None:
    p = subprocess.run(argv, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"{' '.join(argv)} failed ({p.returncode}): "
            f"{(p.stderr or p.stdout).strip()}")


def apply(actions, *, deploy: Path, prefix: Path, user: str, group: str,
          logs_root: Path, source: Path | None = None, log=print) -> None:
    """Carry out a plan. Every step announces itself before it runs."""
    for action in actions:
        if action.skipped:
            log(f"  skip  {action.detail} ({action.skipped})")
            continue
        log(f"  do    {action.detail}")

        if action.kind == "venv":
            # --system-site-packages so the pkg-installed py311-fastapi,
            # py311-pydantic and friends are visible. Without it pip builds
            # them from source and needs a Rust toolchain the base system
            # does not have.
            _run([_python_for_venv(), "-m", "venv",
                  "--system-site-packages", str(prefix / VENV_RELATIVE)])
        elif action.kind == "pip":
            _run([str(prefix / VENV_RELATIVE / "bin" / "python"), "-m", "pip",
                  "install", "--upgrade", action.target])
        elif action.kind == "link":
            link = prefix / "bin" / action.target
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(prefix / VENV_RELATIVE / "bin" / action.target)
        elif action.kind == "group":
            _run(["pw", "groupadd", group])
        elif action.kind == "user":
            _run(["pw", "useradd", user, "-g", group,
                  "-d", str(SERVICE_HOME), "-s", "/usr/sbin/nologin",
                  "-c", "Polytropos services"])
        elif action.kind == "mkdir":
            Path(action.target).mkdir(parents=True, exist_ok=True)
        elif action.kind == "mkdir_owned":
            path = Path(action.target)
            path.mkdir(parents=True, exist_ok=True)
            shutil.chown(path, user=user, group=group)
        elif action.kind == "rc_script":
            dst = prefix / "etc" / "rc.d" / action.target
            shutil.copyfile(deploy / "rc.d" / action.target, dst)
            dst.chmod(0o755)
        elif action.kind == "operator_file":
            sample, _, mode, group_owned = next(
                f for f in OPERATOR_FILES if f[1] == action.target)
            dst = prefix / "etc" / action.target
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(deploy / sample, dst)
            dst.chmod(mode)
            if group_owned:
                shutil.chown(dst, user="root", group=group)
        elif action.kind == "chown":
            # Group inheritance covers new files (BSD takes the group from
            # the parent directory), but everything already there predates
            # the service account and has to be handed over once.
            _run(["chown", "-R", f"{user}:{group}", str(logs_root)])
            _run(["chmod", "-R", "g+w", str(logs_root)])


def _migrate_legacy_config(prefix: Path, group: str, log) -> None:
    """Fold an existing harness.env / chat.env into polytropos.toml.

    Run as part of the install, not left to the operator, because this
    upgrade also stops the rc scripts sourcing those files. An operator
    who restarts before migrating would get a runner with no API keys and
    every job failing on the first LLM call — a total outage produced by
    a tidy-up. Non-destructive: the originals stay where they are.

    Silent when there is nothing to migrate, and never fatal: a
    half-recognised old file should not stop an install that is otherwise
    fine.
    """
    import os as _os  # noqa: PLC0415

    etc = prefix / "etc" / "polytropos"
    legacy = [etc / "harness.env", etc / "chat.env"]
    if not any(p.is_file() for p in legacy):
        return

    from dportsv3.commands import config as config_cmd  # noqa: PLC0415

    values: dict[str, str] = {}
    for source in legacy:
        if source.is_file():
            values.update(config_cmd.parse_shell_assignments(source.read_text()))
    settings_values, secrets, _unmapped = config_cmd.plan_migration(values)
    if not settings_values and not secrets:
        return

    from dportsv3 import settings  # noqa: PLC0415

    for path, secret in secrets.items():
        name = Path(str(_SECRET_FILES[path])).name
        destination = etc / "secrets" / name
        if destination.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(secret + "\n")
        # The mode follows the reader. 0600 root for the two the queue
        # runner uses, 0640 root:group for the ones the tracker needs —
        # a blanket 0640 would hand the patch key to a service that has
        # no authentication and an API that can spend LLM credit.
        mode, grouped = settings.SECRET_MODES.get(path, (0o600, False))
        _os.chmod(destination, mode)
        if grouped:
            try:
                shutil.chown(destination, user="root", group=group)
            except (LookupError, PermissionError):
                pass
        log(f"  migrated a credential into {destination} "
            f"({oct(mode)[2:]}{', ' + group if grouped else ', root only'})")

    # And the values, not just the credentials. Leaving these to the
    # operator was the same outage in slower motion: the rc scripts stop
    # sourcing harness.env in this upgrade, so an install that migrated
    # the keys but not llm.triage.model comes back up with no model
    # configured and refuses every job.
    #
    # merge_settings leaves anything the file already sets, so this is
    # safe against a settings file the operator has edited, and safe to
    # run twice.
    target = etc / "polytropos.toml"
    written, _kept = config_cmd.merge_settings(
        target, config_cmd.typed_values(settings_values))
    if written:
        target.chmod(0o644)
        log(f"  migrated {len(written)} setting(s) into {target}")


#: ``secret setting -> the file it names``, read once so the installer
#: does not have to import the settings table's defaults by hand.
_SECRET_FILES = {
    "llm.triage.api_key_file": "secrets/triage.key",
    "llm.patch.api_key_file": "secrets/patch.key",
    "llm.chat.api_key_file": "secrets/chat.key",
}


def missing_commands(prefix: Path) -> list[str]:
    """Which of the expected entry points are not in ``<prefix>/bin``."""
    return [c for c in EXPECTED_COMMANDS if not (prefix / "bin" / c).exists()]


def cmd_deploy(args: Namespace) -> int:
    if getattr(args, "deploy_action", None) != "install":
        print("usage: dportsv3 deploy install", file=sys.stderr)
        return 2

    if platform.system() not in ("DragonFly", "FreeBSD"):
        print(f"error: deploy install targets DragonFly BSD; this is "
              f"{platform.system()}. The files are in deploy/ if you want "
              f"to place them by hand.", file=sys.stderr)
        return 1

    try:
        deploy = paths.deploy_dir(getattr(args, "tool_root", None))
    except paths.MissingInput as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # The tree to install the software from. Absent when running from a
    # packaged install, which has nothing to install itself from and no
    # need to: there, a port owns the software and this only wires the host.
    source = None
    if not getattr(args, "no_software", False):
        try:
            source = paths.tool_root(getattr(args, "tool_root", None))
        except paths.MissingInput:
            source = None

    prefix = Path(args.prefix)
    logs_root = Path(args.logs_root)
    try:
        actions = plan(deploy=deploy, prefix=prefix, user=args.user,
                       group=args.group, logs_root=logs_root, source=source)
    except paths.MissingInput as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"installing from {deploy}")
    if args.dry_run:
        for action in actions:
            mark = "skip" if action.skipped else "do  "
            suffix = f" ({action.skipped})" if action.skipped else ""
            print(f"  {mark}  {action.detail}{suffix}")
        print("\ndry run; nothing was changed")
        return 0

    if os.geteuid() != 0:
        print("error: deploy install needs root (it creates a user and "
              "writes under /usr/local/etc)", file=sys.stderr)
        return 1

    try:
        apply(actions, deploy=deploy, prefix=prefix, user=args.user,
              group=args.group, logs_root=logs_root, source=source)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        _migrate_legacy_config(prefix, args.group, print)
    except Exception as exc:  # noqa: BLE001 — never fail an install over this
        print(f"warning: could not migrate the old .env files: {exc}",
              file=sys.stderr)

    absent = missing_commands(prefix)
    if absent:
        print(f"""
warning: {', '.join(absent)} still missing from {prefix / 'bin'}. The
services refuse to start until polytropos_cmd and polytropos_dev_env_cmd
in {prefix}/etc/polytropos.conf name commands that run.""",
              file=sys.stderr)

    etc_p = prefix / "etc" / "polytropos"
    user, group = args.user, args.group
    print(f"""
done. To bring the stack up, add to /etc/rc.conf:

    polytropos_tracker_enable="YES"
    polytropos_runner_enable="YES"

then put real credentials in {etc_p}/harness.env and
start the services.

Everything is configured in one place now:

    {etc_p}/polytropos.toml

Every setting in it is commented out and shows its default. See what is
actually in effect, and where each value came from, with:

    dportsv3 config show
    dportsv3 config check

Credentials are NOT in that file. Each lives in its own file under
{etc_p}/secrets/, so the mode can follow whichever
service reads it — the runner is root, the tracker is not:

    secrets/triage.key      0600 root
    secrets/patch.key       0600 root
    secrets/chat.key        0640 root:{group}
    secrets/delivery.token  0640 root:{group}

Delivery is off until delivery.type is set. Start with "local-patch",
which writes the diff to delivery.outbox instead of pushing, then switch
to "github" once you have seen a patch land there.""")
    return 0
