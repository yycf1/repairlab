import pytest
from repairlab.repository import copy_repository, diff_repositories
from repairlab.runners import LocalRunner
from repairlab.verification import Verifier
from repairlab.models import Limits


@pytest.mark.parametrize("name", ["../secret.py", "a/../../secret", "E:/secret", "/etc/passwd", ".env", ".git/config"])
def test_path_escape_blocked(repo, name):
    with pytest.raises(ValueError):
        repo.path(name)


@pytest.mark.parametrize("name", ["tests/test_code.py", "conftest.py", "CONFTEST.PY", "test_code.py", "pyproject.toml"])
def test_protected_files_blocked(repo, name):
    with pytest.raises(ValueError):
        repo.edit(name, "assert True", "assert False")


def test_edit_requires_unique_fragment(repo):
    with pytest.raises(ValueError, match="one exact match"):
        repo.edit("code.py", "missing", "anything")
    repo.edit("code.py", "return 1", "return 2")
    assert "return 2" in repo.read("code.py")["text"]
    assert repo.symbols("code.py")["symbols"][0]["name"] == "answer"
    assert repo.search("return 2")["matches"][0]["path"] == "code.py"


def test_recursive_source_glob_covers_flat_and_deep_modules_but_not_tests(repo):
    repo.profile.editable = ["src/**/*.py"]
    assert repo.editable("src/parser.py")
    assert repo.editable("src/package/parser.py")
    assert repo.editable("src/package/subpackage/parser.py")
    assert not repo.editable("other/parser.py")
    assert not repo.editable("src/tests/test_parser.py")


def test_snapshot_does_not_copy_credentials_or_history(repo, tmp_path):
    (repo.root / ".env").write_text("SECRET=do-not-copy", encoding="utf-8")
    (repo.root / ".git").mkdir()
    (repo.root / ".git" / "config").write_text("private remote", encoding="utf-8")
    target = tmp_path / "snapshot"
    copy_repository(repo.root, target)
    assert (target / "code.py").exists()
    assert not (target / ".env").exists()
    assert not (target / ".git").exists()


def test_no_newline_patch_can_be_applied_to_clean_copy(repo, tmp_path):
    baseline = tmp_path / "baseline"
    (repo.root / "code.py").write_text("value = 1", encoding="utf-8")
    copy_repository(repo.root, baseline)
    repo.edit("code.py", "1", "2")
    result = Verifier(LocalRunner(allowed=True), Limits()).verify(baseline, repo.root, tmp_path / "verify", repo.profile)
    assert result["reason"] != "patch_not_applicable"
    assert (tmp_path / "verify" / "code.py").read_text() == "value = 2"


def test_final_guard_detects_direct_test_tampering(repo, tmp_path):
    baseline = tmp_path / "baseline"
    copy_repository(repo.root, baseline)
    (repo.root / "tests" / "test_code.py").write_text("assert False", encoding="utf-8")
    patch, changed, violations = diff_repositories(baseline, repo.root, repo.profile)
    assert patch and changed == violations == ["tests/test_code.py"]
    result = Verifier(None, Limits()).verify(baseline, repo.root, tmp_path / "verify", repo.profile)
    assert result["reason"] == "protected_file_changed"
