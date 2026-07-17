import json
import os
import threading
import time

import pytest

from green_tracker.storage import (
    CURRENT_CONFIG_VERSION,
    FIELDNAMES,
    RECENT_TAGS_MAX,
    UNDO_STACK_DEPTH,
    SessionRow,
    can_undo,
    clear_undo_stack,
    commit_session,
    delete_session,
    format_tag_total,
    get_config_path,
    get_csv_path,
    load_config,
    load_sessions,
    merge_into,
    rename_session,
    rename_tag,
    retag_session,
    save_config,
    save_sessions,
    set_minutes_for_tag_date,
    tag_totals,
    undo,
    undo_depth,
)


def _tmp_path_for(path):
    """The sibling .tmp the atomic writer stages through."""
    return path.with_suffix(path.suffix + ".tmp")


# ---------------------------------------------------------------------------
# format_tag_total
# ---------------------------------------------------------------------------

class TestFormatTagTotal:
    def test_zero(self):
        assert format_tag_total(0) == "00d 00h"

    def test_one_hour(self):
        assert format_tag_total(60) == "00d 01h"

    def test_23_hours(self):
        assert format_tag_total(23 * 60) == "00d 23h"

    def test_one_day_exactly(self):
        assert format_tag_total(24 * 60) == "01d 00h"

    def test_spec_example_50h(self):
        # spec: 50h → 02d 02h
        assert format_tag_total(50 * 60) == "02d 02h"

    def test_minutes_are_truncated(self):
        # 90 minutes = 1h30m → shown as 00d 01h
        assert format_tag_total(90) == "00d 01h"

    def test_days_grow_past_99(self):
        assert format_tag_total(100 * 24 * 60) == "100d 00h"

    def test_mixed_days_and_hours(self):
        # 3 days 5 hours = 72h + 5h = 77h = 4620 min
        assert format_tag_total(77 * 60) == "03d 05h"


# ---------------------------------------------------------------------------
# tag_totals
# ---------------------------------------------------------------------------

class TestTagTotals:
    def test_single_tag_single_session(self):
        rows = [SessionRow("2026-05-28", "email", "email-2026-05-28", 120)]
        assert tag_totals(rows) == {"email": "00d 02h"}

    def test_single_tag_multiple_sessions(self):
        rows = [
            SessionRow("2026-05-28", "coding", "coding-2026-05-28", 120),
            SessionRow("2026-05-29", "coding", "coding-2026-05-29", 120),
        ]
        assert tag_totals(rows) == {"coding": "00d 04h"}

    def test_multiple_tags(self):
        rows = [
            SessionRow("2026-05-28", "email", "email-2026-05-28", 60),
            SessionRow("2026-05-28", "coding", "coding-2026-05-28", 120),
        ]
        result = tag_totals(rows)
        assert result["email"] == "00d 01h"
        assert result["coding"] == "00d 02h"

    def test_empty_list(self):
        assert tag_totals([]) == {}


# ---------------------------------------------------------------------------
# merge_into
# ---------------------------------------------------------------------------

class TestMergeInto:
    def test_appends_when_no_match(self):
        rows = [SessionRow("2026-05-28", "email", "email-2026-05-28", 60)]
        new = SessionRow("2026-05-28", "coding", "coding-2026-05-28", 30)
        result = merge_into(rows, new)
        assert len(result) == 2

    def test_adds_minutes_on_same_tag_and_date(self):
        rows = [SessionRow("2026-05-28", "email", "email-2026-05-28", 60)]
        new = SessionRow("2026-05-28", "email", "email-2026-05-28", 30)
        result = merge_into(rows, new)
        assert len(result) == 1
        assert result[0].minutes == 90

    def test_keeps_existing_session_name_on_merge(self):
        rows = [SessionRow("2026-05-28", "email", "my-special-name", 60)]
        new = SessionRow("2026-05-28", "email", "email-2026-05-28", 30)
        result = merge_into(rows, new)
        assert result[0].session_name == "my-special-name"

    def test_same_tag_different_date_no_merge(self):
        rows = [SessionRow("2026-05-28", "email", "email-2026-05-28", 60)]
        new = SessionRow("2026-05-29", "email", "email-2026-05-29", 30)
        result = merge_into(rows, new)
        assert len(result) == 2

    def test_same_date_different_tag_no_merge(self):
        rows = [SessionRow("2026-05-28", "email", "email-2026-05-28", 60)]
        new = SessionRow("2026-05-28", "coding", "coding-2026-05-28", 30)
        result = merge_into(rows, new)
        assert len(result) == 2

    def test_merge_into_empty_list(self):
        result = merge_into([], SessionRow("2026-05-28", "email", "email-2026-05-28", 60))
        assert len(result) == 1



# ---------------------------------------------------------------------------
# CSV round-trip
# ---------------------------------------------------------------------------

class TestCSVIO:
    def test_save_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        rows = [
            SessionRow("2026-05-28", "email", "email-2026-05-28", 60),
            SessionRow("2026-05-29", "coding", "coding-2026-05-29", 120),
        ]
        save_sessions(rows)
        assert load_sessions() == rows

    def test_load_returns_empty_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        assert load_sessions() == []

    def test_save_creates_directory(self, tmp_path, monkeypatch):
        target = tmp_path / "nested" / "dir"
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(target))
        save_sessions([SessionRow("2026-05-28", "email", "email-2026-05-28", 60)])
        assert (target / "sessions.csv").exists()

    def test_minutes_stored_as_integer(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        save_sessions([SessionRow("2026-05-28", "email", "email-2026-05-28", 75)])
        loaded = load_sessions()
        assert loaded[0].minutes == 75
        assert isinstance(loaded[0].minutes, int)


# ---------------------------------------------------------------------------
# Crash-safe CSV write (brief §10)
# ---------------------------------------------------------------------------

class TestAtomicSessionWrite:
    def test_write_is_byte_identical_to_a_plain_csv_writer(self, tmp_path):
        # The atomic path serialises through StringIO rather than the
        # file object. csv defaults to \r\n line endings, so a StringIO
        # built without newline="" would silently rewrite every line in
        # the user's file the first time they saved.
        import csv as _csv
        rows = [
            SessionRow("2026-05-28", "email", "e-1", 60),
            SessionRow("2026-05-29", "coding", "c-1", 120),
        ]
        save_sessions(rows)

        reference = tmp_path / "reference.csv"
        with reference.open("w", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=FIELDNAMES)
            w.writeheader()
            for r in rows:
                w.writerow(r._asdict())

        assert get_csv_path().read_bytes() == reference.read_bytes()
        assert load_sessions() == rows

    def test_failed_write_leaves_original_intact(self, monkeypatch):
        # The exact failure the old open("w") path could not survive: it
        # truncated the file on open, so an interruption before the rows
        # were flushed destroyed the history. os.replace() cannot leave
        # a partial file — either the rename lands or it doesn't.
        original = [SessionRow("2026-05-28", "email", "e-1", 60)]
        save_sessions(original)
        before = get_csv_path().read_bytes()

        def boom(*args, **kwargs):
            raise OSError("simulated failure mid-write")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            save_sessions([SessionRow("2026-05-29", "coding", "c-1", 999)])

        assert get_csv_path().read_bytes() == before
        assert load_sessions() == original

    def test_no_tmp_left_behind_on_success(self):
        save_sessions([SessionRow("2026-05-28", "email", "e-1", 60)])
        assert not _tmp_path_for(get_csv_path()).exists()

    def test_concurrent_mutations_do_not_lose_changes(self, monkeypatch):
        # The load-modify-save race: two threads both load the same rows,
        # both apply their own change to that snapshot, and the later
        # save writes a version derived from pre-change state — silently
        # dropping the earlier thread's work. Locking only the file write
        # does not help; the lock has to span the load too.
        #
        # merge_into runs between commit_session's load and its save, so
        # stalling it widens the existing window to something a test can
        # observe rather than hit by luck. Without the wide lock every
        # thread loads the same empty file and the last save wins, so
        # exactly one row survives instead of eight.
        import green_tracker.storage as st

        real_merge_into = st.merge_into

        def slow_merge_into(sessions, new_row):
            time.sleep(0.02)
            return real_merge_into(sessions, new_row)

        monkeypatch.setattr(st, "merge_into", slow_merge_into)

        def add(n):
            commit_session(f"tag{n}", "2026-05-28", 10)

        threads = [threading.Thread(target=add, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = load_sessions()
        assert sorted(r.tag for r in rows) == [f"tag{n}" for n in range(8)]
        assert all(r.minutes == 10 for r in rows)

    def test_concurrent_writers_do_not_collide(self):
        # The web editor saves on Flask's thread while the widget saves
        # on Qt's. The tmp path is derived from the target, so without
        # serialisation both writers stage through the same file: the
        # first rename moves it away and the second dies on a tmp that
        # no longer exists.
        errors = []

        def hammer(n):
            for i in range(20):
                try:
                    save_sessions([
                        SessionRow("2026-05-28", f"t{n}", f"s{n}-{i}", i + 1),
                    ])
                except Exception as exc:      # noqa: BLE001 - reported below
                    errors.append(exc)

        threads = [
            threading.Thread(target=hammer, args=(n,)) for n in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(load_sessions()) == 1      # last writer wins, intact
        assert not _tmp_path_for(get_csv_path()).exists()

    def test_no_tmp_left_behind_on_failure(self, monkeypatch):
        save_sessions([SessionRow("2026-05-28", "email", "e-1", 60)])

        def boom(*args, **kwargs):
            raise OSError("simulated failure mid-write")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            save_sessions([SessionRow("2026-05-29", "coding", "c-1", 999)])
        assert not _tmp_path_for(get_csv_path()).exists()


# ---------------------------------------------------------------------------
# Undo stack (spec §5)
# ---------------------------------------------------------------------------

class TestUndo:
    def test_nothing_to_undo_on_empty_stack(self):
        assert can_undo() is False
        assert undo() is False

    def test_undo_restores_previous_csv(self):
        first = [SessionRow("2026-05-28", "email", "e-1", 60)]
        save_sessions(first)
        save_sessions([SessionRow("2026-05-29", "coding", "c-1", 120)])
        assert undo() is True
        assert load_sessions() == first

    def test_undo_of_first_ever_save_removes_the_file(self):
        # The pre-write state had no CSV. Restoring must delete it, not
        # leave a header-only file that reads as "a real but empty
        # history".
        assert not get_csv_path().exists()
        save_sessions([SessionRow("2026-05-28", "email", "e-1", 60)])
        assert undo() is True
        assert not get_csv_path().exists()
        assert load_sessions() == []

    def test_undo_is_lifo_to_full_depth(self):
        # Spec §5's stated test: undo up to 8 deep, each step matching
        # the state before that write.
        states = []
        for i in range(UNDO_STACK_DEPTH):
            states.append(load_sessions())
            save_sessions([
                SessionRow("2026-05-28", f"tag{i}", f"s{i}", (i + 1) * 10),
            ])
        assert undo_depth() == UNDO_STACK_DEPTH
        for expected in reversed(states):
            assert undo() is True
            assert load_sessions() == expected
        assert can_undo() is False

    def test_stack_evicts_oldest_beyond_depth(self):
        for i in range(UNDO_STACK_DEPTH + 5):
            save_sessions([
                SessionRow("2026-05-28", f"tag{i}", f"s{i}", i + 1),
            ])
        assert undo_depth() == UNDO_STACK_DEPTH

    def test_undo_does_not_stack_itself(self):
        # No redo: an undo must not push its own snapshot, or it would
        # be un-undoable and would burn a slot per press.
        save_sessions([SessionRow("2026-05-28", "email", "e-1", 60)])
        save_sessions([SessionRow("2026-05-29", "coding", "c-1", 120)])
        depth_before = undo_depth()
        undo()
        assert undo_depth() == depth_before - 1

    def test_every_mutating_entry_point_is_undoable(self):
        # Spec §2d: all CSV mutations push a snapshot, whatever the
        # entry point. They all funnel through save_sessions, so this
        # guards that funnel staying intact.
        base = [
            SessionRow("2026-05-28", "email", "e-1", 60),
            SessionRow("2026-05-28", "coding", "c-1", 30),
        ]
        for mutate in (
            lambda: commit_session("email", "2026-05-28", 15),
            lambda: rename_session("e-1", "renamed"),
            lambda: delete_session("c-1"),
            lambda: retag_session("e-1", "admin"),
            lambda: rename_tag("email", "work"),
            lambda: set_minutes_for_tag_date("email", "2026-05-28", 99),
        ):
            save_sessions(base)
            clear_undo_stack()
            mutate()
            assert undo_depth() == 1, mutate
            assert undo() is True
            assert load_sessions() == base, mutate

    def test_noop_mutations_do_not_consume_the_stack(self):
        # rename/delete/retag return early without writing when there's
        # nothing to do, so they must not burn an undo slot.
        save_sessions([SessionRow("2026-05-28", "email", "e-1", 60)])
        clear_undo_stack()
        assert rename_session("nosuch", "x") is False
        assert delete_session("nosuch") is False
        assert retag_session("nosuch", "x") is False
        assert rename_tag("nosuch", "x") is False
        assert undo_depth() == 0

    def test_failed_write_does_not_push_a_snapshot(self, monkeypatch):
        save_sessions([SessionRow("2026-05-28", "email", "e-1", 60)])
        clear_undo_stack()

        def boom(*args, **kwargs):
            raise OSError("simulated failure mid-write")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            save_sessions([SessionRow("2026-05-29", "coding", "c-1", 999)])
        assert undo_depth() == 0

    def test_undo_restore_is_atomic(self, monkeypatch):
        # An interrupted undo must not damage the file it's repairing.
        save_sessions([SessionRow("2026-05-28", "email", "e-1", 60)])
        current = [SessionRow("2026-05-29", "coding", "c-1", 120)]
        save_sessions(current)
        before = get_csv_path().read_bytes()

        def boom(*args, **kwargs):
            raise OSError("simulated failure mid-restore")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            undo()
        assert get_csv_path().read_bytes() == before
        assert load_sessions() == current
        assert not _tmp_path_for(get_csv_path()).exists()

    def test_snapshots_are_byte_exact(self):
        # Snapshot is raw bytes, not re-serialised rows, so CRLF and any
        # hand-edited formatting survive a round trip.
        save_sessions([SessionRow("2026-05-28", "email", "e-1", 60)])
        before = get_csv_path().read_bytes()
        save_sessions([SessionRow("2026-05-29", "coding", "c-1", 120)])
        undo()
        assert get_csv_path().read_bytes() == before


# ---------------------------------------------------------------------------
# Config migration (spec §1: config_version v2 -> v3)
# ---------------------------------------------------------------------------

class TestConfigMigration:
    def _write(self, tmp_path, config):
        """Drop a raw config.json on disk, bypassing save_config."""
        (tmp_path / "config.json").write_text(
            json.dumps(config), encoding="utf-8",
        )

    def test_fresh_install_is_stamped_at_current_version(
        self, tmp_path, monkeypatch,
    ):
        # No config.json at all. The returned config must carry a version
        # stamp, else the next load mistakes it for v1 and applies the
        # v1->v2 size remap to whatever size the user just picked.
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        config = load_config()
        assert config["config_version"] == CURRENT_CONFIG_VERSION
        assert config["recent_tags"] == []
        assert config["tag_schemes"] == {}

    def test_fresh_install_does_not_remap_size_on_next_launch(
        self, tmp_path, monkeypatch,
    ):
        # Regression: fresh install -> pick "medium" -> relaunch used to
        # yield "large" (v1->v2 remap applied to an unstamped config).
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        config = load_config()
        config["widget_size"] = "medium"
        save_config(config)
        assert load_config()["widget_size"] == "medium"

    def test_fresh_install_writes_no_file(self, tmp_path, monkeypatch):
        # load_config stays a pure read; the app's first save persists.
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        load_config()
        assert not get_config_path().exists()

    def test_v2_gains_new_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        self._write(tmp_path, {"config_version": 2, "widget_size": "small"})
        config = load_config()
        assert config["config_version"] == 3
        assert config["recent_tags"] == []   # no CSV history to seed from
        assert config["tag_schemes"] == {}
        # v2->v3 must not touch size — that remap already ran.
        assert config["widget_size"] == "small"

    def test_v1_cascades_through_both_steps(self, tmp_path, monkeypatch):
        # A genuine v1 config on disk: unversioned, pre-remap size.
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        self._write(tmp_path, {"widget_size": "medium"})
        config = load_config()
        assert config["config_version"] == 3
        assert config["widget_size"] == "large"   # v1->v2 remap still applies
        assert config["recent_tags"] == []
        assert config["tag_schemes"] == {}

    def test_migration_is_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        self._write(tmp_path, {"config_version": 2})
        load_config()
        on_disk = json.loads(get_config_path().read_text(encoding="utf-8"))
        assert on_disk["config_version"] == 3
        assert on_disk["recent_tags"] == []
        assert on_disk["tag_schemes"] == {}

    def test_migration_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        self._write(tmp_path, {"config_version": 2})
        first = load_config()
        assert load_config() == first

    def test_existing_v3_data_is_not_clobbered(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        self._write(tmp_path, {
            "config_version": 3,
            "recent_tags": ["work", "admin"],
            "tag_schemes": {"work": "Earthen"},
        })
        config = load_config()
        assert config["recent_tags"] == ["work", "admin"]
        assert config["tag_schemes"] == {"work": "Earthen"}

    def test_seeds_recent_tags_from_csv_by_recency(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        save_sessions([
            SessionRow("2026-05-20", "old", "old-1", 60),
            SessionRow("2026-05-28", "recent", "recent-1", 60),
            SessionRow("2026-05-24", "middle", "middle-1", 60),
        ])
        self._write(tmp_path, {"config_version": 2})
        assert load_config()["recent_tags"] == ["recent", "middle", "old"]

    def test_seed_dedupes_tag_to_its_latest_date(self, tmp_path, monkeypatch):
        # "old" appears twice; its newest row must set its MRU position,
        # and it must appear exactly once.
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        save_sessions([
            SessionRow("2026-05-01", "old", "old-1", 60),
            SessionRow("2026-05-10", "other", "other-1", 60),
            SessionRow("2026-05-30", "old", "old-2", 60),
        ])
        self._write(tmp_path, {"config_version": 2})
        assert load_config()["recent_tags"] == ["old", "other"]

    def test_seed_is_capped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        save_sessions([
            SessionRow(f"2026-05-{day:02d}", f"tag{day}", f"s{day}", 60)
            for day in range(1, 29)          # 28 distinct tags > the cap
        ])
        self._write(tmp_path, {"config_version": 2})
        recent = load_config()["recent_tags"]
        assert len(recent) == RECENT_TAGS_MAX
        assert recent[0] == "tag28"          # newest survives the cap
        assert "tag1" not in recent          # oldest is dropped

    def test_seed_empty_without_csv_history(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        self._write(tmp_path, {"config_version": 2})
        assert load_config()["recent_tags"] == []

    def test_seed_survives_corrupt_csv(self, tmp_path, monkeypatch):
        # load_config runs at startup; a damaged CSV must cost an empty
        # MRU, not prevent the app from launching.
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        (tmp_path / "sessions.csv").write_text(
            "date,tag,session_name,minutes\n2026-05-28,work,w,notaninteger\n",
            encoding="utf-8",
        )
        self._write(tmp_path, {"config_version": 2})
        config = load_config()
        assert config["recent_tags"] == []
        assert config["config_version"] == 3   # bump still happens

    def test_fresh_install_seeds_from_csv(self, tmp_path, monkeypatch):
        # No config.json but a real sessions.csv = history, not a new user.
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        save_sessions([SessionRow("2026-05-28", "work", "work-1", 60)])
        assert load_config()["recent_tags"] == ["work"]

    def test_existing_recent_tags_skip_the_seed(self, tmp_path, monkeypatch):
        # An explicit list wins over CSV history, even a stale one.
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        save_sessions([SessionRow("2026-05-28", "fromcsv", "c-1", 60)])
        self._write(tmp_path, {"config_version": 2, "recent_tags": []})
        assert load_config()["recent_tags"] == []

    def test_unrelated_keys_survive_migration(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        self._write(tmp_path, {
            "config_version": 2,
            "color_scheme": "Twilight",
            "tag_color_overrides": {"work": "#aabbcc"},
            "last_tag": "work",
        })
        config = load_config()
        assert config["color_scheme"] == "Twilight"
        assert config["tag_color_overrides"] == {"work": "#aabbcc"}
        assert config["last_tag"] == "work"


# ---------------------------------------------------------------------------
# commit_session
# ---------------------------------------------------------------------------

class TestCommitSession:
    def test_creates_new_entry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        commit_session("email", "2026-05-28", 60)
        rows = load_sessions()
        assert len(rows) == 1
        assert rows[0].tag == "email"
        assert rows[0].minutes == 60
        assert rows[0].session_name == "email-2026-05-28"

    def test_merges_same_tag_and_date(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        commit_session("email", "2026-05-28", 60)
        commit_session("email", "2026-05-28", 30)
        rows = load_sessions()
        assert len(rows) == 1
        assert rows[0].minutes == 90

    def test_does_not_merge_different_date(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        commit_session("email", "2026-05-28", 60)
        commit_session("email", "2026-05-29", 30)
        assert len(load_sessions()) == 2

    def test_custom_session_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        commit_session("email", "2026-05-28", 60, session_name="board-prep")
        assert load_sessions()[0].session_name == "board-prep"

    def test_auto_name_format(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        commit_session("deep-work", "2026-05-28", 90)
        assert load_sessions()[0].session_name == "deep-work-2026-05-28"


# ---------------------------------------------------------------------------
# rename_session / delete_session
# ---------------------------------------------------------------------------

class TestRenameSession:
    def test_renames_existing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        commit_session("email", "2026-05-28", 60)
        assert rename_session("email-2026-05-28", "important-email") is True
        assert load_sessions()[0].session_name == "important-email"

    def test_returns_false_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        commit_session("email", "2026-05-28", 60)
        assert rename_session("does-not-exist", "new-name") is False

    def test_does_not_touch_other_rows(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        commit_session("email", "2026-05-28", 60)
        commit_session("coding", "2026-05-28", 120)
        rename_session("email-2026-05-28", "renamed")
        rows = {r.session_name for r in load_sessions()}
        assert "renamed" in rows
        assert "coding-2026-05-28" in rows


class TestDeleteSession:
    def test_deletes_existing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        commit_session("email", "2026-05-28", 60)
        commit_session("coding", "2026-05-28", 120)
        assert delete_session("email-2026-05-28") is True
        rows = load_sessions()
        assert len(rows) == 1
        assert rows[0].tag == "coding"

    def test_returns_false_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        assert delete_session("nonexistent") is False

    def test_deletes_from_empty_csv(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path))
        assert delete_session("anything") is False
