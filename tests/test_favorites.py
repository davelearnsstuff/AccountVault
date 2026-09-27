# -----------------------------------------------------------------------------
# tests/test_favorites.py
# Pinning an account, and what pinning is careful not to do.
#
# The flag itself is one key in extra_data, which is barely worth testing. What
# is worth testing is everything set_favorite leaves alone: it must not move
# last_updated, must not file a balance-history entry, and must not rewrite a
# blob it cannot read. Each of those is a way a pin could quietly destroy or
# misstate something the user did not ask it to touch.
# -----------------------------------------------------------------------------

import pytest

from conftest import corrupt_column, make_account, snapshot_rows
from categories import FAVORITE_KEY, is_favorite
from services import accounts as accounts_service


def test_a_new_account_is_not_pinned(vault):
    account = make_account(name='Chase')

    assert is_favorite(account) is False
    assert accounts_service.favorite_count() == 0


def test_pinning_and_unpinning_round_trip(vault):
    account = make_account(name='Chase')

    assert accounts_service.set_favorite(account['id'], True) is True
    assert is_favorite(accounts_service.get_account(account['id'])) is True
    assert accounts_service.favorite_count() == 1

    assert accounts_service.set_favorite(account['id'], False) is False
    assert is_favorite(accounts_service.get_account(account['id'])) is False
    assert accounts_service.favorite_count() == 0


def test_unpinning_drops_the_key_rather_than_storing_a_false(vault):
    """An account that was never pinned and one that has been unpinned should
    be stored identically, so extra_data carries the key only while it means
    something."""
    account = make_account(name='Chase')
    accounts_service.set_favorite(account['id'], True)
    accounts_service.set_favorite(account['id'], False)

    assert FAVORITE_KEY not in accounts_service.get_account(account['id'])


def test_pinning_leaves_the_last_updated_stamp_alone(vault):
    """That stamp says when the record was last known good and it drives the
    staleness colouring. Pinning verifies nothing about what is inside the
    account, so moving it would clear a warning the user still needs."""
    account = make_account(name='Chase', current_balance='100')
    with_old_stamp = '2020-01-01'
    accounts_service.update_account(account['id'], {**account, 'name': 'Chase'})
    import database
    with database.connect() as db:
        db.execute("UPDATE accounts SET last_updated = ? WHERE id = ?",
                   (with_old_stamp, account['id']))
    accounts_service.invalidate_cache()

    accounts_service.set_favorite(account['id'], True)

    assert accounts_service.get_account(account['id'])['last_updated'] == with_old_stamp


def test_pinning_does_not_file_a_balance_snapshot(vault):
    """Going through update_account would force one, so an account would grow a
    history entry claiming its balance had been re-read every time it was
    pinned."""
    account = make_account(name='Chase', category='Banking', current_balance='100')
    before = len(accounts_service.list_balance_history(account['id']))

    accounts_service.set_favorite(account['id'], True)
    accounts_service.set_favorite(account['id'], False)

    assert len(accounts_service.list_balance_history(account['id'])) == before


def test_pinning_keeps_every_other_stored_field(vault):
    account = make_account(name='Chase', category='Credit Cards',
                           card_number='4111111111111111', credit_limit='5000')

    accounts_service.set_favorite(account['id'], True)

    stored = accounts_service.get_account(account['id'])
    assert stored['card_number'] == '4111111111111111'
    assert stored['credit_limit'] == '5000'


def test_an_unreadable_blob_is_refused_rather_than_overwritten(vault):
    """extra_data that will not open decodes to {}, and writing that back would
    drop every category-specific field at once. A pin is not worth that."""
    account = make_account(name='Chase', category='Credit Cards',
                           card_number='4111111111111111')
    corrupt_column(account['id'], 'extra_data')
    accounts_service.invalidate_cache()
    before = snapshot_rows()

    with pytest.raises(ValueError):
        accounts_service.set_favorite(account['id'], True)

    assert snapshot_rows() == before


def test_pinning_an_account_that_is_not_there(vault):
    with pytest.raises(accounts_service.AccountNotFound):
        accounts_service.set_favorite(9999, True)


def test_a_closed_account_keeps_its_pin(vault):
    """The Favorites view lists what is pinned, closed accounts included: a pin
    is the user saying they want this one to hand, and settling an account does
    not withdraw that."""
    account = make_account(name='Chase')
    accounts_service.set_favorite(account['id'], True)

    accounts_service.set_closed([account['id']], True)

    assert accounts_service.favorite_count() == 1


def test_an_edit_does_not_silently_unpin(vault):
    """The account form carries forward every key the record holds, which is
    what stops a save rebuilding extra_data without the fields the current
    category has no input for. The pin rides on that."""
    account = make_account(name='Chase')
    accounts_service.set_favorite(account['id'], True)

    stored = accounts_service.get_account(account['id'])
    accounts_service.update_account(account['id'], {**stored, 'name': 'Chase Renamed'})

    fresh = accounts_service.get_account(account['id'])
    assert fresh['name'] == 'Chase Renamed'
    assert is_favorite(fresh) is True


@pytest.mark.parametrize('stored, expected', [
    (True, True), (1, True), ('1', True), ('true', True), ('yes', True),
    (False, False), (0, False), ('0', False), ('false', False), (None, False),
])
def test_is_favorite_reads_what_json_and_a_csv_can_produce(stored, expected):
    """The value has been through JSON, and for an imported record through a
    CSV cell as well — so the string '0' must not read as pinned merely by
    being a non-empty string."""
    assert is_favorite({FAVORITE_KEY: stored}) is expected


# -- the view -----------------------------------------------------------------

def test_the_favorites_tab_appears_only_when_something_is_pinned(vault, qt_app):
    from conftest import _destroy
    from ui.account_list import ALL, FAVORITES
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        window._enter_vault('test')
        account = make_account(name='Chase')
        window.load_category(ALL)
        assert FAVORITES not in window._tab_order

        accounts_service.set_favorite(account['id'], True)
        window.load_category(ALL)
        assert FAVORITES in window._tab_order
        assert window.favorite_count == 1

        window.load_category(FAVORITES)
        assert [a['name'] for a in window.accounts] == ['Chase']

        accounts_service.set_favorite(account['id'], False)
        window.load_category(ALL)
        assert FAVORITES not in window._tab_order
    finally:
        _destroy(window, qt_app)


def test_the_favorites_view_offers_no_add_button(vault, qt_app):
    """It is a selection out of accounts that already exist, so there is no
    category a new record created here could be filed under."""
    from conftest import _destroy
    from ui.account_list import FAVORITES
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        window._enter_vault('test')
        account = make_account(name='Chase')
        accounts_service.set_favorite(account['id'], True)

        window.load_category(FAVORITES)

        assert not window.add_button.isVisible()
    finally:
        _destroy(window, qt_app)
