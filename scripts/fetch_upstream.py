"""Fetch only the selected MIT-licensed core at a recorded immutable commit."""
import hashlib
import json
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "SWE-agent/mini-swe-agent"


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "RepairLab-source-import"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read()


def main():
    metadata_path = ROOT / "third_party/mini-swe-agent/source.json"
    if metadata_path.exists():
        raise SystemExit("Pinned source already exists; do not silently update the upstream revision.")
    remote = subprocess.run(["git", "ls-remote", f"https://github.com/{REPO}.git", "refs/heads/main"],
                            capture_output=True, text=True, check=True, timeout=60)
    commit = remote.stdout.split()[0]
    files = {
        "src/minisweagent/agents/default.py": "src/repairlab/_vendor/mini/agent.py",
        "src/minisweagent/exceptions.py": "src/repairlab/_vendor/mini/exceptions.py",
        "src/minisweagent/utils/serialize.py": "src/repairlab/_vendor/mini/serialize.py",
        "LICENSE.md": "third_party/mini-swe-agent/LICENSE.md",
    }
    records = []
    for origin, target in files.items():
        url = f"https://raw.githubusercontent.com/{REPO}/{commit}/{origin}"
        raw = fetch(url)
        content = raw.decode("utf-8")
        if target.endswith("agent.py"):
            content = content.replace(
                "from minisweagent import Environment, Model, __version__",
                "from repairlab._vendor.mini.contracts import Environment, Model, __version__",
            ).replace(
                "from minisweagent.exceptions import", "from repairlab._vendor.mini.exceptions import"
            ).replace(
                "from minisweagent.utils.serialize import", "from repairlab._vendor.mini.serialize import"
            )
        path = ROOT / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        records.append({"origin": origin, "local": target, "url": url,
                        "upstream_sha256": hashlib.sha256(raw).hexdigest(),
                        "local_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps({"repository": f"https://github.com/{REPO}",
        "commit": commit, "license": "MIT", "files": records,
        "changes": ["Relocated three imports in DefaultAgent; core loop retained.",
                    "Original package startup/global dotenv side effects are not imported."]},
        indent=2), encoding="utf-8")
    print(json.dumps({"commit": commit, "files": len(records)}))


if __name__ == "__main__":
    main()
