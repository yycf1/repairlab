from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT

    @property
    def data_dir(self):
        return Path(os.getenv("REPAIRLAB_DATA_DIR", str(self.root / ".data"))).resolve()

    @property
    def model(self):
        return os.getenv("REPAIRLAB_MODEL", "").strip()

    @property
    def base_url(self):
        return os.getenv("REPAIRLAB_BASE_URL", "").strip().rstrip("/")

    @property
    def api_key(self):
        return os.getenv("REPAIRLAB_API_KEY", "").strip()

    @property
    def model_ready(self):
        return bool(self.model and self.base_url and self.api_key)

    def public(self):
        import shutil
        return {"name": "修码工坊 · RepairLab", "model": self.model,
                "model_configured": self.model_ready, "docker_installed": bool(shutil.which("docker")),
                "directory_picker": os.name == "nt",
                "version": "0.1.0", "default_backend": "docker"}
