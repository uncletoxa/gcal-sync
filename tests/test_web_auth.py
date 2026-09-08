from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

import gcal_sync.cli as cli_module
from gcal_sync import web_auth
from gcal_sync.config import Config
from gcal_sync.crypto import TokenEncryptionError, decrypt, encrypt
from gcal_sync.db import Database
from gcal_sync.errors import AuthenticationError


def _key() -> str:
    return Fernet.generate_key().decode()


def test_crypto_round_trip():
    key = _key()
    ciphertext = encrypt("top secret refresh token", key)
    assert ciphertext != "top secret refresh token"
    assert decrypt(ciphertext, key) == "top secret refresh token"


def test_crypto_rejects_missing_key():
    with pytest.raises(TokenEncryptionError):
        encrypt("x", "")
    with pytest.raises(TokenEncryptionError):
        decrypt("x", "")


def test_crypto_wrong_key_fails_to_decrypt():
    ciphertext = encrypt("secret", _key())
    with pytest.raises(TokenEncryptionError):
        decrypt(ciphertext, _key())


def test_tenant_account_key_is_namespaced_per_tenant():
    assert web_auth.tenant_account_key(7, "personal") == "t7:personal"
    assert web_auth.tenant_account_key(9, "personal") == "t9:personal"
    assert web_auth.tenant_account_key(7, "personal") != web_auth.tenant_account_key(9, "personal")


def _cfg(db_path, allowed_domain: str = "") -> Config:
    return Config(
        client_secrets_file="unused",
        token_dir="unused",
        db_path=str(db_path),
        calendars={},
        poll_interval_seconds=1,
        full_resync_interval_hours=24,
        sync_window_days=14,
        log_level="INFO",
        allowed_domain=allowed_domain,
    )


def test_check_domain_allowed_permits_matching_domain(tmp_path):
    web_auth.check_domain_allowed("alice@company.com", _cfg(tmp_path, "company.com"))


def test_check_domain_allowed_is_case_insensitive(tmp_path):
    web_auth.check_domain_allowed("Alice@Company.com", _cfg(tmp_path, "COMPANY.COM"))


def test_check_domain_allowed_rejects_other_domain(tmp_path):
    with pytest.raises(AuthenticationError):
        web_auth.check_domain_allowed("alice@gmail.com", _cfg(tmp_path, "company.com"))


def test_check_domain_allowed_rejects_suffix_spoof(tmp_path):
    """A domain suffix match isn't enough — 'evilcompany.com' must not pass for 'company.com'."""
    with pytest.raises(AuthenticationError):
        web_auth.check_domain_allowed("alice@evilcompany.com", _cfg(tmp_path, "company.com"))


def test_check_domain_allowed_permits_anything_when_unset(tmp_path):
    web_auth.check_domain_allowed("alice@anywhere.com", _cfg(tmp_path))


def test_single_account_tenant_is_skipped(tmp_path):
    db = Database(str(tmp_path / "s.sqlite3"))
    tenant = db.get_or_create_tenant("solo@example.com")
    db.upsert_connected_account(
        tenant_id=tenant["id"], account_label="solo@example.com",
        google_email="solo@example.com", calendar_id="solo@example.com",
        credentials_json="cipher",
    )

    ran = cli_module._run_tenant_sync_passes(_cfg(tmp_path / "s.sqlite3"), db, dry_run=True)

    assert ran == 0
    db.close()


def test_tenant_sync_passes_are_namespaced_and_isolated(tmp_path, monkeypatch):
    """Two tenants who both label an account 'personal' must sync as fully separate groups."""
    db = Database(str(tmp_path / "s.sqlite3"))
    tenant_ids = []
    for email in ("a@example.com", "b@example.com"):
        tenant = db.get_or_create_tenant(email)
        tenant_ids.append(tenant["id"])
        for label in ("personal", "work"):
            db.upsert_connected_account(
                tenant_id=tenant["id"], account_label=label,
                google_email=f"{label}-{email}", calendar_id=f"{label}-{email}",
                credentials_json="cipher",
            )

    monkeypatch.setattr(web_auth, "load_account_credentials", lambda db, cfg, acc: object())
    monkeypatch.setattr(web_auth, "GoogleCalendarClient", lambda creds, label: object())

    seen: list[set[str]] = []

    def fake_run_sync_pass(cfg, db, clients, dry_run=False):
        assert set(cfg.calendars.keys()) == set(clients.keys())
        seen.append(set(cfg.calendars.keys()))
        return {}

    monkeypatch.setattr(web_auth, "run_sync_pass", fake_run_sync_pass)

    ran = cli_module._run_tenant_sync_passes(_cfg(tmp_path / "s.sqlite3"), db, dry_run=True)

    assert ran == 2
    assert len(seen) == 2
    key_sets = seen
    # no account keys leak between the two tenants, despite identical labels
    assert not (key_sets[0] & key_sets[1])
    for tid, keys in zip(tenant_ids, key_sets):
        assert keys == {f"t{tid}:personal", f"t{tid}:work"}

    db.close()
