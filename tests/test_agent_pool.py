import json
import os
import time

import pytest

from src.agent_pool import (MAX_AGENTS_CAP, STALE_GRACE_SECONDS, acquire_slot,
                            agent_slot, max_agents, release_slot, slot_dir)


def test_max_agents_reads_env_and_clamps(monkeypatch):
    monkeypatch.setenv("HARNESS_MAX_AGENTS", "3")
    assert max_agents() == 3
    monkeypatch.setenv("HARNESS_MAX_AGENTS", "0")
    assert max_agents() == 1
    monkeypatch.setenv("HARNESS_MAX_AGENTS", "9999")
    assert max_agents() == MAX_AGENTS_CAP
    monkeypatch.setenv("HARNESS_MAX_AGENTS", "not-a-number")
    assert max_agents() == 4


def test_slot_is_held_then_released(tmp_path):
    with agent_slot("a", limit=2, root=tmp_path) as slot:
        assert slot.exists()
        assert json.loads(slot.read_text())["pid"] == os.getpid()
    assert not slot.exists()


def test_slots_are_distinct_and_the_cap_blocks(tmp_path):
    a = acquire_slot("a", limit=2, root=tmp_path)
    b = acquire_slot("b", limit=2, root=tmp_path)
    assert a != b

    waited = []
    with pytest.raises(TimeoutError, match="no agent slot free"):
        acquire_slot("c", limit=2, root=tmp_path, timeout=0,
                     sleep=waited.append)

    release_slot(a)
    # Freeing one lets the next caller straight through.
    c = acquire_slot("c", limit=2, root=tmp_path, timeout=0)
    assert c == a


def test_dead_pid_slot_is_reclaimed_after_the_grace_window(tmp_path):
    d = slot_dir(tmp_path)
    d.mkdir(parents=True)
    stale = d / "slot-0.lock"
    stale.write_text(json.dumps({"pid": 999_999_999, "label": "dead"}))

    # Fresh: not reclaimable, so a crashed-looking-but-new slot is never stolen.
    with pytest.raises(TimeoutError):
        acquire_slot("x", limit=1, root=tmp_path, timeout=0)

    old = time.time() - STALE_GRACE_SECONDS - 1
    os.utime(stale, (old, old))
    got = acquire_slot("x", limit=1, root=tmp_path, timeout=0)
    assert json.loads(got.read_text())["label"] == "x"


def test_live_pid_slot_is_never_reclaimed(tmp_path):
    d = slot_dir(tmp_path)
    d.mkdir(parents=True)
    live = d / "slot-0.lock"
    live.write_text(json.dumps({"pid": os.getpid(), "label": "alive"}))
    old = time.time() - STALE_GRACE_SECONDS - 1
    os.utime(live, (old, old))

    with pytest.raises(TimeoutError):
        acquire_slot("x", limit=1, root=tmp_path, timeout=0)


def test_release_of_a_vanished_slot_is_not_an_error(tmp_path):
    slot = acquire_slot("a", limit=1, root=tmp_path)
    slot.unlink()
    release_slot(slot)  # must not raise


def test_default_root_does_not_follow_the_working_directory(tmp_path, monkeypatch):
    """A cap rooted at CWD is not global — a run from elsewhere gets its own pool."""
    from src.agent_pool import default_root

    monkeypatch.delenv("HARNESS_SLOT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    here = default_root()
    monkeypatch.chdir(tmp_path.parent)
    assert default_root() == here
    assert (here / "src" / "agent_pool.py").exists()


def test_slot_root_can_be_overridden(tmp_path, monkeypatch):
    from src.agent_pool import default_root, slot_dir

    monkeypatch.setenv("HARNESS_SLOT_ROOT", str(tmp_path))
    assert default_root() == tmp_path
    assert slot_dir() == tmp_path / ".agent-slots"


def test_a_real_review_and_a_test_run_can_be_kept_apart(tmp_path, monkeypatch):
    """HARNESS_SLOT_ROOT exists so a test suite never draws on the live budget.

    The pool is rooted at the install directory on purpose — a cap tied to the
    CWD is not global — which also means chdir cannot isolate anything from it.
    """
    from src.agent_pool import acquire_slot, default_root

    production = tmp_path / "prod"
    monkeypatch.setenv("HARNESS_SLOT_ROOT", str(production))
    assert default_root() == production

    # Fill the "production" pool completely.
    held = [acquire_slot(f"live-{i}", limit=2) for i in range(2)]
    assert len(held) == 2

    # A different root is unaffected by it.
    monkeypatch.setenv("HARNESS_SLOT_ROOT", str(tmp_path / "isolated"))
    free = acquire_slot("test", limit=2, timeout=0)
    assert free.parent.parent == tmp_path / "isolated"
