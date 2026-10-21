from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from repairlab.api import create_app
from repairlab.discovery import discover_repository
from repairlab.models import RepoProfile
from repairlab.repository import Repository


def write(root, name, text="def value():\n    return 1\n"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_discovery_infers_src_layout_and_protects_tests_without_importing(tmp_path, settings):
    root = tmp_path / "my-code"
    write(root, "src/package/parser.py")
    write(root, "tests/test_parser.py", "raise RuntimeError('do not import during discovery')\n")
    write(root, "docs/example.py")
    write(root, "setup.py", "raise RuntimeError('do not execute setup.py')\n")
    write(root, "pyproject.toml", '[project]\nname="friendly-name"\n[tool.pytest.ini_options]\ntestpaths=["tests"]\n')
    result = discover_repository(str(root), settings)
    p, s = result["profile"], result["summary"]
    assert p["name"] == "friendly-name"
    assert p["editable"] == ["src/package/parser.py"]
    assert p["development_tests"] == p["regression_tests"] == ["tests"]
    assert s["source_count"] == 1 and s["test_count"] == 1
    assert not s["needs_configuration"]


def test_custom_pytest_naming_and_helper_files_remain_protected(tmp_path, settings):
    root = tmp_path / "project"
    write(root, "module.py")
    write(root, "checks/check_feature.py")
    write(root, "checks/support.py")
    write(root, "pytest.ini", "[pytest]\ntestpaths = checks\npython_files = check_*.py\n")
    result = discover_repository(str(root), settings)
    profile = RepoProfile.model_validate(result["profile"])
    assert profile.development_tests == ["checks"]
    assert profile.editable == ["module.py"]
    # Even an advanced source wildcard cannot override detected tests or test target directories.
    profile.editable = ["*.py"]
    repo = Repository(root, profile)
    assert not repo.editable("checks/check_feature.py")
    assert not repo.editable("checks/support.py")


def test_flat_layout_and_no_tests_require_only_missing_configuration(tmp_path, settings):
    root = tmp_path / "flat-project"
    write(root, "calculator.py")
    result = discover_repository(str(root), settings)
    assert result["profile"]["name"] == "flat-project"
    assert result["profile"]["editable"] == ["calculator.py"]
    assert result["summary"]["needs_configuration"]
    assert not result["profile"]["development_tests"]
    write(root, "test_calculator.py")
    result = discover_repository(str(root), settings)
    assert result["profile"]["development_tests"] == ["test_calculator.py"]
    assert not result["summary"]["needs_configuration"]


def test_auto_scope_does_not_grant_same_named_files_in_excluded_directories(tmp_path, settings):
    root = tmp_path / "project"
    write(root, "parser.py")
    write(root, "docs/parser.py")
    write(root, "value[1].py")
    write(root, "docs/value[1].py")
    write(root, "tests/test_parser.py")
    result = discover_repository(str(root), settings)
    repo = Repository(root, RepoProfile.model_validate(result["profile"]))
    assert repo.editable("parser.py") and repo.editable("value[1].py")
    assert not repo.editable("docs/parser.py") and not repo.editable("docs/value[1].py")


def test_literal_brackets_in_filename_and_configured_test_globs(tmp_path, settings):
    root = tmp_path / "project"
    write(root, "src/value[1].py")
    write(root, "checks/check_one.py")
    write(root, "pytest.ini", "[pytest]\ntestpaths = checks/check_*.py\npython_files = check_*.py\n")
    result = discover_repository(str(root), settings)
    repo = Repository(root, RepoProfile.model_validate(result["profile"]))
    assert repo.editable("src/value[1].py")
    assert not repo.editable("src/value1.py")
    assert result["profile"]["development_tests"] == ["checks/check_one.py"]


def test_discovery_and_picker_api_authorization_cancel_and_failure(repo, settings):
    with TestClient(create_app(settings)) as client:
        body = {"source": str(repo.root)}
        assert client.post("/api/repositories/discover", json=body).status_code == 400
        body["authorize_directory"] = True
        assert client.post("/api/repositories/discover", json=body).json()["profile"]["name"] == repo.root.name
        with patch("repairlab.api.choose_directory", return_value={"cancelled": True}) as choose:
            assert client.post("/api/repositories/pick-directory", json={}).json() == {"cancelled": True}
            choose.assert_called_once_with("")
        with patch("repairlab.api.choose_directory", return_value={"cancelled": False, "source": str(repo.root)}):
            response = client.post("/api/repositories/pick-directory", json={})
            assert response.status_code == 200
            assert response.json()["profile"]["source"] == str(repo.root)
            assert not client.app.state.service.store.repositories()  # Selection does not implicitly save.
        with patch("repairlab.api.choose_directory", side_effect=RuntimeError("picker unavailable")):
            assert client.post("/api/repositories/pick-directory", json={}).status_code == 400
        with patch("repairlab.api.choose_directory") as choose:
            assert client.post("/api/repositories/pick-directory", json={}, headers={"Origin": "https://external.invalid"}).status_code == 403
            choose.assert_not_called()


@pytest.mark.parametrize("source", ["E:\\", "relative-path"])
def test_discovery_rejects_root_and_relative_paths(settings, source):
    with pytest.raises(ValueError):
        discover_repository(source, settings)
