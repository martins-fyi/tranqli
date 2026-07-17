from pathlib import Path

import pytest

from green_tracker import storage


@pytest.fixture(autouse=True)
def isolate_data_dir(tmp_path, monkeypatch):
    """Point every test at a throwaway data directory.

    Applied automatically to all tests, including ones that never think
    about storage. Without it, any test that touches a storage read or
    write without overriding the location resolves to the real user data
    directory — %APPDATA%\\Tranqli on Windows, ~/.tranqli elsewhere — and
    a call like save_sessions() overwrites the developer's own session
    history with fixture rows. That is exactly what happened while the
    tests were setting the stale GREEN_TRACKER_DATA_DIR name that
    get_data_dir() no longer reads: they passed on CI-less machines and
    silently wrote to real data on Windows.

    Each override below closes one route to real data:

    - TRANQLI_DATA_DIR is checked first by get_data_dir(), so setting it
      makes every other branch unreachable. This alone covers the normal
      path; the rest defend the paths that ignore it.
    - TRAENKY_DATA_DIR is get_data_dir()'s legacy alias and would be
      honoured if a developer had it exported from a pre-rename install.
    - APPDATA is read directly by _legacy_data_dir(), which deliberately
      ignores the override.
    - Path.home() is _legacy_data_dir()'s last resort. It matters because
      migrate_legacy_data_if_needed() *renames* the legacy directory onto
      the target — so a test calling it with a real home would move an
      actual ~/.traenky into tmp_path and lose it at teardown.

    Tests that need a specific location still just set TRANQLI_DATA_DIR
    themselves; a later setenv in the test body wins over this fixture.
    """
    monkeypatch.setenv("TRANQLI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("TRAENKY_DATA_DIR", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(
        Path, "home", classmethod(lambda cls: tmp_path / "home"),
    )


@pytest.fixture(autouse=True)
def reset_undo_stack():
    """Empty the undo stack around every test.

    The stack is module-level state in storage, so it outlives any single
    test even though isolate_data_dir gives each one a fresh directory.
    Left alone, a snapshot pushed by one test is poppable by the next —
    and worse, it holds CSV bytes belonging to a temp dir that no longer
    exists. Cleared on the way in and out so order can't matter.
    """
    storage.clear_undo_stack()
    yield
    storage.clear_undo_stack()
