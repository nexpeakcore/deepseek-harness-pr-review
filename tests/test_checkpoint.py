# tests/test_checkpoint.py
from src import checkpoint


def _task(**over):
    task = {"name": "code-1", "out": ".harness-review-aaa/findings-code-1.json",
            "keys": ("code", "unresolved_questions"),
            "prompt": "read .harness-review-aaa/review-diff-code-1.patch",
            "inputs": {".harness-review-aaa/review-diff-code-1.patch": "diff"}}
    task.update(over)
    task["fingerprint"] = checkpoint.fingerprint(task, "head1")
    return task


PART = {"code": [{"title": "x"}], "unresolved_questions": []}


def test_a_saved_part_comes_back_for_the_same_task(tmp_path):
    task = _task()
    checkpoint.save(tmp_path, task, PART)
    assert checkpoint.load(tmp_path, task) == PART


def test_the_per_round_work_dir_does_not_change_the_fingerprint():
    other = _task(out=".harness-review-zzz/findings-code-1.json",
                  prompt="read .harness-review-zzz/review-diff-code-1.patch",
                  inputs={".harness-review-zzz/review-diff-code-1.patch": "diff"})
    assert other["fingerprint"] == _task()["fingerprint"]


def test_a_changed_diff_prompt_or_head_is_a_miss(tmp_path):
    checkpoint.save(tmp_path, _task(), PART)
    changed_diff = _task(inputs={".harness-review-aaa/review-diff-code-1.patch": "d2"})
    changed_prompt = _task(prompt="something else")
    assert checkpoint.load(tmp_path, changed_diff) is None
    assert checkpoint.load(tmp_path, changed_prompt) is None
    new_head = _task()
    new_head["fingerprint"] = checkpoint.fingerprint(new_head, "head2")
    assert checkpoint.load(tmp_path, new_head) is None


def test_a_damaged_checkpoint_is_ignored(tmp_path):
    task = _task()
    checkpoint.save(tmp_path, task, PART)
    path = tmp_path / checkpoint.DIR / "code-1.json"
    path.write_text("{truncated")
    assert checkpoint.load(tmp_path, task) is None
    path.write_text('{"fingerprint": "%s", "part": {"code": "no"}}'
                    % task["fingerprint"])
    assert checkpoint.load(tmp_path, task) is None


def test_a_task_without_a_fingerprint_is_never_checkpointed(tmp_path):
    task = _task()
    del task["fingerprint"]
    checkpoint.save(tmp_path, task, PART)
    assert not (tmp_path / checkpoint.DIR).exists()
    assert checkpoint.load(tmp_path, task) is None


def test_clear_removes_everything(tmp_path):
    checkpoint.save(tmp_path, _task(), PART)
    checkpoint.clear(tmp_path)
    assert not (tmp_path / checkpoint.DIR).exists()
    checkpoint.clear(tmp_path)  # idempotent
