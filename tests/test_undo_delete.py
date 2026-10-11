# -----------------------------------------------------------------------------
# tests/test_undo_delete.py
# Deleting an account, and putting it back.
#
# This is a data-loss path, so it gets the same treatment as the rotation: what
# is asserted is not that the happy case works but that the restore is exact
# and that the offer expires. A restore that came back with the history missing,
# or the id renumbered, or offered into a vault whose key cannot open the rows,
# would each be a worse outcome than having had no undo at all — the user would
# believe the account was back.
# -----------------------------------------------------------------------------

import pytest

from conftest import _destroy, make_account, raw_column
from services import accounts as accounts_service


def _history_rows(account_id):
    from core import database
    with database.connect() as db:
        return [tuple(r) for r in db.execute(
            "SELECT * FROM balance_history WHERE account_id = ? ORDER BY id",
            (account_id,))]


def test_delete_hands_back_a_token_and_the_account_is_gone(vault):
    account = make_account(name='Chase', category='Banking', current_balance='100')

    token = accounts_service.delete_account(account['id'])

    assert accounts_service.list_accounts() == []
    assert token['account']['id'] == account['id']
    assert token['history']


def test_the_token_carries_ciphertext_rather_than_decrypted_fields(vault):
    """The undo window holds the token in memory. Ciphertext there costs no
    more exposure than the vault file already does; the plaintext password
    would."""
    account = make_account(name='Chase', password='hunter2-long-enough')
    stored = raw_column(account['id'], 'password')

    token = accounts_service.delete_account(account['id'])

    assert token['account']['password'] == stored
    assert 'hunter2-long-enough' not in str(token)


def test_restoring_brings_back_every_field(vault):
    account = make_account(name='Chase Visa', category='Credit Cards',
                           username='jsmith', password='hunter2-long-enough',
                           website='https://chase.com', notes='a note',
                           card_number='4111111111111111', current_balance='250')
    before = accounts_service.get_account(account['id'])

    token = accounts_service.delete_account(account['id'])
    restored_id = accounts_service.restore_account(token)

    assert restored_id == account['id']
    assert accounts_service.get_account(restored_id) == before


def test_restoring_brings_back_the_balance_history_too(vault):
    """An account restored without its history is not the account that was
    deleted, and the user asked for the delete to be undone rather than partly
    undone."""
    account = make_account(name='Chase', category='Banking', current_balance='100')
    accounts_service.add_balance_entry(account['id'], '250', '2026-01-05')
    accounts_service.add_balance_entry(account['id'], '300', '2026-02-05')
    before = _history_rows(account['id'])
    assert len(before) == 3

    token = accounts_service.delete_account(account['id'])
    assert _history_rows(account['id']) == []

    accounts_service.restore_account(token)

    assert _history_rows(account['id']) == before


def test_the_restored_row_keeps_its_original_id(vault):
    """accounts.id is AUTOINCREMENT, so sqlite never hands the number out
    again — which makes it free to reclaim, and reclaiming it is what keeps the
    balance_history rows pointing at the account they belong to."""
    first = make_account(name='First')
    second = make_account(name='Second')

    token = accounts_service.delete_account(first['id'])
    make_account(name='Third')
    restored = accounts_service.restore_account(token)

    assert restored == first['id']
    assert restored != second['id']
    assert {a['name'] for a in accounts_service.list_accounts()} == {
        'First', 'Second', 'Third'}


def test_the_restored_account_still_decrypts(vault):
    """The columns are written back verbatim rather than re-encrypted. Every
    field is bound to the column it lives in, so this is also the check that
    the restore put each one back where it came from."""
    account = make_account(name='Chase', password='hunter2-long-enough',
                           notes='a note that must survive')

    token = accounts_service.delete_account(account['id'])
    accounts_service.restore_account(token)

    fresh = accounts_service.get_account(account['id'])
    assert fresh['password'] == 'hunter2-long-enough'
    assert fresh['notes'] == 'a note that must survive'


def test_restoring_twice_is_refused(vault):
    account = make_account(name='Chase')
    token = accounts_service.delete_account(account['id'])
    accounts_service.restore_account(token)

    with pytest.raises(ValueError):
        accounts_service.restore_account(token)

    assert len(accounts_service.list_accounts()) == 1


def test_restoring_nothing_is_refused(vault):
    with pytest.raises(ValueError):
        accounts_service.restore_account({})


def test_deleting_an_account_that_is_not_there(vault):
    with pytest.raises(accounts_service.AccountNotFound):
        accounts_service.delete_account(9999)


def test_stray_history_in_a_token_is_filed_under_the_restored_account(vault):
    """The account_id is forced back onto the restored row rather than trusted
    from the token, so a mangled token cannot graft this history onto a
    different account."""
    account = make_account(name='Chase', category='Banking', current_balance='100')
    other = make_account(name='Other', category='Banking', current_balance='500')

    token = accounts_service.delete_account(account['id'])
    for entry in token['history']:
        entry['account_id'] = other['id']

    accounts_service.restore_account(token)

    assert all(row[1] == account['id'] for row in _history_rows(account['id']))


# -- the window -----------------------------------------------------------------

@pytest.fixture
def window(vault, qt_app):
    from ui.main_window import MainWindow

    window = MainWindow()
    window._enter_vault('test')
    yield window
    _destroy(window, qt_app)


def _delete_confirmed(window, account, monkeypatch):
    """Runs _delete_account with the confirmation answered Yes."""
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, 'question',
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    window._delete_account(account)


def test_the_window_offers_an_undo_and_it_works(window, monkeypatch):
    from ui.account_list import ALL

    make_account(name='Chase', category='Banking')
    window.load_category(ALL)
    account = window.accounts[0]

    _delete_confirmed(window, account, monkeypatch)
    assert accounts_service.list_accounts() == []
    assert window._undo_delete is not None
    # isHidden rather than isVisible: the window is never shown in these tests,
    # so every child reports itself invisible whatever it was asked to do.
    assert not window.toast._action.isHidden()
    assert window.toast._callback is not None

    window.toast._run_action()

    assert [a['name'] for a in accounts_service.list_accounts()] == ['Chase']
    assert window._undo_delete is None
    assert window.selected_id == account['id']


def test_the_offer_is_dropped_when_the_vault_locks(window, monkeypatch):
    """A token is rows encrypted under the key that was live when they were
    deleted. Restored after a lock it would be written into whatever vault is
    open next, as rows nothing there can open — an account made permanently
    unreadable by a button labelled Undo."""
    from ui.account_list import ALL

    make_account(name='Chase')
    window.load_category(ALL)
    account = window.accounts[0]

    _delete_confirmed(window, account, monkeypatch)
    token = window._undo_delete
    assert token is not None

    window.lock_vault()

    assert window._undo_delete is None
    # The toast itself is up again — lock_vault says 'Locked.' through it — but
    # it is carrying no offer, which is the part that matters.
    assert window.toast._callback is None
    assert window.toast._action.isHidden()


def test_an_undo_whose_token_is_no_longer_held_does_nothing(window, monkeypatch):
    from ui.account_list import ALL

    make_account(name='Chase')
    window.load_category(ALL)
    account = window.accounts[0]

    _delete_confirmed(window, account, monkeypatch)
    token = window._undo_delete
    window._undo_delete = None

    window._undo_last_delete(token)

    assert accounts_service.list_accounts() == []


def test_declining_the_confirmation_deletes_nothing(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from ui.account_list import ALL

    make_account(name='Chase')
    window.load_category(ALL)
    account = window.accounts[0]

    monkeypatch.setattr(QMessageBox, 'question',
                        staticmethod(lambda *a, **k: QMessageBox.No))
    window._delete_account(account)

    assert len(accounts_service.list_accounts()) == 1
    assert window._undo_delete is None
