"""What the agent changed (poly-qqx9.7).

The overlay under ports/<origin>/ is NOT reset between attempts, so the
tree is cumulative and every file needs saying which attempt first wrote
it. That attribution is a query over the write-tool rows, which carry
their arguments since poly-qqx9.13.
"""

from __future__ import annotations

from dportsv3.tracker.render import working_tree
from dportsv3.tracker.render.worktree import parse_diff_files

DIFF = """diff --git a/ports/devel/foo/Makefile b/ports/devel/foo/Makefile
index 1111111..2222222 100644
--- a/ports/devel/foo/Makefile
+++ b/ports/devel/foo/Makefile
@@ -1,3 +1,3 @@
 USES=\tcmake
-OLD_LINE=\t1
+NEW_LINE=\t2
diff --git a/ports/devel/foo/files/patch-CMakeLists_txt b/ports/devel/foo/files/patch-CMakeLists_txt
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/ports/devel/foo/files/patch-CMakeLists_txt
@@ -0,0 +1,2 @@
+--- CMakeLists.txt.orig
++++ CMakeLists.txt
diff --git a/ports/devel/foo/files/patch-old b/ports/devel/foo/files/patch-old
deleted file mode 100644
index 4444444..0000000
--- a/ports/devel/foo/files/patch-old
+++ /dev/null
@@ -1,1 +0,0 @@
-gone
"""


def test_status_comes_from_git_not_from_the_hunks():
    """A file whose every line changed is still M; one created empty is
    still A."""
    files = parse_diff_files(DIFF)
    assert [(f["status"], f["path"].rsplit("/", 1)[-1]) for f in files] == [
        ("M", "Makefile"),
        ("A", "patch-CMakeLists_txt"),
        ("D", "patch-old"),
    ]


def test_counts_are_per_file():
    files = {f["path"].rsplit("/", 1)[-1]: f for f in parse_diff_files(DIFF)}
    assert (files["Makefile"]["added"], files["Makefile"]["removed"]) == (1, 1)
    assert files["patch-old"]["removed"] == 1


def test_a_patch_payload_line_is_content_not_a_header():
    """These ports' payloads ARE patches, so a line reading '++++' is an
    added line whose content starts with +++."""
    added = {f["path"].rsplit("/", 1)[-1]: f["added"]
             for f in parse_diff_files(DIFF)}
    assert added["patch-CMakeLists_txt"] == 2


def test_each_file_keeps_its_own_diff_for_its_own_pane():
    files = parse_diff_files(DIFF)
    assert files[0]["raw"].startswith("diff --git a/ports/devel/foo/Makefile")
    assert "patch-CMakeLists_txt" not in files[0]["raw"]


def test_a_file_is_attributed_to_the_attempt_that_first_wrote_it():
    """The tree is cumulative: the LAST toucher is nearly always the
    current attempt and says nothing."""
    calls = [
        {"attempt": 1, "args": {"name": "files/patch-CMakeLists_txt"}},
        {"attempt": 2, "args": {"path": "Makefile"}},
        {"attempt": 3, "args": {"name": "files/patch-CMakeLists_txt"}},
    ]
    tree = working_tree(DIFF, calls)
    by_name = {f["path"].rsplit("/", 1)[-1]: f for f in tree["files"]}
    assert by_name["patch-CMakeLists_txt"]["attempt"] == 1
    assert by_name["Makefile"]["attempt"] == 2


def test_a_file_nothing_named_says_so_rather_than_guessing():
    tree = working_tree(DIFF, [{"attempt": 1, "args": {"path": "other"}}])
    assert tree["files"][0]["attempt"] is None


def test_arguments_match_on_the_tail_the_two_sides_agree_on():
    """The model passes whatever relative path it chose; the diff carries
    the full in-tree path."""
    for arg in ("Makefile", "./Makefile", "devel/foo/Makefile"):
        tree = working_tree(DIFF, [{"attempt": 7, "args": {"p": arg}}])
        assert tree["files"][0]["attempt"] == 7, arg


def test_the_file_being_written_right_now_is_marked():
    tree = working_tree(DIFF, [], live_path="files/patch-CMakeLists_txt")
    live = [f for f in tree["files"] if f["live"]]
    assert len(live) == 1
    assert live[0]["path"].endswith("patch-CMakeLists_txt")


def test_totals_are_the_band_s_headline():
    tree = working_tree(DIFF, [])
    assert tree["n_files"] == 3
    assert tree["added"] == 3
    assert tree["removed"] == 2


def test_no_changes_yet_is_a_real_state():
    """A job that is still reading has changed nothing, and that is not
    the same as having no diff to show."""
    tree = working_tree("", [])
    assert tree["empty"] is True
    assert tree["files"] == []


# --- the band on the page --------------------------------------------------


def test_the_job_page_renders_the_band_from_the_rescued_diff(tmp_path):
    """A finished job needs no new data: the rescue path already filed
    analysis/rescued/<job_id>.diff, per job and durable."""
    import sqlite3  # noqa: PLC0415

    import pytest  # noqa: PLC0415
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from dportsv3 import settings  # noqa: PLC0415
    from dportsv3.db.schema import init_db  # noqa: PLC0415
    from dportsv3.tracker.server import create_app  # noqa: PLC0415

    blob = tmp_path / "rescued.diff"
    blob.write_text(DIFF)
    db = tmp_path / "state.db"
    c = sqlite3.connect(str(db)); c.row_factory = sqlite3.Row; init_db(c)
    now = "2026-09-23T10:00:00+00:00"
    c.execute(
        "INSERT INTO bundles (bundle_id, run_id, origin, flavor, ts_utc, "
        "result, path, last_seen_at, target) VALUES "
        "('b1','r1','devel/foo','',?, 'failed','/p',?, '@2026Q3')",
        (now, now))
    c.execute(
        "INSERT INTO jobs (job_id, state, type, origin, flavor, bundle_dir, "
        "created_ts_utc, path, last_seen_at, target, bundle_id) VALUES "
        "('j1','done','patch','devel/foo','','',?,'',?,'@2026Q3','b1')",
        (now, now))
    c.execute(
        "INSERT INTO artifact_refs (bundle_id, relpath, backend, sha256, "
        "fs_path, kind, size, created_at) VALUES "
        "('b1','analysis/rescued/j1.diff','fs','x',?, 'text', ?, ?)",
        (str(blob), blob.stat().st_size, now))
    # One write-tool row, so a file can be attributed and the rest cannot.
    c.execute(
        "INSERT INTO activity_log (ts, job_id, stage, message, extra_json) "
        "VALUES (?, 'j1', 'tool:make_patch', 'made', "
        "'{\"attempt\": 2, \"args\": {\"name\": \"files/patch-CMakeLists_txt\"}}')",
        (now,))
    c.commit(); c.close()

    with TestClient(create_app(db)) as client:
        body = client.get("/agentic/jobs/j1").text
    assert 'id="worktree"' in body
    assert "3 files changed" in body
    assert "first written in attempt 2" in body
    assert "attempt not recorded" in body      # the other two
    assert "as the job left it" in body        # not a live read
    # The diff is rendered by the reader's own renderer, not a second one.
    assert 'class="diff-view"' in body


def test_a_job_with_nothing_changed_gets_no_band(tmp_path):
    import sqlite3  # noqa: PLC0415

    import pytest  # noqa: PLC0415
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from dportsv3.db.schema import init_db  # noqa: PLC0415
    from dportsv3.tracker.server import create_app  # noqa: PLC0415

    db = tmp_path / "state.db"
    c = sqlite3.connect(str(db)); c.row_factory = sqlite3.Row; init_db(c)
    now = "2026-09-23T10:00:00+00:00"
    c.execute(
        "INSERT INTO jobs (job_id, state, type, origin, flavor, bundle_dir, "
        "created_ts_utc, path, last_seen_at) VALUES "
        "('j2','done','patch','devel/foo','','',?,'',?)", (now, now))
    c.commit(); c.close()
    with TestClient(create_app(db)) as client:
        assert 'id="worktree"' not in client.get("/agentic/jobs/j2").text


def test_the_job_links_through_to_its_occurrence(tmp_path):
    """bundle_id was in the schema and joined the other way for the
    cockpit; this page printed a directory as text (poly-qqx9.7)."""
    import sqlite3  # noqa: PLC0415

    import pytest  # noqa: PLC0415
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from dportsv3.db.schema import init_db  # noqa: PLC0415
    from dportsv3.tracker.server import create_app  # noqa: PLC0415

    db = tmp_path / "state.db"
    c = sqlite3.connect(str(db)); c.row_factory = sqlite3.Row; init_db(c)
    now = "2026-09-23T10:00:00+00:00"
    c.execute(
        "INSERT INTO bundles (bundle_id, run_id, origin, flavor, ts_utc, "
        "result, path, last_seen_at) VALUES "
        "('b7','r1','devel/foo','',?, 'failed','/p',?)", (now, now))
    c.execute(
        "INSERT INTO jobs (job_id, state, type, origin, flavor, bundle_dir, "
        "created_ts_utc, path, last_seen_at, bundle_id) VALUES "
        "('j7','done','patch','devel/foo','','/var/b/j7',?,'',?,'b7')",
        (now, now))
    c.commit(); c.close()
    with TestClient(create_app(db)) as client:
        body = client.get("/agentic/jobs/j7").text
    assert "Occurrence" in body
    assert "/agentic/bundles/b7" in body
