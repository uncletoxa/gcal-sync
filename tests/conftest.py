import pytest

from gcal_sync.db import Database

from fake_google_client import FakeCalendarClient


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "state.sqlite3"))
    yield database
    database.close()


@pytest.fixture
def fake_client():
    return FakeCalendarClient()
