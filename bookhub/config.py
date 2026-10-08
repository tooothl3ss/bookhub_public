"""Paths are configured before creating an application; importing has no I/O."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def data_root():
    return Path(os.environ.get('BOOKHUB_DATA', ROOT / 'data')).resolve()

def database_path():
    return Path(os.environ.get('BOOKHUB_DB') or os.environ.get('DB_PATH') or data_root() / 'bookhub.sqlite3').resolve()
