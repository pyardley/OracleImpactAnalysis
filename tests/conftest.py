import pytest

from oia.storage.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "test_oia.sqlite")
    yield s
    s.close()
