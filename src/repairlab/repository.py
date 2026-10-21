from __future__ import annotations

import ast
import difflib
from fnmatch import fnmatchcase
from functools import lru_cache
import os
from pathlib import Path, PurePosixPath
import shutil

from repairlab.models import RepoProfile

EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".data", ".pytest_cache", ".idea", ".vscode"}
SUFFIXES = {".py", ".toml", ".ini", ".cfg", ".txt", ".md", ".json", ".yaml", ".yml"}


def source_matches(name, pattern):
    """Match ** across zero or more directories; bare patterns keep basename semantics."""
    parts = PurePosixPath(name).parts
    tokens = PurePosixPath(pattern.replace("\\", "/")).parts
    if len(tokens) == 1 and not pattern.startswith("./"):
        if not any(c in tokens[0] for c in "*?["):
            return name == pattern
        return bool(parts) and fnmatchcase(parts[-1], tokens[0])
    @lru_cache(maxsize=None)
    def match(i, j):
        if j == len(tokens):
            return i == len(parts)
        if tokens[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatchcase(parts[i], tokens[j]) and match(i + 1, j + 1)
    return match(0, 0)


def snapshot_files(source: Path):
    """Shared, bounded manifest for preflight and copying; never walk excluded directories."""
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Repository must be a directory")
    files, total, visited, entries = [], 0, 0, 0
    def unreadable(error):
        raise ValueError("Repository contains an unreadable directory") from error
    for directory, dirs, names in os.walk(source, topdown=True, followlinks=False, onerror=unreadable):
        visited += 1
        entries += len(dirs) + len(names)
        if visited > 2000 or entries > 20000:
            raise ValueError("Repository traversal exceeds first-version limits")
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not d.startswith("."))
        paths = [Path(directory) / name for name in dirs + sorted(names) if not name.startswith(".")]
        for path in paths:
            relative = path.relative_to(source)
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ValueError(f"Symlinks/junctions are not supported: {relative}")
            if not path.is_file() or path.suffix.lower() not in SUFFIXES:
                continue
            total += path.stat().st_size
            files.append(path)
            if len(files) > 500 or total > 8_000_000 or path.stat().st_size > 500_000:
                raise ValueError("Repository exceeds first-version snapshot limits (500 files / 8 MB)")
            try:
                path.read_text(encoding="utf-8")
            except UnicodeError as error:
                raise ValueError(f"Snapshot files must be UTF-8 text: {relative}") from error
    if not files:
        raise ValueError("No supported repository files found")
    return sorted(files)


def copy_repository(source: Path, target: Path):
    source = source.resolve(strict=True)
    files = snapshot_files(source)
    target.mkdir(parents=True, exist_ok=False)
    for path in files:
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)


class Repository:
    def __init__(self, root: Path, profile: RepoProfile):
        self.root = root.resolve()
        self.profile = profile

    def path(self, name: str) -> Path:
        normalized = name.replace("\\", "/")
        relative = PurePosixPath(normalized)
        if not name or relative.is_absolute() or ":" in normalized or ".." in relative.parts:
            raise ValueError("Path must stay within the repository")
        if any(p.startswith(".") or p in EXCLUDED for p in relative.parts):
            raise ValueError("Hidden/configuration directories are not exposed")
        candidate = self.root / normalized
        for parent in [candidate, *candidate.parents]:
            if parent == self.root:
                break
            if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
                raise ValueError("Symlink/junction paths are not allowed")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Path escapes repository")
        return resolved

    def files(self):
        result = []
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root)
            if p.is_file() and not any(part in EXCLUDED or part.startswith(".") for part in rel.parts):
                if p.suffix.lower() in SUFFIXES and not p.is_symlink():
                    result.append(rel.as_posix())
        return result[:500]

    def editable(self, name):
        rel = PurePosixPath(name.replace("\\", "/"))
        protected_names = {"conftest.py", "pyproject.toml", "pytest.ini", "setup.cfg", "setup.py", "tox.ini"}
        if any(p.lower() in {"test", "tests", "acceptance", "_acceptance"} for p in rel.parts):
            return False
        name_lower = rel.name.lower()
        if name_lower in protected_names or name_lower.startswith("test_") or name_lower.endswith("_test.py"):
            return False
        if any(source_matches(rel.as_posix(), pattern) for pattern in self.profile.protected):
            return False
        for target in self.profile.development_tests + self.profile.regression_tests:
            base = PurePosixPath(target.split("::", 1)[0].replace("\\", "/")).as_posix()
            if base != "." and (rel.as_posix() == base or rel.as_posix().startswith(base.rstrip("/") + "/")):
                return False
        return rel.suffix == ".py" and any(source_matches(rel.as_posix(), pattern) for pattern in self.profile.editable)

    def read(self, name, start=1, end=160):
        path = self.path(name)
        if not path.is_file() or path.stat().st_size > 500_000:
            raise ValueError("File unavailable or too large")
        lines = path.read_text(encoding="utf-8").splitlines()
        end = min(end, start + 199, len(lines))
        return {"path": name, "total_lines": len(lines),
                "text": "\n".join(f"{i+1:4} | {lines[i]}" for i in range(start-1, end))}

    def search(self, query):
        matches = []
        for name in self.files():
            lines = self.path(name).read_text(encoding="utf-8", errors="replace").splitlines()
            for i, line in enumerate(lines, 1):
                if query.casefold() in line.casefold():
                    matches.append({"path": name, "line": i, "text": line[:220]})
                    if len(matches) >= 60:
                        return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def symbols(self, name):
        tree = ast.parse(self.path(name).read_text(encoding="utf-8"))
        return {"path": name, "symbols": [{"name": n.name, "kind": type(n).__name__,
            "line": n.lineno, "end_line": n.end_lineno} for n in ast.walk(tree)
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))][:100]}

    def edit(self, name, old, new):
        if not self.editable(name):
            raise ValueError("Only explicitly allowed source files may be changed; tests/configuration are protected")
        path = self.path(name)
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count != 1:
            raise ValueError(f"Expected one exact match, found {count}; read the file and select a unique fragment")
        revised = text.replace(old, new, 1)
        if len(revised.encode("utf-8")) > 500_000:
            raise ValueError("Edited file too large")
        path.write_text(revised, encoding="utf-8", newline="\n")
        return {"path": name, "changed": revised != text}


def diff_repositories(baseline: Path, workspace: Path, profile: RepoProfile):
    original, current = Repository(baseline, profile), Repository(workspace, profile)
    before_files, after_files = set(original.files()), set(current.files())
    patch, changes, violations = [], [], []
    for name in sorted(before_files | after_files):
        a, b = baseline / name, workspace / name
        before = a.read_text(encoding="utf-8") if a.is_file() else ""
        after = b.read_text(encoding="utf-8") if b.is_file() else ""
        if before == after:
            continue
        if not current.editable(name) or name not in before_files or name not in after_files:
            violations.append(name)
        changes.append(name)
        for line in difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile=f"a/{name}", tofile=f"b/{name}"):
            patch.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(patch), changes, violations
