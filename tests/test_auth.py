import json

import pytest

from gcal_sync import auth
from gcal_sync.errors import AuthenticationError


class FakeCreds:
    def __init__(self, expired, refresh_token, valid_after_refresh=True):
        self.expired = expired
        self.refresh_token = refresh_token
        self._valid_after_refresh = valid_after_refresh
        self.valid = not expired
        self.refreshed = False

    def refresh(self, request):
        self.refreshed = True
        self.expired = False
        self.valid = self._valid_after_refresh

    def to_json(self):
        return json.dumps({"refreshed": self.refreshed})


def test_load_credentials_refreshes_expired_token_automatically(tmp_path, monkeypatch):
    token_path = tmp_path / "token_workspace.json"
    token_path.write_text(json.dumps({"refresh_token": "abc"}))

    fake = FakeCreds(expired=True, refresh_token="abc")
    monkeypatch.setattr(auth.Credentials, "from_authorized_user_file", classmethod(lambda cls, *a, **kw: fake))

    creds = auth.load_credentials("workspace", str(tmp_path))

    assert creds.refreshed is True
    assert json.loads(token_path.read_text()) == {"refreshed": True}


def test_load_credentials_raises_when_no_token_stored(tmp_path):
    with pytest.raises(AuthenticationError):
        auth.load_credentials("workspace", str(tmp_path))


def test_load_credentials_raises_when_refresh_fails(tmp_path, monkeypatch):
    token_path = tmp_path / "token_workspace.json"
    token_path.write_text(json.dumps({"refresh_token": "abc"}))

    class FailingCreds(FakeCreds):
        def refresh(self, request):
            raise RuntimeError("network down")

    fake = FailingCreds(expired=True, refresh_token="abc")
    monkeypatch.setattr(auth.Credentials, "from_authorized_user_file", classmethod(lambda cls, *a, **kw: fake))

    with pytest.raises(AuthenticationError):
        auth.load_credentials("workspace", str(tmp_path))
