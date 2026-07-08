"""EasyCode 配置：独立数据目录与 EASYCODE_* 环境变量。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT

    @property
    def data_dir(self):
        return Path(os.getenv("EASYCODE_DATA_DIR", str(self.root / ".easycode"))).resolve()

    @property
    def env_path(self):
        return Path(os.getenv("EASYCODE_ENV_PATH", str(self.root / ".env")))

    def _env(self, name):
        return os.getenv(name, "").strip()

    @property
    def model(self):
        return self._env("EASYCODE_MODEL")

    @property
    def base_url(self):
        return self._env("EASYCODE_BASE_URL").rstrip("/")

    @property
    def api_key(self):
        return self._env("EASYCODE_API_KEY")

    @property
    def model_ready(self):
        return bool(self.model and self.base_url and self.api_key)

    def public(self):
        import shutil
        return {"name": "EasyCode", "model": self.model, "model_configured": self.model_ready,
                "docker_installed": bool(shutil.which("docker")), "directory_picker": os.name == "nt"}
