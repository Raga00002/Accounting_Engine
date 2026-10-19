import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import db  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path):
    p = tmp_path / "test.db"
    db.init_db(p)
    return p
