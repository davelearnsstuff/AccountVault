# -----------------------------------------------------------------------------
# tests/test_search.py
# Which fields a search query is allowed to touch.
#
# This is a security property rather than a cosmetic one, which is why it is
# tested at all: a search that matches a secret confirms the secret. Type a
# password into the box and the single account left in the list is the account
# using it — the value is never drawn on screen and never needs to be.
# -----------------------------------------------------------------------------

import pytest

from categories import CATEGORY_CONFIG, get_config, searchable_fields


def _keys(category):
    return {field['key'] for field in searchable_fields(category)}


@pytest.mark.parametrize('category', sorted(CATEGORY_CONFIG))
def test_no_sensitive_field_is_searchable(category):
    """Every field the schema marks sensitive, and every password, stays out."""
    searchable = _keys(category)
    for field in get_config(category)['fields']:
        if field.get('sensitive') or field['type'] == 'password':
            assert field['key'] not in searchable, (
                f"{category}.{field['key']} is sensitive but searchable"
            )


@pytest.mark.parametrize('category, key', [
    ('Credit Cards', 'card_number'),
    ('Banking', 'account_number'),
    ('Banking', 'routing_number'),
    ('Websites', 'security_question'),
    ('Websites', 'security_answer'),
    ('Secure Notes', 'content'),
])
def test_named_secrets_are_excluded(category, key):
    """Spelled out per field, so removing a `sensitive` flag from the schema
    fails here by name rather than quietly widening the search."""
    assert key not in _keys(category)


def test_custom_category_still_excludes_its_password():
    """Unknown categories fall back to GENERIC_CONFIG, which has a password of
    its own — the fallback must not be a way around the rule."""
    searchable = _keys('Some Category That Does Not Exist')
    assert 'password' not in searchable
    assert 'name' in searchable


@pytest.mark.parametrize('category', sorted(CATEGORY_CONFIG))
def test_something_is_still_searchable(category):
    """A filter that excluded everything would pass the tests above and leave
    the box permanently returning nothing."""
    assert 'name' in _keys(category)
