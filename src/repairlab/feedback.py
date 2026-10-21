"""Convert bounded pytest output into observations, never infer root cause as fact."""
import re
from pathlib import Path
from repairlab.test_evidence import suite_policy


def pytest_counts(log):
    return {label: int(number) for number, label in re.findall(
        r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed)\b", log)}


def test_feedback(result, root, run_id, revision, digest):
    log = result.get("stdout", "") + "\n" + result.get("stderr", "")
    code = result["exit_code"]
    if result["cancelled"]:
        category = "cancelled"
    elif result["timed_out"]:
        category = "timeout"
    elif result["output_limited"]:
        category = "output_limit"
    elif code == 0:
        category = "ok" if suite_policy(result)["passed"] else "insufficient_test_evidence"
    elif code == 1:
        category = "test_failure"
    elif code == 2 and re.search(r"\b(?:SyntaxError|IndentationError|TabError)\b", log):
        category = "code_error"
    elif code == 2 and re.search(r"\b(?:ModuleNotFoundError|ImportError)\b", log):
        category = "environment_error"
    elif code == 2:
        category = "collection_error"
    else:
        category = "environment_error"
    nodes = list(dict.fromkeys(re.findall(r"^(?:FAILED|ERROR)\s+(\S+)", log, re.MULTILINE)))[:8]
    raw_frames = re.findall(r'([\w./\\:-]+\.py):(\d+)', log)
    raw_frames += re.findall(r'File "([^"]+\.py)", line (\d+)', log)
    frames = []
    for name, line in raw_frames:
        candidate = Path(name.replace("\\", "/"))
        if candidate.is_absolute():
            try:
                candidate = candidate.resolve().relative_to(root.resolve())
            except ValueError:
                continue
        name = candidate.as_posix()
        if ".." in candidate.parts or not (root / candidate).is_file():
            continue
        frame = {"path": name, "line": int(line)}
        if frame not in frames:
            frames.append(frame)
    hints = {
        "ok": "Tests passed for this source revision; review the patch before submitting.",
        "test_failure": "Inspect failed nodes and locations; revise the hypothesis or undo an edit.",
        "code_error": "Collection hit a syntax error; inspect its source location before editing.",
        "collection_error": "Inspect logs to distinguish code defects from test setup errors.",
        "environment_error": "Do not guess a code fix for missing dependencies or runner failure; finish if blocked.",
        "timeout": "Inspect for hangs or report the execution limit.",
        "output_limit": "Inspect runaway logging or report the execution limit.",
        "cancelled": "Task was cancelled."
    }
    hints["insufficient_test_evidence"] = "No complete passing test evidence; inspect skipped, missing or incomplete tests."
    counts = (result.get("test_evidence") or {}).get("counts", pytest_counts(log))
    return {"run_id": run_id, "revision": revision, "source_digest": digest, "category": category,
            "exit_code": code, "failed_nodes": nodes, "source_locations": frames[:8], "counts": counts,
            "hint": hints[category]}
