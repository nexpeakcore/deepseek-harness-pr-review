"""Finished agent results, kept so an interrupted review does not start over.

A large PR needs a dozen agents of several minutes each. When the review is
killed or runs out of time, the agents that had finished used to be lost with
it, and the retry — facing the same PR — ran out of time in the same place.
Each finished agent is written here as it completes; the next attempt at the
same head reuses it and runs only what is missing.

A result is reused only when its fingerprint still matches: the head commit,
the agent's name, its prompt and its input files. A new push, or a different
description or diff, changes the fingerprint and the result is ignored.
"""
import hashlib
import json
import os
import shutil
from pathlib import Path

DIR = "checkpoints"


def fingerprint(task: dict, head_sha: str) -> str:
    """What a result depends on. The per-round work directory is left out: it is
    a random name that appears in the prompt and the input paths."""
    work_dir = str(Path(task.get("out", "")).parent)

    def norm(text: str) -> str:
        return text if work_dir in ("", ".") else text.replace(work_dir + "/", "")

    inputs = {norm(name): content
              for name, content in sorted((task.get("inputs") or {}).items())}
    digest = hashlib.sha256()
    for piece in (head_sha or "", task["name"], norm(task["prompt"]),
                  json.dumps(inputs, sort_keys=True)):
        digest.update(piece.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _path(session_dir: Path, task: dict) -> Path:
    return session_dir / DIR / f"{task['name']}.json"


def save(session_dir: Path, task: dict, part: dict) -> None:
    """Record a finished agent. Best effort: failing to save costs a re-run on
    the next attempt, never the review in hand."""
    if not task.get("fingerprint"):
        return
    path = _path(session_dir, task)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fingerprint": task["fingerprint"],
                                   "part": part}))
        os.replace(tmp, path)
    except OSError:
        pass


def load(session_dir: Path, task: dict) -> dict | None:
    """The saved part for this task, if one exists for exactly this input."""
    if not task.get("fingerprint"):
        return None
    try:
        data = json.loads(_path(session_dir, task).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("fingerprint") != task["fingerprint"]:
        return None
    part = data.get("part")
    if not isinstance(part, dict) or not all(
            isinstance(part.get(k), list) for k in task["keys"]):
        return None
    return {k: part[k] for k in task["keys"]}


def clear(session_dir: Path) -> None:
    shutil.rmtree(session_dir / DIR, ignore_errors=True)
