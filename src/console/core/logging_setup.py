"""UTF-8 檔案 logging（Windows cp950 對策：不依賴 console 編碼）。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from console.core.config import STATE_DIR

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging(filename: str = "console.log", level: int = logging.INFO) -> None:
    log_dir = STATE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    handler = RotatingFileHandler(
        log_dir / filename, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)
    root.setLevel(level)
