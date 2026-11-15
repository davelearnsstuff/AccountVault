# -----------------------------------------------------------------------------
# tests/test_hash_bounds.py
# A master hash is checked before it is verified.
#
# Verifying an Argon2 hash runs the work factors written inside it, and the
# hash is read from the file before anything is authenticated. These tests put
# hostile factors in the hash the way a crafted vault would, and check that
# nothing ever runs them: not the login, not a password change, and not the
# chooser looking at the file.
# -----------------------------------------------------------------------------

import pytest
from argon2 import PasswordHasher

from core import auth
from core import database
from core import encryption
from conftest import MASTER, NEW_MASTER, make_account, snapshot_rows

# Parses as Argon2id, and a verify would run four billion passes.
HOSTILE_TIME = '$argon2id$v=19$m=8,t=4294967295,p=1$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaA'
# 16 GiB of memory.
HOSTILE_MEMORY = '$argon2id$v=19$m=16777216,t=1,p=1$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaA'

HOSTILE = [
    pytest.param(HOSTILE_TIME, id='time'),
    pytest.param(HOSTILE_MEMORY, id='memory'),
    pytest.param(PasswordHasher(time_cost=1, memory_cost=8, parallelism=1,
                                type=encryption.Type.I).hash(MASTER), id='argon2i'),
    pytest.param('not a hash at all', id='garbage'),
]


@pytest.fixture
def never_verify(monkeypatch):
    """Fails the test if anything tries to run a hash. A stand-in rather than a
    patched method: PasswordHasher uses slots."""
    real = auth.ph

    class Refusing:
        def verify(self, *args, **kwargs):
            raise AssertionError('a hash was verified')

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(auth, 'ph', Refusing())


def store_hash(value: str):
    database.write_config('master_hash', value)


# -- what is accepted -------------------------------------------------------------

def test_the_hash_this_app_writes_is_valid(vault):
    assert encryption.valid_password_hash(database.read_config('master_hash'))


def test_the_production_work_factors_are_valid():
    """The real settings, not the cheap ones the suite runs under."""
    hasher = PasswordHasher(time_cost=encryption.ARGON2_TIME_COST,
                            memory_cost=encryption.ARGON2_MEMORY_COST,
                            parallelism=encryption.ARGON2_PARALLELISM)
    assert encryption.valid_password_hash(hasher.hash('x'))


def test_argon2_cffis_defaults_are_valid():
    """What a vault hashed before the work factors were raised carries."""
    assert encryption.valid_password_hash(PasswordHasher().hash('x'))


# -- what is refused, before anything runs ----------------------------------------

@pytest.mark.parametrize('hostile', HOSTILE)
def test_a_hostile_hash_is_invalid(hostile):
    assert not encryption.valid_password_hash(hostile)


@pytest.mark.parametrize('hostile', HOSTILE)
def test_a_login_refuses_it_without_verifying(vault, never_verify, hostile):
    auth.lock_app()
    store_hash(hostile)

    with pytest.raises(auth.VaultConfigError, match='password record'):
        auth.verify_master_password(MASTER)

    assert encryption.is_unlocked() is False


def test_a_refusal_is_not_counted_as_a_failed_login(vault):
    """No password gets past it, so a lockout would only make the owner retype
    a correct one."""
    auth.lock_app()
    store_hash(HOSTILE_TIME)

    for _ in range(auth._MAX_ATTEMPTS + 1):
        with pytest.raises(auth.VaultConfigError):
            auth.verify_master_password(MASTER)

    assert auth.check_login_rate_limit() == (True, 0)
    assert auth._read_failed_attempts() == 0


def test_a_password_change_refuses_it_without_verifying(vault, never_verify):
    make_account(name='Bank', password='s3cret')
    store_hash(HOSTILE_TIME)
    before = snapshot_rows()

    with pytest.raises(auth.VaultConfigError):
        auth.change_master_password(MASTER, NEW_MASTER)

    assert snapshot_rows() == before


@pytest.mark.parametrize('hostile', HOSTILE)
def test_the_chooser_reports_it_as_damaged(vault, hostile):
    store_hash(hostile)
    assert database.inspect_vault(database.DB_PATH) == 'damaged'
