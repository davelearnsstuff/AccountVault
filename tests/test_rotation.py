# -----------------------------------------------------------------------------
# tests/test_rotation.py
# change_master_password, and the work-factor invariant it depends on.
#
# Rotation is all-or-nothing on purpose: a vault half under the old key and half
# under the new one has no password that opens all of it. The abort path is the
# most destructive thing in the codebase if it ever stops working, and it is
# also the one a user is least likely to exercise before they need it.
# -----------------------------------------------------------------------------

import pytest

from core import auth
from core import encryption
from conftest import MASTER, NEW_MASTER, corrupt_column, make_account, snapshot_rows
from services import accounts as accounts_service


def test_rotation_rekeys_every_field_and_the_history(vault):
    account = make_account(
        name='Bank', username='dave', password='s3cret',
        notes='a note', current_balance=1200,
    )

    assert auth.change_master_password(MASTER, NEW_MASTER) is True

    # Round-trip through a real unlock rather than trusting the in-memory key
    # the rotation left behind.
    encryption.release_key()
    assert auth.verify_master_password(NEW_MASTER) is True

    fresh = accounts_service.get_account(account['id'])
    assert fresh['name'] == 'Bank'
    assert fresh['username'] == 'dave'
    assert fresh['password'] == 's3cret'
    assert fresh['notes'] == 'a note'
    assert fresh['current_balance'] == 1200
    assert accounts_service.list_balance_history(account['id'])[0]['balance'] == '1200'


def test_the_old_password_stops_working_after_rotation(vault):
    make_account(password='s3cret')

    assert auth.change_master_password(MASTER, NEW_MASTER) is True

    encryption.release_key()
    assert auth.verify_master_password(MASTER) is False


def test_rotation_aborts_and_changes_nothing_when_a_field_will_not_decrypt(vault):
    """The critical path. That field is already unreadable; carrying on would
    re-encrypt the unreadable token and lose the last copy of what it held."""
    account = make_account(category='Websites', username='dave', password='s3cret')
    corrupt_column(account['id'], 'password')
    before = snapshot_rows()

    with pytest.raises(auth.ReencryptionError):
        auth.change_master_password(MASTER, NEW_MASTER)

    # Not a single byte moved — including the master hash and the salt.
    assert snapshot_rows() == before

    # The old password still opens the vault, and the readable fields survived.
    encryption.release_key()
    assert auth.verify_master_password(MASTER) is True
    assert accounts_service.get_account(account['id'])['username'] == 'dave'


def test_rotation_aborts_on_broken_balance_history_too(vault):
    """balance_history.balance is in ENCRYPTED_FIELDS, so it is re-keyed as well
    and gets the same all-or-nothing treatment."""
    from core import database

    account = make_account(current_balance=1200)
    with database.connect() as db:
        row = db.execute(
            "SELECT id, balance FROM balance_history WHERE account_id = ?",
            (account['id'],),
        ).fetchone()
        from conftest import corrupt
        db.execute("UPDATE balance_history SET balance = ? WHERE id = ?",
                   (corrupt(row['balance']), row['id']))
    before = snapshot_rows()

    with pytest.raises(auth.ReencryptionError):
        auth.change_master_password(MASTER, NEW_MASTER)

    assert snapshot_rows() == before


def test_rotation_surfaces_a_locked_vault_rather_than_calling_it_corruption(vault):
    """A vault that closed underneath the rotation is not a corrupt field. Told
    it was one, the user is sent to restore a backup over data that is perfectly
    intact — and the vault they would overwrite is the good copy."""
    make_account(password='s3cret')
    before = snapshot_rows()
    encryption.release_key()

    with pytest.raises(encryption.VaultLockedError):
        auth.change_master_password(MASTER, NEW_MASTER)

    assert snapshot_rows() == before


def test_rotation_rejects_a_wrong_current_password(vault):
    make_account(password='s3cret')
    before = snapshot_rows()

    assert auth.change_master_password('not the master password', NEW_MASTER) is False

    assert snapshot_rows() == before
    assert accounts_service.list_accounts()[0]['password'] == 's3cret'


# -- the invariant rotation rests on ------------------------------------------
#
# Deliberately no `vault` fixture: these read the real parameters, not the
# lowered ones conftest substitutes for speed.

def test_verifier_and_key_derivation_share_work_factors():
    """A stolen database exposes both the master hash and the key derivation,
    and either one recovers the password — so an attacker spends their time on
    whichever is cheaper. Tuned apart, the pair is only as strong as its weaker
    half."""
    assert auth.ph.time_cost == encryption.ARGON2_TIME_COST
    assert auth.ph.memory_cost == encryption.ARGON2_MEMORY_COST
    assert auth.ph.parallelism == encryption.ARGON2_PARALLELISM


def test_work_factors_clear_the_owasp_floor():
    """encryption.py claims to sit comfortably above 19 MiB / t=2."""
    assert encryption.ARGON2_MEMORY_COST >= 19 * 1024
    assert encryption.ARGON2_TIME_COST >= 2
