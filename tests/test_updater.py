from datetime import date, timedelta

import pytest

# updater imports PySide6 (QThread) at module load; skip cleanly without it.
pytest.importorskip("PySide6")

from green_tracker import updater  # noqa: E402


def _today():
    return date.today().isoformat()


def _days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# Version parsing / comparison
# ---------------------------------------------------------------------------

class TestVersionParsing:
    def test_strips_leading_v(self):
        assert updater._parse_version("v0.2.1") == (0, 2, 1)
        assert updater._parse_version("V1.0") == (1, 0)
        assert updater._parse_version("0.2.1") == (0, 2, 1)

    def test_stray_suffix_reads_leading_digits(self):
        assert updater._parse_version("0.2.1-beta") == (0, 2, 1)
        assert updater._parse_version("1.2rc3") == (1, 2)  # part "2rc3" -> 2

    def test_nondigit_part_is_zero(self):
        assert updater._parse_version("1.x.3") == (1, 0, 3)

    def test_is_newer(self):
        assert updater.is_newer("0.2.1", "0.2.0") is True
        assert updater.is_newer("v0.3.0", "0.2.9") is True
        assert updater.is_newer("1.0.0", "0.9.9") is True

    def test_is_not_newer_when_same_or_older(self):
        assert updater.is_newer("0.2.0", "0.2.0") is False
        assert updater.is_newer("0.2.0", "0.2.1") is False
        # Different lengths that mean the same version don't count as newer.
        assert updater.is_newer("0.2", "0.2.0") is False
        assert updater.is_newer("0.2.0", "0.2") is False


# ---------------------------------------------------------------------------
# Daily-check bookkeeping
# ---------------------------------------------------------------------------

class TestDailyCheck:
    def test_should_check_when_never_checked(self):
        assert updater.should_check_today({}) is True

    def test_should_not_check_twice_same_day(self):
        cfg = {}
        updater.record_check_result(cfg, "9.9.9")
        assert updater.should_check_today(cfg) is False

    def test_should_check_again_next_day(self):
        cfg = {"update_check": {"last_checked": _days_ago(1)}}
        assert updater.should_check_today(cfg) is True

    def test_record_stamps_date_even_on_failure(self):
        # A None result (offline) still stamps last_checked, so we wait
        # until tomorrow rather than hammering every launch.
        cfg = {}
        updater.record_check_result(cfg, None)
        assert cfg["update_check"]["last_checked"] == _today()
        assert cfg["update_check"]["latest_version"] is None

    def test_record_updates_version_only_when_present(self):
        cfg = {"update_check": {"latest_version": "0.2.0"}}
        updater.record_check_result(cfg, None)
        assert cfg["update_check"]["latest_version"] == "0.2.0"  # unchanged
        updater.record_check_result(cfg, "0.3.0")
        assert cfg["update_check"]["latest_version"] == "0.3.0"


# ---------------------------------------------------------------------------
# pending_update / dismiss
# ---------------------------------------------------------------------------

class TestPending:
    def test_pending_when_newer_known(self):
        cfg = {"update_check": {"latest_version": "0.3.0"}}
        assert updater.pending_update(cfg, "0.2.0") == "0.3.0"

    def test_no_pending_when_not_newer(self):
        cfg = {"update_check": {"latest_version": "0.2.0"}}
        assert updater.pending_update(cfg, "0.2.0") is None

    def test_no_pending_when_none_known(self):
        assert updater.pending_update({}, "0.2.0") is None

    def test_dismiss_roundtrip(self):
        cfg = {}
        assert updater.is_dismissed(cfg, "0.3.0") is False
        updater.dismiss(cfg, "0.3.0")
        assert updater.is_dismissed(cfg, "0.3.0") is True
        # dismissing one version doesn't dismiss another
        assert updater.is_dismissed(cfg, "0.4.0") is False


# ---------------------------------------------------------------------------
# Popup throttle (independent of the daily check)
# ---------------------------------------------------------------------------

class TestPopupThrottle:
    def test_shows_when_never_shown(self):
        assert updater.should_show_popup({}, "0.3.0") is True

    def test_suppressed_for_dismissed_version(self):
        cfg = {}
        updater.dismiss(cfg, "0.3.0")
        assert updater.should_show_popup(cfg, "0.3.0") is False
        # but a newer, undismissed version still shows
        assert updater.should_show_popup(cfg, "0.4.0") is True

    def test_suppressed_within_min_interval(self):
        cfg = {"update_check": {"last_popup_shown": _days_ago(1)}}
        assert updater.POPUP_MIN_INTERVAL_DAYS >= 2
        assert updater.should_show_popup(cfg, "0.3.0") is False

    def test_shows_again_after_min_interval(self):
        cfg = {"update_check": {
            "last_popup_shown": _days_ago(updater.POPUP_MIN_INTERVAL_DAYS + 1),
        }}
        assert updater.should_show_popup(cfg, "0.3.0") is True

    def test_record_popup_shown_stamps_today(self):
        cfg = {}
        updater.record_popup_shown(cfg)
        assert cfg["update_check"]["last_popup_shown"] == _today()
        # and it now throttles
        assert updater.should_show_popup(cfg, "0.3.0") is False


# ---------------------------------------------------------------------------
# Network fetch — must fail completely silently
# ---------------------------------------------------------------------------

class TestFetch:
    def test_returns_none_on_network_error(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("no network")
        monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
        assert updater.fetch_latest_version() is None  # never raises

    def test_returns_none_on_bad_json(self, monkeypatch):
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"not json"
        monkeypatch.setattr(updater.urllib.request, "urlopen",
                            lambda *a, **k: FakeResp())
        assert updater.fetch_latest_version() is None

    def test_parses_tag_name_and_strips_v(self, monkeypatch):
        import json as _json

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return _json.dumps({"tag_name": "v0.9.9"}).encode()
        monkeypatch.setattr(updater.urllib.request, "urlopen",
                            lambda *a, **k: FakeResp())
        assert updater.fetch_latest_version() == "0.9.9"
