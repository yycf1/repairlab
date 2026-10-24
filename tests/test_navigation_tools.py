"""AST navigation tools: repo outline and symbol references."""
from repairlab.models import Action
from repairlab.tools import ToolEnvironment


def test_outline_lists_top_level_definitions(repo):
    data = repo.outline()
    paths = {f["path"] for f in data["files"]}
    assert "code.py" in paths
    code = next(f for f in data["files"] if f["path"] == "code.py")
    assert any(s["name"] == "answer" and s["kind"] == "def" and s["line"] == 1 for s in code["symbols"])


def test_references_finds_loads_and_attributes(repo):
    (repo.root / "use.py").write_text("from code import answer\n\n\ndef call():\n    return answer() or answer\n",
                                      encoding="utf-8")
    data = repo.references("answer")
    hits = {(m["path"], m["line"]) for m in data["matches"]}
    assert ("use.py", 1) in hits and ("use.py", 5) in hits
    assert not data["truncated"]


def test_dispatch_wiring(repo):
    environment = ToolEnvironment(repo, runner=None, limits=None, emit=lambda *a: None, cancel=None)
    outline = environment.dispatch(Action(tool="outline"))
    assert any(f["path"] == "code.py" for f in outline.data["files"])
    references = environment.dispatch(Action(tool="references", symbol="clamp"))
    assert references.data["matches"] == []
