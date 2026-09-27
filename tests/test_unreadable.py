# -----------------------------------------------------------------------------
# tests/test_unreadable.py
# The plaintext / ciphertext / broken-ciphertext distinction, and the write
# guards built on it.
#
# A field that will not decrypt is already lost. What these protect against is
# the second, worse outcome: the app overwriting it with a placeholder — or
# re-encrypting the token itself — and taking away any chance of recovering it
# from a backup.
# -----------------------------------------------------------------------------

import base64
import os

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import encryption
from conftest import corrupt, corrupt_column, make_account, raw_column
from services import accounts as accounts_service


# -- looks_encrypted ----------------------------------------------------------

def test_looks_encrypted_recognises_our_own_ciphertext(vault):
    assert encryption.looks_encrypted(encryption.encrypt('hunter2'))


@pytest.mark.parametrize('value', ['hunter2', '', None, 123, 'short'])
def test_looks_encrypted_rejects_non_ciphertext(vault, value):
    assert not encryption.looks_encrypted(value)


def test_looks_encrypted_still_says_yes_to_broken_ciphertext(vault):
    """The distinction that matters: broken ciphertext is encrypted, and must
    never be treated as plaintext and wrapped a second time."""
    assert encryption.looks_encrypted(corrupt(encryption.encrypt('hunter2')))


# -- safe_decrypt -------------------------------------------------------------

def test_safe_decrypt_passes_legacy_plaintext_through(vault):
    assert encryption.safe_decrypt('a value from before field encryption') == \
        'a value from before field encryption'


def test_safe_decrypt_marks_broken_ciphertext(vault):
    broken = corrupt(encryption.encrypt('secret'))
    assert encryption.safe_decrypt(broken) == encryption.UNREADABLE


def test_safe_decrypt_reraises_when_locked(vault):
    """A locked vault is not a decrypt failure. Swallowing it here would stamp
    placeholders over every field in a vault that is merely closed."""
    token = encryption.encrypt('secret')
    encryption.release_key()
    with pytest.raises(encryption.VaultLockedError):
        encryption.safe_decrypt(token)


# -- safe_reencrypt -----------------------------------------------------------

def test_safe_reencrypt_wraps_legacy_plaintext_under_the_new_key(vault):
    new_key = os.urandom(32)

    token = encryption.safe_reencrypt('legacy value', new_key)

    raw = base64.urlsafe_b64decode(token)
    assert AESGCM(new_key).decrypt(raw[:12], raw[12:], None).decode() == 'legacy value'


def test_safe_reencrypt_refuses_broken_ciphertext(vault):
    """Wrapping it would encrypt the base64 token and lose the real value for
    good — the new key returns the token, the old password opens nothing."""
    broken = corrupt(encryption.encrypt('secret'))
    with pytest.raises(InvalidTag):
        encryption.safe_reencrypt(broken, os.urandom(32))


# -- the write guards ---------------------------------------------------------

def test_saving_an_account_does_not_write_the_placeholder_over_a_column(vault):
    """The edit form round-trips whatever it was given, so a field showing
    UNREADABLE would be saved back as that literal text."""
    account = make_account(category='Websites', password='s3cret')
    broken = corrupt_column(account['id'], 'password')

    shown = accounts_service.get_account(account['id'])
    assert shown['password'] == encryption.UNREADABLE

    accounts_service.update_account(account['id'], shown)

    assert raw_column(account['id'], 'password') == broken


def test_saving_an_account_does_not_wipe_unreadable_extra_data(vault):
    """Unreadable extra_data decodes to {}; writing that back would drop every
    category-specific field at once."""
    account = make_account(category='Credit Cards', card_number='4111', notes='keep me')
    broken = corrupt_column(account['id'], 'extra_data')

    shown = accounts_service.get_account(account['id'])
    assert 'card_number' not in shown

    accounts_service.update_account(account['id'], shown)

    assert raw_column(account['id'], 'extra_data') == broken


def test_readable_fields_still_save_normally_alongside_a_broken_one(vault):
    """The guard is per field — one bad column must not freeze the whole record."""
    account = make_account(category='Websites', username='dave', password='s3cret')
    broken = corrupt_column(account['id'], 'password')

    shown = accounts_service.get_account(account['id'])
    shown['username'] = 'dave2'
    accounts_service.update_account(account['id'], shown)

    fresh = accounts_service.get_account(account['id'])
    assert fresh['username'] == 'dave2'
    assert raw_column(account['id'], 'password') == broken


def test_one_broken_field_does_not_hide_every_account(vault):
    """list_accounts uses safe_decrypt so a single corrupt column degrades to a
    placeholder instead of raising past the whole list."""
    good = make_account(name='Readable', category='Websites', password='fine')
    bad = make_account(name='Broken', category='Websites', password='s3cret')
    corrupt_column(bad['id'], 'password')

    listed = accounts_service.list_accounts()

    assert len(listed) == 2
    by_id = {a['id']: a for a in listed}
    assert by_id[good['id']]['password'] == 'fine'
    assert by_id[bad['id']]['password'] == encryption.UNREADABLE
