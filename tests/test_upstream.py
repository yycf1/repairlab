import hashlib
import json
from repairlab.settings import ROOT


def test_pinned_vendor_matches_recorded_local_hashes():
    source = json.loads((ROOT / "third_party/mini-swe-agent/source.json").read_text(encoding="utf-8"))
    assert len(source["commit"]) == 40
    for item in source["files"]:
        assert hashlib.sha256((ROOT / item["local"]).read_bytes()).hexdigest() == item["local_sha256"]
    license_text = (ROOT / "third_party/mini-swe-agent/LICENSE.md").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "Copyright" in license_text
