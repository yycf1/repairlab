"""Exercise the real Docker runner against authored trusted checks; no model API."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

from repairlab.runners import DockerRunner
from repairlab.test_evidence import suite_policy

CHECKS = '''import os
from pathlib import Path
import socket
import pytest

def test_non_root():
    assert os.getuid() == 10001

def test_workspace_readonly():
    with pytest.raises(OSError):
        Path('/workspace/forbidden.txt').write_text('write')

def test_root_readonly():
    with pytest.raises(OSError):
        Path('/etc/repairlab-forbidden').write_text('write')

def test_network_disabled():
    assert set(os.listdir('/sys/class/net')) == {'lo'}
    with socket.socket() as client:
        client.settimeout(1)
        with pytest.raises(OSError):
            client.connect(('1.1.1.1', 443))
'''


def verify(image):
    runner = DockerRunner()
    with tempfile.TemporaryDirectory(prefix="repairlab-docker-smoke-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        check = root / "test_constraints.py"
        check.write_text(CHECKS, encoding="utf-8")
        check.chmod(0o644)
        result = runner.run_tests(root, [check.name], 30, image=image)
        assert suite_policy(result)["passed"], result
        check.write_text("import time\ndef test_timeout(): time.sleep(60)\n", encoding="utf-8")
        timeout = runner.run_tests(root, [check.name], 2, image=image)
        assert timeout["timed_out"] and not suite_policy(timeout)["passed"], timeout
        name = timeout["command"][timeout["command"].index("--name") + 1]
        leftover = subprocess.run(["docker", "ps", "-aq", "--filter", f"name=^{name}$"],
                                  capture_output=True, text=True, check=True)
        assert not leftover.stdout.strip(), "Timed-out container was not removed"
    image_id = subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True).strip()
    version = subprocess.check_output(["docker", "version", "--format", "{{.Server.Version}}"], text=True).strip()
    return {"passed": True, "image_id": image_id, "docker_server": version,
            "constraints": result["test_evidence"], "timeout_observed": timeout["timed_out"],
            "timed_out_container_removed": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="repairlab-python:latest")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.image)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "image_id": report["image_id"]}))
