"""Install the service files onto a host.

The rc.d scripts, the shared config and the credential stubs live in the
checkout's ``deploy/`` tree; this puts them where ``rc.subr`` and the
operator will look for them, creates the service account, and hands the
evidence tree to it.

Two kinds of file, and the difference is the whole safety story:

* **tool-owned** — the rc.d scripts. Overwritten every time, because an
  upgrade that leaves an old script in place is worse than one that
  replaces it.
* **operator-owned** — ``polytropos.conf`` and the credential files.
  Written once, from the ``.sample``, and never touched again. The same
  sample/live split ``paths.config_file`` already uses.

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
RC_SCRIPTS = ("polytropos_artifact_store", "polytropos_tracker",
              "polytropos_runner")

#: ``(sample name, destination relative to <prefix>/etc, mode, group-owned)``.
#: Group-owned files are readable by the service account; the rest stay
#: root-only. Never overwritten once they exist.
OPERATOR_FILES = (
    ("polytropos.conf.sample", "polytropos.conf", 0o644, False),
    ("harness.env.sample", "polytropos/harness.env", 0o600, False),
    ("chat.env.sample", "polytropos/chat.env", 0o640, True),
)

QUEUE_SUBDIRS = ("pending", "inflight", "done", "failed")

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
    user_exists=None,
    group_exists=None,
) -> list[Action]:
    """Decide every step. Reads the filesystem; changes nothing."""
    user_exists = _user_exists if user_exists is None else user_exists
    group_exists = _group_exists if group_exists is None else group_exists
    actions: list[Action] = []

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
    actions.append(Action(
        "mkdir", f"{etc / 'polytropos'}", etc / "polytropos",
        skipped="already exists" if (etc / "polytropos").is_dir() else None))
    for sample, rel, mode, group_owned in OPERATOR_FILES:
        src = deploy / sample
        if not src.is_file():
            raise paths.MissingInput(f"missing sample in the checkout: {src}")
        dst = etc / rel
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
    actions.append(Action(
        "chown", f"{logs_root} -> {user}:{group}, recursively", logs_root,
        skipped=None if logs_root.exists() else "logs root does not exist yet"))
    return actions


def _run(argv: list[str]) -> None:
    p = subprocess.run(argv, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"{' '.join(argv)} failed ({p.returncode}): "
            f"{(p.stderr or p.stdout).strip()}")


def apply(actions, *, deploy: Path, prefix: Path, user: str, group: str,
          logs_root: Path, log=print) -> None:
    """Carry out a plan. Every step announces itself before it runs."""
    for action in actions:
        if action.skipped:
            log(f"  skip  {action.detail} ({action.skipped})")
            continue
        log(f"  do    {action.detail}")

        if action.kind == "group":
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

    prefix = Path(args.prefix)
    logs_root = Path(args.logs_root)
    try:
        actions = plan(deploy=deploy, prefix=prefix, user=args.user,
                       group=args.group, logs_root=logs_root)
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
              group=args.group, logs_root=logs_root)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"""
done. To bring the stack up, add to /etc/rc.conf:

    polytropos_artifact_store_enable="YES"
    polytropos_tracker_enable="YES"
    polytropos_runner_enable="YES"

then put real credentials in {prefix}/etc/polytropos/harness.env and
start the services. The runner refuses to start until the venv is
installed: run {deploy.parent}/bin/dportsv3 --version once first.""")
    return 0
