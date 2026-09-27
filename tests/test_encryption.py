# -----------------------------------------------------------------------------
# tests/test_encryption.py
# The AES-GCM envelope itself.
#
# Covered indirectly everywhere — every stored field goes through it — but the
# two properties that matter most are ones a round-trip test cannot see: that no
# nonce is ever reused under a key, and that a token which has been shortened is
# refused as forcefully as one that has been tampered with. Both are asserted
# here directly.
# -----------------------------------------------------------------------------

import base64

import pytest
from cryptography.exceptions import InvalidTag

import encryption


# -- the envelope --------------------------------------------------------------

def test_a_value_round_trips(vault):
    assert encryption.decrypt(encryption.encrypt('hunter2')) == 'hunter2'


def test_the_ciphertext_does_not_contain_the_plaintext(vault):
    token = encryption.encrypt('hunter2')
    assert 'hunter2' not in token
    assert b'hunter2' not in base64.urlsafe_b64decode(token)


def test_the_same_plaintext_never_encrypts_to_the_same_token(vault):
    """A repeated nonce under one key is what breaks GCM outright, and equal
    ciphertexts would also tell an observer which two accounts share a password
    without decrypting either."""
    tokens = {encryption.encrypt('the same value') for _ in range(200)}
    assert len(tokens) == 200


def test_every_nonce_in_a_run_is_distinct(vault):
    nonces = {base64.urlsafe_b64decode(encryption.encrypt('x'))[:encryption._NONCE_LEN]
              for _ in range(500)}
    assert len(nonces) == 500


def test_the_nonce_is_the_documented_length(vault):
    raw = base64.urlsafe_b64decode(encryption.encrypt(''))
    assert encryption._NONCE_LEN == 12
    assert len(raw) == encryption._NONCE_LEN + encryption._TAG_LEN


def test_an_empty_string_still_round_trips(vault):
    assert encryption.decrypt(encryption.encrypt('')) == ''


def test_unicode_survives_the_envelope(vault):
    value = 'café ⚠ 密码 €1.200,00'
    assert encryption.decrypt(encryption.encrypt(value)) == value


# -- what a damaged token has to do --------------------------------------------

def test_a_tampered_token_will_not_open(vault):
    from conftest import corrupt
    with pytest.raises(InvalidTag):
        encryption.decrypt(corrupt(encryption.encrypt('hunter2')))


def test_a_token_shorter_than_an_envelope_is_a_failed_authentication(vault):
    """InvalidTag rather than the ValueError AESGCM would raise on a short
    nonce. A truncated token has failed authentication just as surely as a
    tampered one, and every caller is already written to handle that."""
    for length in range(0, encryption._MIN_TOKEN_LEN):
        token = base64.urlsafe_b64encode(b'\x00' * length).decode()
        with pytest.raises(InvalidTag):
            encryption.decrypt(token)


def test_a_token_from_a_different_key_will_not_open(vault):
    other = encryption.derive_key('a completely different password',
                                  encryption.generate_salt())
    foreign = encryption.encrypt_with(other, b'hunter2')
    with pytest.raises(InvalidTag):
        encryption.decrypt(foreign)


# -- the lock is not advisory ---------------------------------------------------

def test_encrypting_while_locked_raises(vault):
    encryption.release_key()
    with pytest.raises(encryption.VaultLockedError):
        encryption.encrypt('hunter2')


def test_decrypting_while_locked_raises(vault):
    token = encryption.encrypt('hunter2')
    encryption.release_key()
    with pytest.raises(encryption.VaultLockedError):
        encryption.decrypt(token)


def test_releasing_the_key_reports_the_vault_locked(vault):
    assert encryption.is_unlocked() is True
    encryption.release_key()
    assert encryption.is_unlocked() is False


# -- telling "never encrypted" from "encrypted and broken" ---------------------

def test_prose_long_enough_to_decode_is_not_mistaken_for_ciphertext(vault):
    """urlsafe_b64decode discards characters outside the alphabet, so a long
    enough note decodes to something token-sized. Read as ciphertext, that field
    is skipped by the legacy migration and left in the clear, then aborts every
    future password rotation when it will not decrypt."""
    prose = ('This is a note the user typed. It is long enough that the '
             'characters base64 recognises add up to more than an envelope, '
             'which is exactly the trap: !!! ... ??? ***')
    assert encryption.looks_encrypted(prose) is False
