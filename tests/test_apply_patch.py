"""One-click patch application: confirmed copy of verified changes onto the source repository."""
import pytest
from repairlab.models import Limits, RepoProfile
from repairlab.service import Service


@pytest.fixture
def local_task(settings, tmp_path):
    source = tmp_path / "repo"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (source / "pkg" / "old.py").write_text("legacy = True\n", encoding="utf-8")
    profile = RepoProfile(name="local-repo", source=str(source), editable=["pkg/*.py"],
                          development_tests=["tests"], regression_tests=["tests"])
    service = Service(settings)
    task_id = "apply0000000001"
    workspace = settings.data_dir / "tasks" / task_id / "workspace"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "mod.py").write_text("x = 2\n", encoding="utf-8")  # fixed file
    # old.py absent from the workspace: the patch deletes it from the source
    service.store.create(task_id, {
        "title": "t", "issue": "i", "mode": "model", "backend": "local", "strategy": "feedback",
        "task_kind": "repair", "fixture": "local-abc123def456", "limits": Limits().model_dump(),
        "allow_local": True, "round": 1, "status": "passed", "patch": "diff-text",
        "changed_files": ["pkg/mod.py", "pkg/old.py"], "repository_profile": profile.model_dump()})
    service.store.update(task_id, status="passed")  # status lives in its own column
    return service, task_id, source


def test_preview_apply_and_undo_restore_source(settings, local_task):
    service, task_id, source = local_task
    try:
        preview = service.apply_patch(task_id, confirm=False)
        assert preview["preview"] and preview["changed_files"] == ["pkg/mod.py", "pkg/old.py"]
        assert preview["deletions"] == ["pkg/old.py"]
        assert (source / "pkg" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"

        result = service.apply_patch(task_id, confirm=True)
        assert result["applied"] == ["pkg/mod.py", "pkg/old.py"]
        assert (source / "pkg" / "mod.py").read_text(encoding="utf-8") == "x = 2\n"
        assert not (source / "pkg" / "old.py").exists()
        assert service.store.get(task_id)["applied"]

        with pytest.raises(ValueError):
            service.apply_patch(task_id, confirm=True)

        undone = service.undo_apply(task_id)
        assert undone["restored"] == ["pkg/mod.py", "pkg/old.py"]
        assert (source / "pkg" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"
        assert (source / "pkg" / "old.py").read_text(encoding="utf-8") == "legacy = True\n"
    finally:
        service.close()


def test_builtin_fixture_and_unpassed_tasks_are_refused(settings, local_task):
    service, task_id, _ = local_task
    try:
        service.store.update(task_id, fixture="boundary-discount")
        with pytest.raises(ValueError, match="内置演示仓库"):
            service.apply_patch(task_id, confirm=True)
        service.store.update(task_id, fixture="local-abc123def456", status="failed")
        with pytest.raises(ValueError, match="验收通过"):
            service.apply_patch(task_id, confirm=True)
    finally:
        service.close()
