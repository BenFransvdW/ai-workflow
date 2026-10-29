from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(log_dir: Path | None, name: str = "pipeline", level: int = logging.INFO) -> None:
    """Log to stderr (captured by journald under systemd) and to a rotating file."""
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(FMT))
    root.addHandler(stream)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / f"{name}.log", maxBytes=2_000_000, backupCount=5)
        fh.setFormatter(logging.Formatter(FMT))
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore", "urllib3", "googleapiclient", "prawcore", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
