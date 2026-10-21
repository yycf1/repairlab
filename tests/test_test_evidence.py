import json

import pytest

from repairlab.feedback import test_feedback as feedback
from repairlab.runners import DockerRunner, LocalRunner
from repairlab.test_evidence import read_evidence, suite_policy


@pytest.mark.parametrize("body,counts,passed", [
    ("def test_ok(): assert True", {"passed": 1}, True),
    ("import pytest\ndef test_skip(): pytest.skip('optional')", {"skipped": 1}, False),
    ("import pytest\n@pytest.mark.xfail\ndef test_xpass(): assert True", {"xpassed": 1}, False),
    ("import pytest\n@pytest.mark.xfail\ndef test_xfail(): assert False", {"xfailed": 1}, False),
    ("import pytest\n@pytest.fixture(autouse=True)\ndef setup():\n    yield\n    assert False\ndef test_ok(): assert True",
     {"error": 1}, False),
    ("def test_a(): assert False\ndef test_b(): assert True", {"failed": 1, "passed": 1}, False),
])
def test_real_pytest_phase_outcomes(tmp_path, body, counts, passed):
    (tmp_path / "test_example.py").write_text(body + "\n", encoding="utf-8")
    result = LocalRunner(allowed=True).run_tests(tmp_path, ["test_example.py"], 20)
    assert result["test_evidence"]["valid"], result
    assert result["test_evidence"]["counts"] == counts
    assert suite_policy(result)["passed"] is passed
    assert (feedback(result, tmp_path, 1, 0, "digest")["category"] == "ok") is passed


def test_collection_only_is_not_execution(tmp_path):
    (tmp_path / "test_example.py").write_text("def test_example(): assert True\n", encoding="utf-8")
    result = LocalRunner(allowed=True).run_tests(tmp_path, ["--collect-only"], 20)
    assert result["exit_code"] == 0
    assert result["test_evidence"]["counts"] == {"not_run": 1}
    assert not suite_policy(result)["passed"]


def test_inherited_pytest_options_cannot_skip_required_tests(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k missing")
    (tmp_path / "test_example.py").write_text("def test_example(): assert True\n", encoding="utf-8")
    result = LocalRunner(allowed=True).run_tests(tmp_path, ["test_example.py"], 20)
    assert suite_policy(result, ["test_example.py::test_example"])["passed"]


def test_report_is_required_and_bound_to_this_run(tmp_path):
    path = tmp_path / "report.json"
    assert not read_evidence(path, "current", 0)["valid"]
    path.write_text("not json")
    assert not read_evidence(path, "current", 0)["valid"]
    path.write_text(json.dumps({"schema_version": 1, "run_id": "old", "exit_code": 0}))
    assert not read_evidence(path, "current", 0)["valid"]


def test_docker_evidence_is_outside_readonly_workspace(tmp_path):
    argv = DockerRunner.command(tmp_path / "workspace", ["tests"], "image", "test",
                                harness=tmp_path / "harness", run_id="current")
    mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--mount"]
    assert len(mounts) == 3
    assert mounts[0].endswith("target=/workspace,readonly")
    assert mounts[1].endswith("target=/harness,readonly")
    assert mounts[2].endswith("target=/reports")
    assert "REPAIRLAB_TEST_RUN_ID=current" in argv
    assert "repairlab_pytest_evidence" in argv
