"""Test bootstrap.

drive_sync reads its config at import time. Setting these BEFORE the module is
imported keeps the suite hermetic: no .env is required, and a developer's real
.env cannot leak into the tests (`_load_env` uses os.environ.setdefault, so a
value already present in the environment wins).
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

os.environ.setdefault("LOCAL_FOLDER", str(REPO_ROOT / ".pytest-local-folder"))
os.environ.setdefault("DRIVE_FOLDER_ID", "ROOT")

sys.path.insert(0, str(REPO_ROOT))
