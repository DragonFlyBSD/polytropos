"""`dev-env rebase` moves an env onto another world, or refuses unchanged (poly-xrok).

The env has to be unmounted in between, and an unmount that fails halfway
takes /dev and /tmp from under whatever is inside. So every refusal must
come before anything is unmounted, and a refusal leaves the env as it was.
The env store and the lock are real; Avalon, the provisioner, the mount
table, fstat, the session's mount path and the venv cache are faked.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import urllib.error
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from dports_dev_env import base, cli, mounts, rebase, runtime
from dports_dev_env.base import BaseArchive, ProvisionedBase
from dports_dev_env.errors import DevEnvError, MountError, StateError, UsageError
from dports_dev_env.mounts import Holder, Mount
from dports_dev_env.session import EnvironmentSession
from dports_dev_env.state import (
    STATE_SCHEMA,
    EnvironmentState,
    RepoState,
    RuntimeState,
    SourceState,
)
from dports_dev_env.store import EnvironmentStore
from dports_dev_env.sync import DirtySyncer

OLD = "DragonFly-x86_64-20261022-DEV-v6.5.0-2096-g85c411a229.world.tar.gz"
NEW = "DragonFly-x86_64-20261023-DEV-v6.5.0-2121-gf25f77d1f4.world.tar.gz"
PUBLISHED = {OLD: "a" * 64, NEW: "b" * 64}


def fake_base_id(config, archive: BaseArchive) -> str:
    """Stands in for provisioned_base_id: the asset, its sha256 and the
    other base inputs, folded into one string the config can change."""
    data = f"{archive.asset}|{archive.sha256}|{config.base_inputs}"
    return hashlib.sha256(data.encode()).hexdigest()[:12]


class World:
    """One env named "e" on the OLD world, idle, with every fake recording."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        cache = tmp_path / "cache"
        self.config = SimpleNamespace(
            cache_root=cache,
            envs_dir=cache / "envs",
            archives_dir=cache / "bases" / "archives",
            provisioned_bases_dir=cache / "bases" / "p",
            locks_dir=cache / "locks",
            avalon_releases_url="https://avalon.example/releases/",
            base_inputs="v1",
        )
        self.store = EnvironmentStore(self.config)
        self.env_dir = self.store.env_dir("e")
        self.root = self.store.root_dir("e")
        self.writable = self.store.writable_dir("e")
        (self.writable / "work").mkdir(parents=True)
        (self.writable / "work" / "DeltaPorts").symlink_to("ports-main")
        self.config.archives_dir.mkdir(parents=True)

        self.old_id = fake_base_id(self.config, BaseArchive(OLD, Path(), PUBLISHED[OLD]))
        self.make_base(self.old_id, OLD)
        self.store.save(self.state())

        self.published = dict(PUBLISHED)
        self.latest = NEW
        self.downloaded_sha: dict[str, str] = {}
        self.calls: dict[str, list] = {"download": [], "provision": [], "unmount": [], "venv": [], "mount": [], "sleep": []}
        self.holders: list[list[Holder]] = []
        self.checked_status: list[str] = []
        self.no_fstat = False
        self.stuck: set[Path] = set()
        self.mount_base: str | None = None
        self.provisioned_id: str | None = None
        self.on_provision = None
        self.on_venv = None
        self.download_error: Exception | None = None
        self.table: list[Mount] = []
        self.mount_env(self.old_id)

        monkeypatch.setattr(rebase, "provisioned_base_id", fake_base_id)
        monkeypatch.setattr(rebase, "fetch_latest_world_asset", lambda config: self.latest)
        monkeypatch.setattr(rebase, "published_world_sha256", self.published_sha256)
        monkeypatch.setattr(rebase, "ensure_base_archive", self.ensure_base_archive)
        monkeypatch.setattr(rebase, "BaseProvisioner", self.provisioner())
        monkeypatch.setattr(rebase, "list_mounts", lambda: list(self.table))
        monkeypatch.setattr(rebase, "unmount_under", self.unmount_under)
        monkeypatch.setattr(rebase, "holders_under", self.holders_under)
        monkeypatch.setattr(rebase, "EnvironmentSession", self.session())
        monkeypatch.setattr(rebase, "GeneratorVenvCache", self.venv_cache())
        monkeypatch.setattr(rebase, "same_directory", self.same_directory)
        monkeypatch.setattr(rebase.time, "sleep", lambda seconds: self.calls["sleep"].append(seconds))

    # -- state ---------------------------------------------------------------

    def state(self, **changes) -> EnvironmentState:
        state = EnvironmentState(
            schema=STATE_SCHEMA, name="e", backend="chroot", target="@2026Q4",
            origin="", status="ready", created_at="2026-10-22T00:00:00Z",
            updated_at="2026-10-22T00:00:00Z", root_dir=self.root,
            writable_dir=self.writable, provisioned_base_id=self.old_id,
            repos=RepoState("master", "2026Q4", "master", "master"),
            source=SourceState("/host/DeltaPorts", "/host/polytropos"),
            runtime=RuntimeState("/usr/distfiles", "off"),
        )
        return replace(state, **changes)

    def load(self) -> EnvironmentState:
        return self.store.load("e")

    def env_json(self) -> str:
        return (self.env_dir / "env.json").read_text()

    def set_state(self, **changes) -> None:
        self.store.save(replace(self.load(), **changes))

    def new_id(self) -> str:
        return fake_base_id(self.config, BaseArchive(NEW, Path(), self.published[NEW]))

    # -- bases and mounts ----------------------------------------------------

    def make_base(self, base_id: str, asset: str) -> Path:
        base_dir = self.config.provisioned_bases_dir / base_id
        (base_dir / "root").mkdir(parents=True, exist_ok=True)
        (base_dir / "ready").write_text("")
        (base_dir / "metadata.json").write_text(json.dumps({"archive": {"asset": asset}}))
        return base_dir

    def mount_env(self, base_id: str) -> None:
        root = self.root.resolve()
        source = (self.config.provisioned_bases_dir / base_id / "root").resolve()
        self.table.append(Mount(str(source), root))
        for target in sorted(runtime.env_mount_targets(root) - {root}):
            self.table.append(Mount("/elsewhere", target))

    def env_mounts(self) -> list[Mount]:
        return rebase.mounts_at_or_below(self.table, self.env_dir)

    def unmount_under(self, root: Path) -> bool:
        self.calls["unmount"].append(root)
        self.table = [m for m in self.table if m.target in self.stuck or m not in self.env_mounts()]
        return not self.stuck

    def same_directory(self, root: Path, base_root: Path) -> bool:
        """What stat through the nullfs mount at ``root`` would say."""
        sources = [m.source for m in self.table if m.target == root.resolve()]
        return bool(sources) and sources[-1] == str(base_root.resolve())

    def holders_under(self, root: Path) -> list[Holder]:
        self.checked_status.append(self.load().status)
        if self.no_fstat:
            raise MountError("fstat not found; cannot tell whether anything holds the env")
        return self.holders.pop(0) if self.holders else []

    # -- Avalon and the provisioner -------------------------------------------

    def published_sha256(self, config, asset: str) -> str:
        if asset not in self.published:
            raise FileNotFoundError(asset)
        return self.published[asset]

    def ensure_base_archive(self, config, asset: str) -> BaseArchive:
        self.calls["download"].append(asset)
        if self.download_error:
            raise self.download_error
        path = self.config.archives_dir / asset
        sha = self.downloaded_sha.get(asset, self.published.get(asset, ""))
        if asset in self.downloaded_sha:
            path.write_bytes(b"not what Avalon published")
        return BaseArchive(asset=asset, path=path, sha256=sha)

    def provisioner(self):
        world = self

        class Provisioner:
            def __init__(self, config):
                pass

            def prepare(self, archive: BaseArchive) -> ProvisionedBase:
                world.calls["provision"].append(archive.asset)
                base_id = world.provisioned_id or fake_base_id(world.config, archive)
                base_dir = world.make_base(base_id, archive.asset)
                if world.on_provision:
                    world.on_provision()
                return ProvisionedBase(base_id, base_dir / "root", base_dir / "metadata.json")

        return Provisioner

    def session(self):
        world = self

        class Session:
            def __init__(self, config, store):
                pass

            def prepare(self, name: str, *, rebasing: bool = False) -> EnvironmentState:
                assert rebasing
                state = world.load()
                assert state.status == "rebasing"
                world.calls["mount"].append(state.provisioned_base_id)
                world.mount_env(world.mount_base or state.provisioned_base_id)
                return state

        return Session

    def venv_cache(self):
        world = self

        class Cache:
            def __init__(self, config):
                pass

            def prepare(self, root_dir: Path, base_id: str) -> None:
                world.calls["venv"].append(base_id)
                if world.on_venv:
                    world.on_venv()

        return Cache

    # -- running -------------------------------------------------------------

    def rebase(self, **kw) -> int:
        return rebase.rebase_env(self.config, self.store, "e", **kw)

    def assert_untouched(self, before: str) -> None:
        assert self.env_json() == before
        assert self.calls["unmount"] == []
        assert self.calls["mount"] == []
        assert self.calls["venv"] == []
        assert len(self.env_mounts()) == len(runtime.env_mount_targets(self.root))


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    return World(tmp_path, monkeypatch)


def holder(pid: str = "853704", fd: str = "root", path: str = "") -> Holder:
    return Holder(cmd="bash", pid=pid, fd=fd, path=path)


# -- success, and what it touches ---------------------------------------------


def test_rebase_moves_the_env_to_the_latest_world(world, capsys):
    assert world.rebase() == 0
    state = world.load()
    assert state.status == "ready"
    assert state.provisioned_base_id == world.new_id()
    assert world.calls["download"] == [NEW]
    assert world.calls["provision"] == [NEW]
    assert world.calls["venv"] == [world.new_id()]
    root_sources = [m.source for m in world.table if m.target == world.root.resolve()]
    assert root_sources == [str(world.config.provisioned_bases_dir / world.new_id() / "root")]
    # The old base stays for other envs and for going back.
    assert (world.config.provisioned_bases_dir / world.old_id / "ready").exists()
    err = capsys.readouterr().err
    assert f"{OLD} -> {NEW}" in err
    assert f"rebase e --asset {OLD}" in err


def test_asset_is_used_verbatim(world):
    world.latest = "not consulted"
    world.published["DragonFly-x86_64-20261010-DEV-v6.5.0-2000-g0123456789.world.tar.gz"] = "c" * 64
    asset = "DragonFly-x86_64-20261010-DEV-v6.5.0-2000-g0123456789.world.tar.gz"
    assert world.rebase(asset=asset) == 0
    assert world.calls["download"] == [asset]


def test_an_older_target_warns(world, capsys):
    older = "DragonFly-x86_64-20260927-DEV-v6.5.0-1955-g412073751d.world.tar.gz"
    world.published[older] = "d" * 64
    world.rebase(asset=older)
    assert f"{older} is older than the env's current world {OLD}" in capsys.readouterr().err


def test_already_on_target_touches_nothing(world, capsys):
    before = world.env_json()
    assert world.rebase(asset=OLD) == 0
    assert "already on" in capsys.readouterr().err
    assert world.calls["provision"] == []
    world.assert_untouched(before)


def test_same_id_while_rebasing_resumes(world):
    world.set_state(status="rebasing")
    assert world.rebase(asset=OLD) == 0
    assert world.calls["unmount"] and world.calls["venv"] == [world.old_id]
    assert world.load().status == "ready"


def test_same_world_with_changed_base_inputs_is_provisioned_again(world, capsys):
    world.config.base_inputs = "v2"
    assert world.rebase(asset=OLD) == 0
    assert "the world is unchanged, but the base inputs changed" in capsys.readouterr().err
    assert world.load().provisioned_base_id != world.old_id
    assert world.calls["provision"] == [OLD]


@pytest.mark.parametrize("status", ["creating", "destroying", "failed"])
def test_envs_mid_operation_or_failed_are_refused(world, status):
    world.set_state(status=status)
    before = world.env_json()
    with pytest.raises(UsageError, match=f"'{status}'|marked failed"):
        world.rebase()
    assert world.calls["provision"] == []
    world.assert_untouched(before)


def test_a_held_env_lock_refuses_at_once(world):
    (world.config.locks_dir / "env-e").mkdir(parents=True)
    with pytest.raises(DevEnvError, match="within 0s"):
        world.rebase()


# -- the target -----------------------------------------------------------------


@pytest.mark.parametrize("asset", [
    "../" + NEW,
    "x/" + NEW,
    "prefix-" + NEW,
    NEW + ".sha256",
    "DragonFly-x86_64-20261023.world.tar.gz/../../etc",
])
def test_asset_names_that_are_not_one_world_are_rejected(asset):
    with pytest.raises(UsageError, match="not a DragonFly world asset name"):
        base.validate_world_asset(asset)


def test_an_asset_neither_cached_nor_published_is_rejected(world):
    gone = "DragonFly-x86_64-20260522-DEV-v6.5.0-1500-gabcdef0123.world.tar.gz"
    with pytest.raises(UsageError, match="neither cached in .* nor published at"):
        world.rebase(asset=gone)


def test_avalon_unreachable_is_an_error_not_a_traceback(world, monkeypatch):
    def unreachable(config):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(rebase, "fetch_latest_world_asset", unreachable)
    with pytest.raises(DevEnvError, match="cannot list https://avalon.example/releases/"):
        world.rebase(dry_run=True)


def test_a_cached_archive_gives_the_same_id_as_the_published_checksum(world):
    content = b"a world archive"
    world.published[NEW] = hashlib.sha256(content).hexdigest()
    published = rebase.resolve_target(world.config, NEW)
    (world.config.archives_dir / NEW).write_bytes(content)
    cached = rebase.resolve_target(world.config, NEW)
    assert (published.archive_cached, cached.archive_cached) == (False, True)
    assert published.base_id == cached.base_id


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("body, ok", [
    (f"{'b' * 64} {NEW}\n", True),
    (f"{'b' * 64} {NEW}", True),
    (f"{'b' * 64} {OLD}\n", False),
    (f"{'b' * 63} {NEW}\n", False),
    (f"{'b' * 64}  {NEW}\n", False),
    (f"SHA256 ({NEW}) = {'b' * 64}\n", False),
    (f"{'b' * 64} {NEW}\n{'b' * 64} {NEW}\n", False),
])
def test_published_checksum_format(monkeypatch, body, ok):
    seen = []

    def urlopen(url, timeout):
        seen.append(url)
        return Response(body.encode())

    monkeypatch.setattr(base.urllib.request, "urlopen", urlopen)
    config = SimpleNamespace(avalon_releases_url="https://avalon.example/releases/")
    if ok:
        assert base.published_world_sha256(config, NEW) == "b" * 64
    else:
        with pytest.raises(DevEnvError, match="unexpected checksum file"):
            base.published_world_sha256(config, NEW)
    assert seen == [f"https://avalon.example/releases/{NEW}.sha256"]


def test_a_missing_published_checksum_is_file_not_found(monkeypatch):
    def urlopen(url, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(base.urllib.request, "urlopen", urlopen)
    config = SimpleNamespace(avalon_releases_url="https://avalon.example/releases")
    with pytest.raises(FileNotFoundError):
        base.published_world_sha256(config, NEW)


def test_a_download_that_does_not_match_the_published_checksum_stops_before_provisioning(world):
    world.downloaded_sha[NEW] = "e" * 64
    before = world.env_json()
    with pytest.raises(StateError, match="but Avalon publishes"):
        world.rebase()
    assert world.calls["provision"] == []
    world.assert_untouched(before)
    # Moved aside, so a re-run cannot take its hash from the bad file.
    archive = world.config.archives_dir / NEW
    assert not archive.exists() and archive.with_name(NEW + ".bad").is_file()
    del world.downloaded_sha[NEW]
    assert world.rebase() == 0
    assert world.calls["download"] == [NEW, NEW]


def test_a_failed_download_is_an_error_not_a_traceback(world):
    world.download_error = urllib.error.URLError("connection reset")
    with pytest.raises(DevEnvError, match=f"cannot download {NEW}"):
        world.rebase()
    assert world.calls["unmount"] == []


def test_a_provisioned_id_other_than_the_computed_one_stops_before_unmounting(world):
    world.provisioned_id = "somethingelse"
    before = world.env_json()
    with pytest.raises(StateError, match="expected"):
        world.rebase()
    world.assert_untouched(before)


# -- busy -----------------------------------------------------------------------


def job_link(world):
    link = world.writable / "work" / "DeltaPorts"
    link.unlink()
    link.symlink_to("job-patch-abc123")


def missing_link(world):
    (world.writable / "work" / "DeltaPorts").unlink()


def worker_mounts(world):
    build = world.root.resolve() / "work/dsynth/build"
    world.table += [Mount("tmpfs", build / "SL00"), Mount("/bin", build / "SL00" / "bin")]


def holder_in_env(world):
    world.holders = [[holder(path=str(world.root.resolve()))]]


def no_fstat(world):
    world.no_fstat = True


def foreign_mount(world):
    world.table.append(Mount("/build/synth/ccache", world.root.resolve() / "work/ccache"))


#: Each makes the env busy one way, and the refusal must say which.
BUSY = {
    "job-patch-abc123, not ports-main": job_link,
    "DeltaPorts is missing": missing_link,
    "2 dsynth worker mount": worker_mounts,
    r"bash \(pid 853704\) root": holder_in_env,
    "fstat not found": no_fstat,
    r"work/ccache \(from /build/synth/ccache\) is not a dev-env mount": foreign_mount,
}


@pytest.mark.parametrize("finding", BUSY)
def test_each_busy_finding_alone_refuses_with_nothing_changed(world, capsys, finding):
    BUSY[finding](world)
    before = world.env_json()
    mounts_before = list(world.table)
    with pytest.raises(UsageError, match="is busy; nothing was changed"):
        world.rebase()
    assert re.search(finding, capsys.readouterr().err)
    assert world.calls["download"] == [] and world.calls["provision"] == []
    assert world.env_json() == before
    assert world.table == mounts_before
    assert world.calls["unmount"] == []


def test_env_mount_targets_are_what_the_dev_env_mounts(tmp_path, monkeypatch):
    """Recorded from mount_env_root and prepare_root_runtime themselves, so a
    mount added there without joining env_mount_targets fails here: rebase
    would refuse it as hand-made."""
    targets = []
    monkeypatch.setattr(runtime, "mount_null_checked",
                        lambda source, target, read_only=False: targets.append(target) or True)
    monkeypatch.setattr(runtime, "mount_procfs_checked", lambda target: targets.append(target) or True)
    monkeypatch.setattr(runtime, "ensure_resolv_conf", lambda root_dir, force=False: None)
    env_dir, base_root = tmp_path / "envs" / "e", tmp_path / "base"
    config = SimpleNamespace(host_distdir=tmp_path / "distfiles", repos_dir=tmp_path / "repos")
    for path in (base_root, config.host_distdir, config.repos_dir):
        path.mkdir(parents=True)
    runtime.mount_env_root(base_root, env_dir, env_dir / "root")
    runtime.prepare_root_runtime(config, env_dir / "root")
    assert {t.resolve() for t in targets} == runtime.env_mount_targets(env_dir / "root")


def test_the_envs_own_mounts_are_not_busy(world):
    # construction is itself one of the env's writable mounts; v1 matched it.
    assert world.root.resolve() / "construction" in {m.target for m in world.env_mounts()}
    assert rebase.busy_findings(world.load(), world.env_dir) == []


def test_holders_are_listed_once_per_process_and_capped(world):
    root = str(world.root.resolve())
    world.holders = [[holder(pid=str(n), fd=fd, path=root) for n in range(12) for fd in ("root", "wd")]]
    findings = rebase.busy_findings(world.load(), world.env_dir)
    assert len([f for f in findings if f.startswith("process in the env")]) == rebase.MAX_LISTED_HOLDERS
    assert findings[-1] == "... and 2 more process(es) in the env"


def test_the_last_busy_check_runs_on_a_rebasing_env_after_the_grace(world):
    world.rebase()
    assert world.checked_status == ["ready", "rebasing"]
    assert world.calls["sleep"] == [rebase.SESSION_GRACE_SECONDS]


def test_busy_only_after_the_save_writes_the_entry_state_back(world):
    world.holders = [[], [holder(path=str(world.root.resolve()))]]
    before = world.env_json()
    with pytest.raises(UsageError, match="is busy"):
        world.rebase()
    assert world.checked_status == ["ready", "rebasing"]
    assert world.env_json() == before
    assert world.calls["unmount"] == []


def test_busy_after_the_save_on_a_resume_stays_rebasing(world):
    world.set_state(status="rebasing")
    world.holders = [[], [holder(path=str(world.root.resolve()))]]
    with pytest.raises(UsageError, match="is busy"):
        world.rebase()
    assert world.checked_status == ["rebasing", "rebasing"]
    assert world.load().status == "rebasing"
    assert world.load().provisioned_base_id == world.old_id


def test_unmount_survivors_write_the_entry_state_back(world):
    world.stuck = {world.root.resolve() / "work"}
    before = world.env_json()
    with pytest.raises(UsageError, match="could not unmount e"):
        world.rebase()
    assert world.env_json() == before
    assert world.calls["mount"] == [] and world.calls["venv"] == []


def test_an_env_changed_while_provisioning_is_not_resaved(world):
    world.on_provision = lambda: world.set_state(updated_at="2026-10-27T00:00:00Z")
    with pytest.raises(UsageError, match="changed while its new base was prepared"):
        world.rebase()
    assert world.calls["unmount"] == []


def test_a_destroy_during_the_rebase_is_not_undone(world):
    import shutil

    world.on_venv = lambda: shutil.rmtree(world.env_dir)
    with pytest.raises(StateError, match="was destroyed during the rebase"):
        world.rebase()
    assert not world.env_dir.exists()


def test_same_directory_is_by_device_and_inode(tmp_path):
    (tmp_path / "base").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "link").symlink_to("base")
    assert rebase.same_directory(tmp_path / "link", tmp_path / "base")
    assert not rebase.same_directory(tmp_path / "other", tmp_path / "base")
    assert not rebase.same_directory(tmp_path / "missing", tmp_path / "base")


def test_an_unmounted_root_fails_the_source_check(world):
    world.table = []
    with pytest.raises(StateError, match="nothing is mounted at"):
        rebase.check_root_source(world.config, world.load(), world.old_id)


def test_old_base_mounted_back_fails_and_stays_rebasing(world):
    world.mount_base = world.old_id
    with pytest.raises(StateError, match="is not mounted from"):
        world.rebase()
    state = world.load()
    assert state.status == "rebasing"
    assert state.provisioned_base_id == world.new_id()
    assert world.calls["venv"] == []


# -- dry run ----------------------------------------------------------------------


def test_dry_run_changes_nothing(world, capsys):
    before = world.env_json()
    assert world.rebase(dry_run=True) == 0
    err = capsys.readouterr().err
    assert f"current  {OLD} (base {world.old_id})" in err
    assert f"target   {NEW} (base {world.new_id()})" in err
    assert "archive  to download" in err and "base     to provision" in err
    assert "busy     no" in err
    assert world.calls["download"] == [] and world.calls["provision"] == []
    world.assert_untouched(before)


def test_dry_run_reports_findings_and_exits_1(world, capsys):
    foreign_mount(world)
    assert world.rebase(dry_run=True) == 1
    assert "busy     yes; rebase would refuse:" in capsys.readouterr().err
    assert world.calls["unmount"] == []


# -- fstat ------------------------------------------------------------------------


FSTAT = """\
USER     CMD          PID   FD PATH                  INUM MODE           SZ|DV R/W
root     bash       853704 root /c/envs/e/root     158026162 drwxr-xr-x    offset:0  r
root     bash       853704   wd /c/envs/e/root/root    158067900 drwx------    offset:0  r
root     bash       853704 text /c/bases/p/dce17dfd5300/root/usr/local/bin/bash     158040701 -rwxr-xr-x    offset:0  r
root     sleep      89962    3 /c/envs/e/root/work/polytropos/pyproject.toml  158143556 -rw-r--r--    offset:0  r
root     sh         1001   wd /c/envs/e/root2/tmp    1 drwxrwxrwt    offset:0  r
root     sh         1002   wd /c/envs/main/root    1 drwxr-xr-x    offset:0  r
root     sh         1003   wd /c/envs/e/writable/work    1 drwxr-xr-x    offset:0  r
root     hammer2       68    5* pipe fffff8068fa60ea0 (A<->B) ravail 0 wavail 0 rw
"""


def test_holders_come_from_plain_fstat_paths(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=FSTAT, stderr="")

    monkeypatch.setattr(mounts.shutil, "which", lambda name: "/usr/bin/fstat")
    monkeypatch.setattr(mounts.subprocess, "run", run)
    found = mounts.holders_under(Path("/c/envs/e/root"))
    assert calls == [["fstat"]]
    assert [(h.pid, h.fd, h.path) for h in found] == [
        ("853704", "root", "/c/envs/e/root"),
        ("853704", "wd", "/c/envs/e/root/root"),
        ("89962", "3", "/c/envs/e/root/work/polytropos/pyproject.toml"),
    ]


def test_no_fstat_or_a_failing_one_is_an_error_not_no_holders(monkeypatch):
    monkeypatch.setattr(mounts.shutil, "which", lambda name: None)
    with pytest.raises(MountError, match="fstat not found"):
        mounts.holders_under(Path("/c/envs/e/root"))
    monkeypatch.setattr(mounts.shutil, "which", lambda name: "/usr/bin/fstat")
    monkeypatch.setattr(
        mounts.subprocess, "run",
        lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="kvm_open: denied"),
    )
    with pytest.raises(MountError, match="fstat failed"):
        mounts.holders_under(Path("/c/envs/e/root"))


# -- the gate ---------------------------------------------------------------------


def test_sessions_refuse_a_rebasing_env(world):
    world.set_state(status="rebasing")
    session = EnvironmentSession(world.config, world.store)
    with pytest.raises(UsageError, match="re-run `rebase e`"):
        session.prepare("e")
    with pytest.raises(UsageError, match="'rebasing'"):
        DirtySyncer(world.config, world.store).sync("e")


def test_a_session_that_passed_the_gate_rechecks_after_mounting(world, monkeypatch):
    """A rebase saving "rebasing" while a session mounts: the session must
    refuse, not enter the env after rebase's last busy check."""
    from dports_dev_env import session as session_mod

    def rebase_starts(self, env_dir, state):
        world.set_state(status="rebasing")

    monkeypatch.setattr(EnvironmentSession, "ensure_root_mounted", rebase_starts)
    for name in ("write_dsynth_config", "write_shell_rc", "write_helper_scripts", "prepare_root_runtime"):
        monkeypatch.setattr(session_mod, name, lambda *a, **kw: None)
    with pytest.raises(UsageError, match="'rebasing'"):
        EnvironmentSession(world.config, world.store).prepare("e")
    world.set_state(status="ready")
    with pytest.raises(UsageError, match="'rebasing'"):
        DirtySyncer(world.config, world.store).sync("e")


def test_cleanup_mounts_refuses_while_an_env_is_rebasing(world, monkeypatch):
    world.set_state(status="rebasing")
    unmounted = []
    monkeypatch.setattr(cli, "require_root", lambda: None)
    monkeypatch.setattr(cli, "load_config", lambda: world.config)
    monkeypatch.setattr(cli, "validate_cache_root", lambda root: None)
    monkeypatch.setattr(cli, "ordered_mounts_under", lambda root: list(world.table))
    monkeypatch.setattr(cli, "unmount_under", lambda root: unmounted.append(root))
    assert cli.cmd_cleanup_mounts(SimpleNamespace(yes=True)) == 1
    assert unmounted == []
