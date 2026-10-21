import pytest

from repairlab.models import Limits, RepoProfile
from repairlab.repository import copy_repository
from repairlab.runners import LocalRunner
from repairlab.verification import Verifier


def prepare(tmp_path, acceptance_mode, regression_mode):
    suites = {
        "passed": "from module import answer\ndef test_patch():\n    assert answer() == 2\n",
        "skipped": "import pytest\ndef test_patch():\n    pytest.skip('Unavailable test prerequisite')\n",
        "xfailed": "import pytest\ndef test_patch():\n    pytest.xfail('Known issue')\n",
    }
    suites["mixed"] = suites["passed"] + "\nimport pytest\ndef test_optional():\n    pytest.skip('Optional check')\n"
    baseline = tmp_path / "baseline"
    (baseline / "tests").mkdir(parents=True)
    (baseline / "module.py").write_text("def answer():\n    return 1\n", encoding="utf-8")
    (baseline / "tests" / "test_module.py").write_text(suites[regression_mode], encoding="utf-8")
    acceptance = tmp_path / "acceptance"
    acceptance.mkdir()
    (acceptance / "test_acceptance.py").write_text(suites[acceptance_mode], encoding="utf-8")
    workspace = tmp_path / "workspace"
    copy_repository(baseline, workspace)
    (workspace / "module.py").write_text("def answer():\n    return 2\n", encoding="utf-8")
    profile = RepoProfile(name="verification-checks", source=str(baseline), acceptance=str(acceptance))
    return baseline, workspace, profile


@pytest.mark.parametrize("acceptance_mode,regression_mode,expected", [
    ("passed", "passed", True),
    ("mixed", "passed", True),
    ("skipped", "passed", False),
    ("passed", "skipped", False),
    ("skipped", "skipped", False),
    ("xfailed", "passed", False),
])
def test_verification_requires_passing_tests_in_each_suite(tmp_path, acceptance_mode, regression_mode, expected):
    baseline, workspace, profile = prepare(tmp_path, acceptance_mode, regression_mode)
    report = Verifier(LocalRunner(allowed=True), Limits()).verify(
        baseline, workspace, tmp_path / "verified", profile)
    assert report["acceptance"]["exit_code"] == report["regression"]["exit_code"] == 0
    assert report["passed"] is expected, report
    assert report["reason"] == ("verified" if expected else "no_passing_verification_tests")
    if expected:
        assert report["acceptance"]["counts"]["passed"] > 0
        assert report["regression"]["counts"]["passed"] > 0


def test_verification_does_not_assume_success_without_test_counts(tmp_path):
    class NoSummaryRunner:
        def run_tests(self, *args, **kwargs):
            return {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False,
                    "cancelled": False, "output_limited": False}

    baseline, workspace, profile = prepare(tmp_path, "passed", "passed")
    report = Verifier(NoSummaryRunner(), Limits()).verify(
        baseline, workspace, tmp_path / "verified", profile)
    assert not report["passed"]
    assert report["reason"] == "missing_or_invalid_test_report"


@pytest.mark.parametrize("node,reason", [
    ("_acceptance/test_acceptance.py::test_patch", "verified"),
    ("_acceptance/test_acceptance.py::test_optional", "required_tests_not_passed"),
    ("_acceptance/test_acceptance.py::test_absent", "required_tests_not_passed"),
])
def test_required_acceptance_must_really_pass(tmp_path, node, reason):
    baseline, workspace, profile = prepare(tmp_path, "mixed", "passed")
    profile.required_acceptance_tests = [node]
    report = Verifier(LocalRunner(allowed=True), Limits()).verify(baseline, workspace, tmp_path / "verified", profile)
    assert report["reason"] == reason
    assert report["passed"] is (reason == "verified")
    assert report["acceptance"]["policy"]["required_count"] == 1


def test_required_regression_deselected_by_config_is_rejected(tmp_path):
    baseline, workspace, profile = prepare(tmp_path, "passed", "mixed")
    for root in (baseline, workspace):
        (root / "pytest.ini").write_text("[pytest]\naddopts = -k test_patch\n", encoding="utf-8")
    profile.required_regression_tests = ["tests/test_module.py::test_optional"]
    report = Verifier(LocalRunner(allowed=True), Limits()).verify(baseline, workspace, tmp_path / "verified", profile)
    assert report["reason"] == "required_tests_not_passed"
    assert report["regression"]["policy"]["missing"] == profile.required_regression_tests
    assert report["regression"]["test_evidence"]["deselected"] == profile.required_regression_tests


def test_console_success_without_report_is_rejected(tmp_path):
    class ConsoleOnlyRunner:
        def run_tests(self, *args, **kwargs):
            return {"exit_code": 0, "stdout": "999 passed", "stderr": "", "timed_out": False,
                    "cancelled": False, "output_limited": False}
    baseline, workspace, profile = prepare(tmp_path, "passed", "passed")
    report = Verifier(ConsoleOnlyRunner(), Limits()).verify(baseline, workspace, tmp_path / "verified", profile)
    assert not report["passed"]
    assert report["reason"] == "missing_or_invalid_test_report"
