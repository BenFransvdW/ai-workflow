"""Shared runtime context passed to every stage."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .config import Config, Secrets, load_config


@dataclass
class Ctx:
    config: Config
    conn: sqlite3.Connection
    secrets: Secrets
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("shorts"))

    @property
    def data_dir(self) -> Path:
        return self.config.data_dir

    def work_dir(self, *parts: str | int) -> Path:
        """Per-entity working directory under data/, created on demand."""
        p = self.data_dir.joinpath(*(str(x) for x in parts))
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def from_files(cls, config_path: str | Path = "config.yaml", env_file: str | Path | None = None) -> "Ctx":
        cfg = load_config(config_path)
        env = env_file if env_file is not None else cfg.root / ".env"
        secrets = Secrets(env if Path(env).exists() else None)
        conn = db.connect(cfg.db_path)
        return cls(config=cfg, conn=conn, secrets=secrets)
