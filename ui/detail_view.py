# -----------------------------------------------------------------------------
# ui/detail_view.py
# Read-only account detail: the field list, the sensitive-value masking, the
# balance history table with its chart, and the per-account CSV export.
# -----------------------------------------------------------------------------

from functools import partial
from html import escape

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from core.categories import (
    DUE_CYCLE_KEY, account_category, due_date_level, format_value, get_config,
    is_favorite, lapsed_after_days, next_due_date, normalize_category,
    updated_age_level, value_noun,
)
from core.encryption import VaultLockedError
from core.urls import normalize_url
from services import account_export
from services import accounts as accounts_service
from services import export_io
from services.dashboard import LOWER_IS_BETTER
from ui.theme import LEVEL_SUFFIX, level_object_name
from ui.widgets import (
    IconButton, ThemedIcon, export_save_path, field_label, plain_label,
    plain_text, reveal_toggle,
)

MASK = '••••••••'

# Field grid width; long-text fields span the whole row instead.
_COLUMNS = 2

# A value wider than this wraps and claims the whole row; anything shorter is
# pinned to its text so the Copy / Show button stays beside it. Only safe
# because DETAIL_MIN_WIDTH keeps the panel wide enough for two such columns.
_WRAP_OVER = 240

# Margins around and between the grid's two columns, plus slack for the
# enclosing QScrollArea's frame and scrollbar.
_GRID_MARGIN = 40
_GRID_GAP = 28
_SCROLL_SLACK = 24

# The narrowest the detail panel may get; main_window sets it as the panel's
# minimum width.
DETAIL_MIN_WIDTH = _WRAP_OVER * _COLUMNS + _GRID_GAP + _GRID_MARGIN + _SCROLL_SLACK


def _export_summary(result: dict, scope: str, noun: str, restricted: bool = True) -> str:
    """What an export actually wrote, for the toast."""
    parts = []
    if scope != account_export.SCOPE_HISTORY:
        parts.append(f"{result['fields']} field{'s' if result['fields'] != 1 else ''}")
    if scope != account_export.SCOPE_ACCOUNT:
        count = result['entries']
        parts.append(f"{count:,} {noun.lower()} entr{'ies' if count != 1 else 'y'}")

    notes = []
    if result['unreadable']:
        notes.append(f"{result['unreadable']} unreadable entr"
                     f"{'ies' if result['unreadable'] != 1 else 'y'} left out")
    # Unlike a history entry, a field that will not decrypt is still in the
    # file, as its placeholder.
    if result.get('unreadable_fields'):
        count = result['unreadable_fields']
        notes.append(f"{count} field{'s' if count != 1 else ''} could not be decrypted")
    if not restricted:
        notes.append('permissions could not be restricted')

    tail = (', ' + ', '.join(notes)) if notes else ''
    return f"Exported {' and '.join(parts)}{tail}."


class DetailView(QWidget):
    """Renders one account. Emits requests upward; it never writes to the vault
    itself apart from balance-history edits, which it owns end to end."""

    edit_requested = Signal(dict)
    delete_requested = Signal(dict)
    favorite_toggled = Signal(dict, bool)   # account, the state asked for
    quick_update_requested = Signal(dict)
    touch_requested = Signal(dict)
    navigate_requested = Signal(int)       # -1 previous, +1 next in the list
    copy_requested = Signal(str, str)      # value, field label
    history_changed = Signal()
    error = Signal(str)
    notice = Signal(str)
    # A locked vault is the window's to answer.
    locked = Signal()

    def __init__(self, theme: str, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._account = None
        self._content = None
        self._prev_button = None
        self._next_button = None
        # Answers (has_previous, has_next). The list's order belongs to the
        # window, so this pane asks rather than keeping a copy.
        self._navigator = lambda: (False, False)

        # Rendering swaps a single content widget rather than rebuilding this
        # layout in place, so a redraw can never race a pending deletion.
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

    def set_theme(self, theme: str):
        """Re-renders the account on show in the new theme."""
        self._theme = theme
        if self._account:
            self.show_account(self._account)

    def set_navigator(self, navigator):
        """Takes a callable answering (has_previous, has_next) for the account
        on show."""
        self._navigator = navigator

    def refresh_navigation(self):
        """Re-asks the navigator, for when the list changed order under the
        account on show."""
        if self._prev_button is None:
            return
        has_previous, has_next = self._navigator()
        self._prev_button.setEnabled(has_previous)
        self._next_button.setEnabled(has_next)

    def clear(self):
        self._account = None
        self._drop_content()

    def _drop_content(self):
        self._prev_button = None
        self._next_button = None
        if self._content is None:
            return
        self._layout.removeWidget(self._content)
        self._content.setParent(None)
        self._content.deleteLater()
        self._content = None

    def show_account(self, account: dict):
        self._account = account
        self._drop_content()

        category = account.get('_category') or account.get('category')
        config = get_config(category)

        self._content = QWidget()
        layout = QVBoxLayout(self._content)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)
        layout.addLayout(self._build_header(account, config))
        layout.addLayout(self._build_fields(account, config))

        if any(f['key'] == 'current_balance' for f in config['fields']) and account.get('id'):
            layout.addWidget(self._build_history(account['id'], category))

        layout.addStretch(1)
        self._layout.addWidget(self._content)

    # -- header ------------------------------------------------------------

    def _build_header(self, account: dict, config: dict) -> QVBoxLayout:
        title = plain_label(account.get(config['name_field'])
                            or f"Untitled {config['item_label']}")
        title.setObjectName('DetailTitle')
        title.setWordWrap(True)

        # The pin is a property of the account, so it sits on the title line
        # rather than among the actions that change stored fields.
        pinned = is_favorite(account)
        star = IconButton(
            'star-filled' if pinned else 'star',
            'Unpin from Favorites' if pinned else 'Pin to Favorites',
            role='accent' if pinned else 'subtle', size=18,
            label='Unpin from Favorites' if pinned else 'Pin to Favorites',
        )
        star.setObjectName('NavIcon')
        star.setFixedWidth(34)
        star.clicked.connect(
            lambda: self.favorite_toggled.emit(account, not pinned))

        # Previous / next ahead of the name, so stepping through a list is one
        # spot to click repeatedly.
        self._prev_button = self._nav_arrow('chevron-left', 'Previous account', -1)
        self._next_button = self._nav_arrow('chevron-right', 'Next account', 1)

        # A strip one line tall keeps the arrows level with the title's first
        # line, rather than riding above it.
        title.ensurePolished()
        arrows = QWidget()
        arrows.setFixedHeight(max(title.fontMetrics().height(),
                                  self._prev_button.sizeHint().height()))
        arrows_layout = QHBoxLayout(arrows)
        arrows_layout.setContentsMargins(0, 0, 0, 0)
        arrows_layout.setSpacing(8)
        arrows_layout.addWidget(self._prev_button, 0, Qt.AlignVCenter)
        arrows_layout.addWidget(self._next_button, 0, Qt.AlignVCenter)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(arrows, 0, Qt.AlignTop)
        title_row.addSpacing(4)
        title_row.addWidget(title, 1)
        title_row.addWidget(star, 0, Qt.AlignTop)

        actions = QHBoxLayout()
        actions.setSpacing(8)

        keys = {f['key'] for f in config['fields']}
        has_balance, has_due = 'current_balance' in keys, 'due_date' in keys
        if has_balance or has_due:
            noun = value_noun(account.get('_category') or account.get('category'))
            fields = ([noun.lower()] if has_balance else []) + (['due date'] if has_due else [])
            actions.addWidget(self._action(
                'Quick Update', lambda: self.quick_update_requested.emit(account),
                tooltip=f"Change the {' or '.join(fields)} without opening the full form",
            ))

        actions.addWidget(self._action(
            'No Change', lambda: self.touch_requested.emit(account),
            tooltip='Refresh the last-updated date and record a balance snapshot',
        ))
        actions.addWidget(self._action(
            'Edit', lambda: self.edit_requested.emit(account),
            tooltip='Open the full form to change any field on this account',
        ))
        actions.addWidget(self._action(
            'Export', lambda: self._export_account(account, config),
            tooltip='Save this account to a CSV file — details, history, or both',
        ))

        # Always on screen, disabled until the account is closed, with a tooltip
        # that says why.
        closed = bool(account.get('closed'))
        delete = self._action(
            'Delete', lambda: self.delete_requested.emit(account),
            tooltip=('Delete this account and its history. There is a short '
                     'window to undo it afterwards.') if closed else
                    ('Only closed accounts can be deleted. Open Edit and tick '
                     '"Closed" first.'),
        )
        delete.setObjectName('Danger')
        delete.setEnabled(closed)
        actions.addWidget(delete)

        actions.addStretch(1)

        header = QVBoxLayout()
        header.setSpacing(10)
        header.addLayout(title_row)
        header.addLayout(actions)
        self.refresh_navigation()
        return header

    def _nav_arrow(self, icon: str, label: str, step: int) -> IconButton:
        button = IconButton(icon, label, role='subtle', size=18, label=label)
        button.setObjectName('NavIcon')
        button.setFixedWidth(34)
        button.clicked.connect(lambda: self.navigate_requested.emit(step))
        return button

    @staticmethod
    def _action(text: str, slot, tooltip: str = '') -> QPushButton:
        button = QPushButton(text)
        button.setObjectName('Small')
        if tooltip:
            button.setToolTip(tooltip)
        button.clicked.connect(lambda: slot())
        return button

    # -- fields ------------------------------------------------------------

    def _build_fields(self, account: dict, config: dict) -> QGridLayout:
        layout = QGridLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(28)
        layout.setVerticalSpacing(12)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)

        row, column = 0, 0
        cycle = account.get(DUE_CYCLE_KEY)
        for field in self._ordered_fields(config):
            value = account.get(field['key'])
            # A recurring due date shows the occurrence still ahead, not the
            # anchor on file.
            if field['key'] == 'due_date':
                value = next_due_date(value, cycle) or value
            # Blank optional fields are skipped; a blank required name still
            # shows, so the gap is visible.
            if not field.get('required') and (value is None or value == ''):
                continue

            # Long text gets its own full-width row, and is told so, because the
            # value is sized differently there.
            full_width = bool(field.get('type') == 'textarea' or field.get('multiline'))
            widget = self._build_field(field, value, full_width)
            if full_width:
                if column:
                    row, column = row + 1, 0
                layout.addWidget(widget, row, 0, 1, 2, Qt.AlignTop)
                row += 1
                continue

            layout.addWidget(widget, row, column, Qt.AlignTop)
            column += 1
            if column == _COLUMNS:
                row, column = row + 1, 0

        return layout

    @staticmethod
    def _ordered_fields(config: dict) -> list:
        """Website sits immediately after the name, whatever order the category
        declares, so the link is always in the same spot."""
        fields = list(config['fields'])
        website = next((f for f in fields if f['key'] == 'website'), None)
        if website is None:
            return fields
        fields.remove(website)
        after_name = next(
            (i for i, f in enumerate(fields) if f['key'] == config['name_field']), -1
        )
        fields.insert(after_name + 1, website)
        return fields

    def _build_field(self, field: dict, value, full_width: bool = False) -> QWidget:
        """Builds one field's label / value block."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        label_row = QHBoxLayout()
        label_row.setSpacing(8)
        label_row.addWidget(field_label(field['label']))

        row = QHBoxLayout()
        row.setSpacing(8)

        buttons = []
        if field.get('sensitive'):
            display, toggle = self._sensitive_value(field, value)
            buttons.append(toggle)
            fills = True
        elif field.get('link') and value:
            display = self._link_value(value)
            fills = self._size_value(display, full_width)
        else:
            display = plain_label(format_value(value, field))
            display.setTextInteractionFlags(Qt.TextSelectableByMouse)
            level = self._age_level(field, value)
            if level in LEVEL_SUFFIX:
                # By object name, so the colour follows the theme.
                display.setObjectName(level_object_name('Level', level))
            elif level == 'lapsed':
                # Left in the ordinary colour: a date this far gone is a record
                # never rolled forward, not a payment due this week. The tooltip
                # says what the colour does not.
                display.setToolTip(
                    f'More than {lapsed_after_days()} days past and never rolled '
                    f'forward. Set Repeats to Monthly, or edit the date.'
                )
            fills = self._size_value(display, full_width)

        if field.get('copy'):
            # Closes over the decrypted value, so Copy works while a sensitive
            # field is still masked: the value reaches the clipboard — with the
            # clipboard-history opt-out and the auto-clear — without ever being
            # put on screen. Withholding the button would only push people to
            # Show, select and Ctrl+C, which gets none of that protection.
            copy = QPushButton('Copy')
            copy.setObjectName('Small')
            copy.setAccessibleName(f"Copy {field['label']}")
            copy.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            copy.clicked.connect(partial(
                self.copy_requested.emit, str(value or ''), field['label']))
            buttons.append(copy)

        if fills:
            # A full-row value puts its buttons up beside the field label.
            for button in buttons:
                label_row.addWidget(button)
            label_row.addStretch(1)
            row.addWidget(display, 1)
        else:
            # A short value keeps its buttons right after the text.
            label_row.addStretch(1)
            row.addWidget(display)
            for button in buttons:
                row.addWidget(button)
            row.addStretch(1)

        layout.addLayout(label_row)
        layout.addLayout(row)
        return container

    @classmethod
    def _size_value(cls, label: QLabel, full_width: bool) -> bool:
        """Sizes a value label and reports whether it takes the whole row."""
        if full_width:
            cls._wrap_label(label)
            return True
        return cls._fit_label(label)

    @staticmethod
    def _wrap_label(label: QLabel):
        """Lets a label wrap across whatever width it is given."""
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        label.setMaximumWidth(16777215)

    @classmethod
    def _fit_label(cls, label: QLabel) -> bool:
        """Sizes a value label to its text and reports whether it needs the whole
        row (True) or just fits (False).

        A word-wrapped QLabel reports a width well below its text's real width,
        so short values do not wrap at all and are pinned to their exact width;
        only genuinely long text wraps, and takes the full row."""
        natural = label.fontMetrics().horizontalAdvance(label.text())
        fills = natural > _WRAP_OVER
        if fills:
            cls._wrap_label(label)
        else:
            label.setWordWrap(False)
            label.setFixedWidth(natural + 4)
        return fills

    def _age_level(self, field: dict, value) -> str:
        """How urgent a dated field is, by the account's own category."""
        if not value:
            return ''
        category = account_category(self._account or {})
        if field['key'] == 'due_date':
            # Already resolved to the next occurrence by the caller.
            return due_date_level(value, None, category)
        if field['key'] == 'last_updated':
            return updated_age_level(value, category)
        return ''

    def _sensitive_value(self, field: dict, value) -> tuple[QLabel, QPushButton]:
        """The masked value label and its Show/Hide toggle.

        Always full width: measuring the fixed-length mask would pin the row to
        a sliver the revealed secret then has to wrap inside. It also keeps the
        on-screen width from hinting at the secret's length."""
        real = '' if value is None else str(value)
        display = plain_label(MASK if real else '—')
        display.setObjectName('MaskedValue')
        display.setTextInteractionFlags(Qt.TextSelectableByMouse)
        display.setAccessibleName(f"{field['label']}: hidden" if real
                                  else f"{field['label']}: empty")

        self._wrap_label(display)

        def on_change(shown: bool):
            display.setText(format_value(real, field) if shown
                            else (MASK if real else '—'))
            display.setObjectName('' if shown else 'MaskedValue')
            display.setAccessibleName(
                f"{field['label']}: {'shown' if shown else 'hidden'}" if real
                else f"{field['label']}: empty"
            )
            # Re-polish so the object-name-driven style change takes effect.
            display.style().unpolish(display)
            display.style().polish(display)

        # Parented to the label so the re-mask timer dies with the row.
        return display, reveal_toggle(field['label'], on_change, parent=display)

    def _link_value(self, value) -> QLabel:
        """A website value, opened in the desktop browser when clicked.

        Qt's rich-text link handling, with the text and URL escaped, so the
        label is reachable by keyboard; openExternalLinks stays off so nothing
        reaches the OS without passing normalize_url first."""
        url = normalize_url(value)
        if not url:
            # Refused by normalize_url (a script-ish scheme): plain text, no link.
            label = plain_label(str(value))
            label.setObjectName('LinkValue')
            label.setWordWrap(True)
            return label

        label = QLabel()
        label.setObjectName('LinkValue')
        label.setWordWrap(True)
        label.setText(f'<a href="{escape(url, quote=True)}">{escape(str(value))}</a>')
        label.setTextFormat(Qt.RichText)
        label.setOpenExternalLinks(False)
        label.setTextInteractionFlags(
            Qt.LinksAccessibleByMouse | Qt.LinksAccessibleByKeyboard)
        label.setCursor(Qt.PointingHandCursor)
        label.setToolTip(plain_text(f'Open {url}'))
        label.linkActivated.connect(lambda target: QDesktopServices.openUrl(QUrl(target)))
        return label

    # -- export ------------------------------------------------------------

    def _export_account(self, account: dict, config: dict):
        """Writes one account to a plaintext CSV, at a scope the user picks."""
        from ui.dialogs import ExportAccountDialog

        category = account.get('_category') or account.get('category')
        noun = value_noun(category)
        # Categories with no balance field have no history to offer.
        has_history = any(f['key'] == 'current_balance' for f in config['fields'])

        dialog = ExportAccountDialog(self, noun, has_history)
        if dialog.exec() != ExportAccountDialog.Accepted:
            return
        scope = dialog.scope()

        path, _ = QFileDialog.getSaveFileName(
            self, 'Export Account',
            export_save_path(account_export.default_name(account.get('name'), scope, noun)),
            'CSV (*.csv)',
        )
        if not path:
            return

        # Read after the file dialog closes, not before: the native picker is
        # outside Qt's modal stack, so the vault can lock while it is open, and
        # reading here puts that behind require_session. Re-read rather than
        # trusted, so a Quick Update since the account was opened is included.
        entries = []
        try:
            fresh = accounts_service.get_account(account['id'])
            if scope != account_export.SCOPE_ACCOUNT:
                entries = accounts_service.list_balance_history(account['id'])
        except VaultLockedError:
            self.locked.emit()
            return
        except Exception as exc:
            self.error.emit(str(exc))
            return

        result = account_export.render(fresh, entries, scope, noun)

        try:
            restricted = export_io.write_export(path, result['text'])
        except OSError as exc:
            self.error.emit(f'Export failed: {exc}')
            return

        self.notice.emit(_export_summary(result, scope, noun, restricted))

    # -- balance history ---------------------------------------------------

    def _build_history(self, account_id: int, category: str | None) -> QWidget:
        from ui.history_table import BalanceHistory

        history = BalanceHistory(
            account_id, self._theme, value_noun(category),
            higher_is_better=normalize_category(category or '') not in LOWER_IS_BETTER,
        )
        history.changed.connect(self.history_changed.emit)
        history.error.connect(self.error.emit)
        history.locked.connect(self.locked.emit)
        # After the connections: the first load can find the vault locked, and
        # that has to reach the window.
        history.reload()
        return history


class BulkView(QWidget):
    """The detail pane when more than one account is selected.

    Every action is one the whole selection can take without opening anything.
    Delete is left out: a mis-shift-click is exactly how to select more than you
    meant."""

    close_requested = Signal(bool)      # True to close, False to reopen
    category_requested = Signal(str)
    clear_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self._title = QLabel()
        self._title.setObjectName('DetailTitle')

        self._summary = QLabel()
        self._summary.setObjectName('Subtle')
        self._summary.setWordWrap(True)

        self._close = QPushButton('Close accounts')
        self._close.setObjectName('Small')
        self._close.clicked.connect(lambda: self.close_requested.emit(True))

        self._reopen = QPushButton('Reopen accounts')
        self._reopen.setObjectName('Small')
        self._reopen.clicked.connect(lambda: self.close_requested.emit(False))

        clear = QPushButton('Clear selection')
        clear.setObjectName('Ghost')
        clear.clicked.connect(self.clear_requested.emit)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self._close)
        actions.addWidget(self._reopen)
        actions.addWidget(clear)
        actions.addStretch(1)

        self._category = QComboBox()
        self._category.setAccessibleName('Move to category')

        move = QPushButton('Move')
        move.setObjectName('Small')
        move.clicked.connect(self._emit_category)

        category_row = QHBoxLayout()
        category_row.setSpacing(8)
        category_row.addWidget(self._category, 1)
        category_row.addWidget(move)
        category_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)
        layout.addWidget(self._title)
        layout.addWidget(self._summary)
        layout.addLayout(actions)
        layout.addWidget(field_label('Move to category'))
        layout.addLayout(category_row)
        layout.addStretch(1)

    def show_selection(self, accounts: list[dict], categories: list[str]):
        count = len(accounts)
        self._title.setText(f"{count} account{'s' if count != 1 else ''} selected")

        closed = sum(1 for a in accounts if a.get('closed'))
        open_count = count - closed
        parts = []
        if open_count:
            parts.append(f"{open_count} open")
        if closed:
            parts.append(f"{closed} closed")
        self._summary.setText(
            ' · '.join(parts) + '. Changes apply to all of them at once.'
        )

        # Only offer the direction that would actually change something.
        self._close.setEnabled(open_count > 0)
        self._reopen.setEnabled(closed > 0)

        current = self._category.currentData()
        self._category.blockSignals(True)
        self._category.clear()
        for name in categories:
            self._category.addItem(name, name)
        index = self._category.findData(current)
        self._category.setCurrentIndex(max(0, index))
        self._category.blockSignals(False)

        self.setAccessibleName(f"{count} accounts selected. {self._summary.text()}")

    def _emit_category(self):
        target = self._category.currentData()
        if target:
            self.category_requested.emit(target)


class PlaceholderView(QWidget):
    """The detail pane with no account on it, in four states: a new vault, an
    empty category, a search with no results, and a list with nothing chosen.
    Only the last is asking the user to select something."""

    add_requested = Signal()

    # Wide enough for a readable measure, narrow enough not to sprawl.
    _TEXT_WIDTH = 380

    def __init__(self, parent=None):
        super().__init__(parent)

        self._icon = ThemedIcon('lock', role='accent', size=48)

        # Titles carry search text and custom category names.
        self._title = plain_label()
        self._title.setObjectName('PlaceholderTitle')
        self._title.setAlignment(Qt.AlignCenter)
        self._title.setWordWrap(True)

        self._body = QLabel()
        self._body.setObjectName('Placeholder')
        self._body.setAlignment(Qt.AlignCenter)
        self._body.setWordWrap(True)
        self._body.setFixedWidth(self._TEXT_WIDTH)

        self._action = QPushButton()
        self._action.setObjectName('Primary')
        self._action.setCursor(Qt.PointingHandCursor)
        self._action.clicked.connect(self.add_requested.emit)

        self._hint = QLabel()
        self._hint.setObjectName('PlaceholderHint')
        self._hint.setAlignment(Qt.AlignCenter)
        self._hint.setWordWrap(True)
        self._hint.setFixedWidth(self._TEXT_WIDTH)

        rows = QVBoxLayout()
        rows.setSpacing(14)
        rows.addWidget(self._icon, 0, Qt.AlignHCenter)
        rows.addWidget(self._title)
        rows.addWidget(self._body, 0, Qt.AlignHCenter)
        rows.addWidget(self._action, 0, Qt.AlignHCenter)
        rows.addWidget(self._hint, 0, Qt.AlignHCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(1)
        layout.addLayout(rows)
        layout.addStretch(1)

        self.show_prompt()

    def _set(self, title: str = '', body: str = '', action: str = '',
             hint: str = '', icon: bool = False):
        for widget, text in ((self._title, title), (self._body, body),
                             (self._action, action), (self._hint, hint)):
            widget.setText(text)
            widget.setVisible(bool(text))
        self._icon.setVisible(icon)
        # One announcement for a screen reader, in reading order.
        self.setAccessibleName(' '.join(p for p in (title, body, hint) if p))

    def show_prompt(self):
        """A list with rows on it and none of them chosen."""
        self._set(body='Select an account to view details')

    def show_empty_vault(self):
        self._set(
            title='Your vault is empty',
            body='Everything you add is encrypted with your master password and '
                 'stays on this computer. Nothing is sent anywhere.',
            action='Add your first account',
            hint='Already have a backup? A backup is a vault — open it from '
                 'Manage → Open Vault.',
            icon=True,
        )

    def show_empty_list(self, message: str, can_add: bool = True):
        """A category with nothing in it, in the same words the list uses."""
        self._set(title=message, action='Add one' if can_add else '')

    def show_no_results(self, query: str):
        self._set(
            title=f'No accounts match “{query}”',
            body='Search covers names, usernames and websites. It never looks '
                 'at passwords, card numbers, notes or other secrets.',
        )
