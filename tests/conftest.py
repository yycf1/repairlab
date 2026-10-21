import pytest
from repairlab.settings import Settings, ROOT


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("REPAIRLAB_DATA_DIR", str(tmp_path / "data"))
    for key in ("REPAIRLAB_MODEL", "REPAIRLAB_API_KEY", "REPAIRLAB_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    return Settings(root=ROOT)


@pytest.fixture
def repo(tmp_path):
    from repairlab.models import RepoProfile
    from repairlab.repository import Repository
    root = tmp_path / "repo"
    root.mkdir()
    (root / "code.py").write_text("def answer():\n    return 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_code.py").write_text("assert True\n", encoding="utf-8")
    (root / "conftest.py").write_text("# protected\n", encoding="utf-8")
    return Repository(root, RepoProfile(name="test", source=str(root)))
