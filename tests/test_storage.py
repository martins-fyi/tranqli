import pytest

from green_tracker.storage import (
    SessionRow,
    commit_session,
    delete_session,
    format_tag_total,
    load_sessions,
    merge_into,
    rename_session,
    save_sessions,
    tag_totals,
)


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
        monkeypatch.setenv("GREEN_TRACKER_DATA_DIR", str(tmp_path))
        rows = [
            SessionRow("2026-05-28", "email", "email-2026-05-28", 60),
            SessionRow("2026-05-29", "coding", "coding-2026-05-29", 120),
        ]
        save_sessions(rows)
        assert load_sessions() == rows

    def test_load_returns_empty_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GREEN_TRACKER_DATA_DIR", str(tmp_path))
        assert load_sessions() == []

    def test_save_creates_directory(self, tmp_path, monkeypatch):
        target = tmp_path / "nested" / "dir"
        monkeypatch.setenv("GREEN_TRACKER_DATA_DIR", str(target))
        save_sessions([SessionRow("2026-05-28", "email", "email-2026-05-28", 60)])
        assert (target / "sessions.csv").exists()

    def test_minutes_stored_as_integer(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GREEN_TRACKER_DATA_DIR", str(tmp_path))
        save_sessions([SessionRow("2026-05-28", "email", "email-2026-05-28", 75)])
        loaded = load_sessions()
        assert loaded[0].minutes == 75
        assert isinstance(loaded[0].minutes, int)


# ---------------------------------------------------------------------------
# commit_session
# ---------------------------------------------------------------------------

class TestCommitSession:
    def test_creates_new_entry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GREEN_TRACKER_DATA_DIR", str(tmp_path))
        commit_session("email", "2026-05-28", 60)
        rows = load_sessions()
        assert len(rows) == 1
        assert rows[0].tag == "email"
        assert rows[0].minutes == 60
        assert rows[0].session_name == "email-2026-05-28"

    def test_merges_same_tag_and_date(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GREEN_TRACKER_DATA_DIR", str(tmp_path))
        commit_session("email", "2026-05-28", 60)
        commit_session("email", "2026-05-28", 30)
        rows = load_sessions()
        assert len(rows) == 1
        assert rows[0].minutes == 90

    def test_does_not_merge_different_date(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GREEN_TRACKER_DATA_DIR", str(tmp_path))
        commit_session("email", "2026-05-28", 60)
        commit_session("email", "2026-05-29", 30)
        assert len(load_sessions()) == 2

    def test_custom_session_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GREEN_TRACKER_DATA_DIR", str(tmp_path))
        commit_session("email", "2026-05-28", 60, session_name="board-prep")
        assert load_sessions()[0].session_name == "board-prep"

    def test_auto_name_format(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GREEN_TRACKER_DATA_DIR", str(tmp_path))
        commit_session("deep-work", "2026-05-28", 90)
        assert load_sessions()[0].session_name == "deep-work-2026-05-28"


# ---------------------------------------------------------------------------
# rename_session / delete_session
# ---------------------------------------------------------------------------

class TestRenameSession:
    def test_renames_existing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GREEN_TRACKER_DATA_DIR", str(tmp_path))
        commit_session("email", "2026-05-28", 60)
        assert rename_session("email-2026-05-28", "important-email") is True
        assert load_sessions()[0].session_name == "important-email"

    def test_returns_false_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GREEN_TRACKER_DATA_DIR", str(tmp_path))
        commit_session("email", "2026-05-28", 60)
        assert rename_session("does-not-exist", "new-name") is False

    def test_does_not_touch_other_rows(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GREEN_TRACKER_DATA_DIR", str(tmp_path))
        commit_session("email", "2026-05-28", 60)
        commit_session("coding", "2026-05-28", 120)
        rename_session("email-2026-05-28", "renamed")
        rows = {r.session_name for r in load_sessions()}
        assert "renamed" in rows
        assert "coding-2026-05-28" in rows


class TestDeleteSession:
    def test_deletes_existing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GREEN_TRACKER_DATA_DIR", str(tmp_path))
        commit_session("email", "2026-05-28", 60)
        commit_session("coding", "2026-05-28", 120)
        assert delete_session("email-2026-05-28") is True
        rows = load_sessions()
        assert len(rows) == 1
        assert rows[0].tag == "coding"

    def test_returns_false_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GREEN_TRACKER_DATA_DIR", str(tmp_path))
        assert delete_session("nonexistent") is False

    def test_deletes_from_empty_csv(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GREEN_TRACKER_DATA_DIR", str(tmp_path))
        assert delete_session("anything") is False
