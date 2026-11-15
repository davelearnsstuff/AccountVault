# -----------------------------------------------------------------------------
# tests/test_small_items.py
# The last five items of the security review, each small.
#
# Links in the document viewers, tooltips that quote a path, mailto: links,
# the demo vault's public password, and CSV cells that only look like text.
# -----------------------------------------------------------------------------

import pytest

from core import database
from core import preferences
from core.urls import normalize_url
from conftest import _destroy
from services.export_io import defuse, undefuse


# -- links in the document viewers ------------------------------------------------

@pytest.fixture
def viewer(qt_app, tmp_path, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    from ui.dialogs import DocumentDialog
    path = tmp_path / 'doc.md'
    path.write_text('# Title\n\nBody.\n', encoding='utf-8')
    opened = []
    monkeypatch.setattr(QDesktopServices, 'openUrl',
                        staticmethod(lambda url: opened.append(url.toString())))
    dialog = DocumentDialog(None, 'Doc', path)
    dialog.opened = opened
    yield dialog
    dialog.deleteLater()


@pytest.mark.parametrize('link', ['docs/AccountVault_Design_Document.md', 'README.md',
                                  '../elsewhere', 'javascript:alert(1)', 'file:///C:/x'])
def test_a_link_that_is_not_a_full_web_address_goes_nowhere(viewer, link):
    from PySide6.QtCore import QUrl
    viewer._follow(QUrl(link))
    assert viewer.opened == []


def test_an_in_page_link_scrolls_instead(viewer, monkeypatch):
    from PySide6.QtCore import QUrl
    scrolled = []
    monkeypatch.setattr(viewer.view, 'scrollToAnchor', scrolled.append)
    viewer._anchor_clicked(QUrl('#security-details'))
    assert scrolled == ['security-details']
    assert viewer.opened == []


@pytest.mark.parametrize('link', ['https://www.python.org/downloads/',
                                  'mailto:someone@example.com'])
def test_a_full_web_or_mail_link_opens(viewer, link):
    from PySide6.QtCore import QUrl
    viewer._follow(QUrl(link))
    assert viewer.opened == [link]


# -- tooltips that quote a path ---------------------------------------------------

def test_a_vault_name_in_a_tooltip_is_shown_as_written(qt_app, tmp_path):
    from ui.auth_screens import LoginScreen
    screen = LoginScreen()
    screen.set_vault(tmp_path / '<b>bold</b>.vault')
    assert '&lt;b&gt;' in screen.vault_caption.toolTip()
    screen.deleteLater()


# -- mailto: ----------------------------------------------------------------------

@pytest.mark.parametrize('value, expected', [
    ('mailto:someone@example.com', 'mailto:someone@example.com'),
    ('mailto:someone@example.com?attach=C:/Users/me/secrets.txt', 'mailto:someone@example.com'),
    ('MAILTO:someone@example.com?subject=x&attachment=~/.ssh/id_rsa',
     'MAILTO:someone@example.com'),
    ('mailto:someone@example.com#fragment', 'mailto:someone@example.com'),
    ('mailto:?attach=C:/x', ''),
    ('mailto:', ''),
])
def test_a_mailto_link_keeps_only_its_address(value, expected):
    assert normalize_url(value) == expected


def test_web_links_are_unchanged():
    assert normalize_url('https://example.com/a?b=c#d') == 'https://example.com/a?b=c#d'


# -- the demo vault ---------------------------------------------------------------

@pytest.fixture
def window(vault, qt_app):
    from ui.main_window import MainWindow
    window = MainWindow()
    yield window
    _destroy(window, qt_app)


def test_an_ordinary_vault_carries_no_demo_warning(window):
    assert preferences.is_demo_vault() is False
    window._show_login()
    assert window.login_screen.demo_notice.isHidden()
    window._enter_vault('test')
    assert window.demo_chip.isHidden()


def test_a_demo_vault_is_flagged_on_the_login_screen_and_while_open(window):
    preferences.mark_demo_vault()

    window._show_login()
    assert not window.login_screen.demo_notice.isHidden()

    window._enter_vault('test')
    assert not window.demo_chip.isHidden()

    window.close_vault()
    assert window.demo_chip.isHidden()


def test_the_demo_builder_marks_what_it_builds(tmp_path, monkeypatch, cheap_argon2):
    from tools import make_demo_vault
    monkeypatch.setattr(database, 'DB_PATH', None)
    monkeypatch.setattr(database, 'restrict_to_owner', lambda path: True)
    try:
        make_demo_vault.build_vault(tmp_path / 'demo.vault')
        assert preferences.is_demo_vault() is True
    finally:
        from core import auth
        auth.lock_app()
        database.close_vault()


# -- CSV cells that only look like text -------------------------------------------

@pytest.mark.parametrize('value', ['＝1+1', '＋1', '－1', '＠SUM(A1)', '  =1+1',
                                   '\u00a0=1+1', '\u3000=1+1', '﹦1+1'])
def test_a_lookalike_formula_is_marked_as_text(value):
    assert defuse(value) == "'" + value


@pytest.mark.parametrize('value', ['hello', ' hello', '', '   ', '1+1', 'a=b'])
def test_ordinary_text_is_left_alone(value):
    assert defuse(value) == value


@pytest.mark.parametrize('value', ['＝1+1', '  =1+1', "'＝1", "' =1", "'hello", ' hello',
                                   '=1', "'=1", 'plain'])
def test_every_value_survives_a_round_trip(value):
    assert undefuse(defuse(value)) == value


def test_an_apostrophe_before_ordinary_text_is_kept_on_import():
    assert undefuse("'hello") == "'hello"
    assert undefuse("' hello") == "' hello"
