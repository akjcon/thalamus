"""Where state lives. Locally that is ./data and ./wiki; when THAL_DATA_DIR is set (the Railway volume) everything goes there."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    return Path(os.environ["THAL_DATA_DIR"]) if os.environ.get("THAL_DATA_DIR") else ROOT / "data"


def db_path() -> Path:
    return data_dir() / "thal.db"


def wiki_dir() -> Path:
    return data_dir() / "wiki" if os.environ.get("THAL_DATA_DIR") else ROOT / "wiki"


def kill_switch() -> Path:
    return data_dir() / "KILL"
