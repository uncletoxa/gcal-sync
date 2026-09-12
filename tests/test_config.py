import pytest

from gcal_sync.config import _parse_pairs

CALENDARS = {"workspace": "workspace-id", "personal": "personal-id"}


def test_parse_pairs_empty_string_yields_no_pairs():
    assert _parse_pairs("", CALENDARS) == frozenset()


def test_parse_pairs_single_directed_pair():
    assert _parse_pairs("workspace:personal", CALENDARS) == frozenset({("workspace", "personal")})


def test_parse_pairs_multiple_pairs_are_independent_directions():
    result = _parse_pairs("workspace:personal,personal:workspace", CALENDARS)
    assert result == frozenset({("workspace", "personal"), ("personal", "workspace")})


def test_parse_pairs_rejects_unknown_account():
    with pytest.raises(SystemExit):
        _parse_pairs("workspace:ghost", CALENDARS)


def test_parse_pairs_rejects_self_pair():
    with pytest.raises(SystemExit):
        _parse_pairs("workspace:workspace", CALENDARS)


def test_parse_pairs_rejects_malformed_entry():
    with pytest.raises(SystemExit):
        _parse_pairs("workspace-personal", CALENDARS)
