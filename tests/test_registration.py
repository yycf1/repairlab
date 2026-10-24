import pytest
from fastapi.testclient import TestClient

from repairlab.api import create_app
from repairlab.model import ScriptedModel
from repairlab.models import RepositoryCheck, RepositoryRegistration, StartRequest
from repairlab.preflight import inspect_repository
from repairlab.service import Service


def registration(repo, **changes):
    return RepositoryRegistration(profile=repo.profile, backend="local", allow_local=True,
                                  authorize_directory=True, **changes)


def test_static_preflight_does_not_import_repository(repo, settings):
    sentinel = repo.root / "imported.txt"
    (repo.root / "conftest.py").write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('imported')\n", encoding="utf-8")
    report = inspect_repository(repo.profile, registration(repo), settings)
    assert report["ready"] and report["can_register"]
    assert report["collection"] is None and not sentinel.exists()
    assert report["runtime"]["packages"]["pytest"]


@pytest.mark.parametrize("target", ["--help", "../tests", "/etc/passwd", "tests --help", "missing.py"])
def test_preflight_rejects_unsafe_or_missing_test_targets(repo, settings, target):
    profile = repo.profile.model_copy(update={"development_tests": [target]})
    report = inspect_repository(profile, registration(repo), settings)
    assert not report["can_register"] and not report["ready"]


def test_missing_dependencies_and_python_version_are_actionable(repo, settings):
    (repo.root / "pyproject.toml").write_text('[project]\nrequires-python=">=99"\ndependencies=["repairlab-nonexistent-package>=9"]\n', encoding="utf-8")
    report = inspect_repository(repo.profile, registration(repo), settings)
    assert report["can_register"] and not report["ready"]
    assert {c["id"] for c in report["checks"] if c["status"] == "error"} == {"python_version", "dependencies"}


def test_acceptance_must_be_outside_source(repo, settings):
    profile = repo.profile.model_copy(update={"acceptance": str(repo.root / "tests")})
    report = inspect_repository(profile, registration(repo), settings)
    assert not report["can_register"]


def test_registration_persists_and_can_be_updated(repo, settings):
    service = Service(settings)
    entry = service.register(registration(repo))
    service.close()
    service = Service(settings)
    try:
        assert service.profile(entry["id"])[0].source == str(repo.root.resolve())
        assert len(service.repositories()) == len(service.fixtures()) + 1
        request = registration(repo)
        request.profile = repo.profile.model_copy(update={"name": "新的仓库名称"})
        updated = service.register(request, entry["id"])
        assert updated["profile"]["name"] == "新的仓库名称"
        assert len(service.store.repositories()) == 1
    finally:
        service.close()


def test_collect_tests_uses_snapshot_and_does_not_run_test_functions(repo, settings):
    (repo.root / "code.py").rename(repo.root / "calculator.py")
    (repo.root / "conftest.py").write_text("from pathlib import Path\nPath('collected.txt').write_text('snapshot only')\n", encoding="utf-8")
    (repo.root / "tests" / "test_code.py").write_text("def test_never_runs():\n    raise RuntimeError('must not execute')\n", encoding="utf-8")
    settings.data_dir.mkdir(parents=True)
    report = inspect_repository(repo.profile, registration(repo, collect_tests=True), settings)
    assert report["ready"] and report["collection"]["exit_code"] == 0, report
    assert "test_never_runs" in report["collection"]["stdout"]
    assert not (repo.root / "collected.txt").exists()
    assert not list(settings.data_dir.glob("preflight-*"))


def test_collection_import_error_is_reported(repo, settings):
    (repo.root / "code.py").rename(repo.root / "calculator.py")
    (repo.root / "tests" / "test_code.py").write_text("import repairlab_nonexistent_module\n", encoding="utf-8")
    settings.data_dir.mkdir(parents=True)
    report = inspect_repository(repo.profile, registration(repo, collect_tests=True), settings)
    assert report["can_register"] and not report["ready"]
    assert "ModuleNotFoundError" in report["collection"]["stdout"] + report["collection"]["stderr"], report


def test_custom_repository_repair_and_start_time_revalidation(repo, settings):
    (repo.root / "tests" / "test_code.py").write_text("from code import answer\ndef test_answer():\n    assert answer() == 2\n", encoding="utf-8")
    # Avoid collision with the stdlib code module in pytest's own process.
    (repo.root / "code.py").rename(repo.root / "calculator.py")
    test = repo.root / "tests" / "test_code.py"
    test.write_text(test.read_text().replace("from code", "from calculator"), encoding="utf-8")
    service = Service(settings)
    try:
        entry = service.register(registration(repo))
        model = ScriptedModel([
            {"action": {"tool": "update_plan", "plan": ["复现并修复返回值"]}},
            {"action": {"tool": "read_file", "path": "calculator.py"}},
            {"action": {"tool": "run_tests"}},
            {"action": {"tool": "record_finding", "paths": ["calculator.py"], "summary": "测试要求 2，实现返回 1"}},
            {"action": {"tool": "edit_file", "path": "calculator.py", "old": "return 1", "new": "return 2"}},
            {"action": {"tool": "run_tests"}},
            {"action": {"tool": "submit", "summary": "修正返回值"}},
        ])
        request = StartRequest(fixture=entry["id"], mode="model", backend="local", allow_local=True)
        task = service.start(request, background=False, model_override=model)
        assert task["status"] == "passed"
        assert task["preflight"]["ready"]
        assert task["repository_profile"]["source"] == str(repo.root)
        assert "return 1" in (repo.root / "calculator.py").read_text()
        assert not task["verification"]["independent_acceptance"]
        test.unlink()
        with pytest.raises(ValueError, match="预检未通过"):
            service.start(request, background=False, model_override=model)
        assert len(service.store.list()) == 1
    finally:
        service.close()


def test_api_registration_requires_explicit_directory_authorization_and_same_origin(repo, settings):
    body = registration(repo).model_dump()
    with TestClient(create_app(settings)) as client:
        body["authorize_directory"] = False
        assert client.post("/api/repositories/preflight", json=body).status_code == 400
        assert client.post("/api/repositories", json=body).status_code == 400
        body["authorize_directory"] = True
        assert client.post("/api/repositories", json=body, headers={"Origin": "https://external.invalid"}).status_code == 403
        saved = client.post("/api/repositories", json=body, headers={"Origin": "http://testserver"})
        assert saved.status_code == 201
        repository_id = saved.json()["id"]
        assert len(client.get("/api/repositories").json()) == len(client.get("/api/fixtures").json()) + 1
        assert any(f["id"] == "quota-guard" and not f["demo"] for f in client.get("/api/fixtures").json())
        body["profile"]["name"] = "更新名称"
        assert client.put(f"/api/repositories/{repository_id}", json=body).json()["profile"]["name"] == "更新名称"
        assert client.get(f"/api/repositories/{repository_id}").status_code == 200
        report = client.post(f"/api/repositories/{repository_id}/preflight", json={"backend": "local"}).json()
        assert report["ready"]  # Metadata-only inspection does not execute repository code.
        report = client.post(f"/api/repositories/{repository_id}/preflight", json={"backend": "local", "collect_tests": True}).json()
        assert not report["ready"]  # Actual import/collection still requires execution consent.
        assert client.get("/api/repositories/local-missing").status_code == 404
        assert client.post("/api/tasks", json={"fixture": repository_id, "mode": "demo", "backend": "local", "allow_local": True}).status_code == 400


def test_unmatched_source_rule_and_non_utf8_snapshot_are_blocked(repo, settings):
    profile = repo.profile.model_copy(update={"editable": ["missing/*.py"]})
    assert not inspect_repository(profile, RepositoryCheck(backend="local", allow_local=True), settings)["can_register"]
    (repo.root / "bad.py").write_bytes(b"\xff\xfe")
    report = inspect_repository(repo.profile, registration(repo), settings)
    assert not report["can_register"]
    assert "UTF-8" in report["checks"][-1]["detail"]
