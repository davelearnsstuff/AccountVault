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

from categories import (
    DUE_CYCLE_KEY, LAPSED_AFTER_DAYS, account_category, due_date_level,
    format_value, get_config, is_favorite, next_due_date, updated_age_level,
    value_noun,
)
from encryption import VaultLockedError
from urls import normalize_url
from services import account_export
from services import accounts as accounts_service
from services import export_io
from ui.theme import LEVEL_SUFFIX, level_object_name
from ui.widgets import (
    IconButton, ThemedIcon, export_save_path, field_label, reveal_toggle,
)

MASK = '••••••••'

# Field grid width; long-text fields span the whole row instead.
_COLUMNS = 2

# A value wider than this wraps and claims the whole row; anything shorter is
# pinned to its text so the Copy / Show button stays beside it. This is only
# safe because DETAIL_MIN_WIDTH below forces the panel to always be wide
# enough for two columns of this size — if that floor ever changes, this
# threshold has to move with it.
_WRAP_OVER = 240

# Margins the grid needs around and between its two columns (see show_account's
# QVBoxLayout margins and _build_fields' horizontalSpacing), plus slack for the
# enclosing QScrollArea's frame/scrollbar.
_GRID_MARGIN = 40
_GRID_GAP = 28
_SCROLL_SLACK = 24

# The narrowest the detail panel is ever allowed to get. main_window.py sets
# this as the panel's minimum width so the splitter — whether auto-fit or
# hand-dragged — can never shrink a pinned field's column below _WRAP_OVER.
DETAIL_MIN_WIDTH = _WRAP_OVER * _COLUMNS + _GRID_GAP + _GRID_MARGIN + _SCROLL_SLACK


def _export_summary(result: dict, scope: str, noun: str, restricted: bool = True) -> str:
    """What an export actually wrote, for the toast.

    Counts rather than a bare 'Exported.', because the three scopes produce
    very different files and the one that was written is worth confirming — an
    account details file and a history file land under similar names."""
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
    # Worded as written-but-broken rather than left out, because that is what
    # happened: unlike a history entry, a field that will not decrypt is still
    # in the file, as its placeholder.
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
    copy_requested = Signal(str, str)      # value, field label
    history_changed = Signal()
    error = Signal(str)
    notice = Signal(str)
    # Forwarded from the history table and raised by this pane's own export.
    # A locked vault is the window's to answer — see BalanceHistory.locked.
    locked = Signal()

    def __init__(self, theme: str, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._account = None
        self._content = None

        # Rendering swaps a single content widget rather than rebuilding this
        # layout in place, so a redraw can never race a pending deletion.
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

    def set_theme(self, theme: str):
        """Re-renders in the new theme, unlike DashboardView.set_theme, which
        only records it. The difference is what each redraw costs: this one
        redraws the account already in hand, while the dashboard's would have to
        read and decrypt every balance entry — and it is called at startup, with
        the vault still locked."""
        self._theme = theme
        if self._account:
            self.show_account(self._account)

    def clear(self):
        self._account = None
        self._drop_content()

    def _drop_content(self):
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
            layout.addWidget(self._build_history(account['id'], value_noun(category)))

        layout.addStretch(1)
        self._layout.addWidget(self._content)

    # -- header ------------------------------------------------------------

    def _build_header(self, account: dict, config: dict) -> QVBoxLayout:
        title = QLabel(account.get(config['name_field']) or f"Untitled {config['item_label']}")
        title.setObjectName('DetailTitle')
        title.setWordWrap(True)

        # The pin sits on the title line rather than among the actions below,
        # because it is a property of the account rather than something done to
        # it — and because the actions row is where the buttons that change
        # stored fields live, which this does not.
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

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(title, 1)
        title_row.addWidget(star, 0, Qt.AlignTop)

        # The actions sit on their own row under the name, left-aligned with the
        # fields below them, so the title has the full width to wrap into and
        # they never drift out to the far edge of a wide panel.
        actions = QHBoxLayout()
        actions.setSpacing(8)

        keys = {f['key'] for f in config['fields']}
        has_balance, has_due = 'current_balance' in keys, 'due_date' in keys
        if has_balance or has_due:
            # Named after what this category actually tracks, so Income offers
            # its annual pay rather than a 'balance' it has no concept of.
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

        # Always on screen, disabled until the account is closed. Hiding it kept
        # the rule invisible: nothing said why an account could not be deleted,
        # or what to do about it. A greyed button with a reason teaches it.
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
        return header

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
            # A recurring due date is stored as an anchor — what repeats is its
            # day of the month — so the pane shows the occurrence still ahead
            # rather than whichever month the user last typed in. A one-off is
            # its own next occurrence, so this leaves it exactly as it was.
            if field['key'] == 'due_date':
                value = next_due_date(value, cycle) or value
            # Blank optional fields would only clutter the view; a blank required
            # name still shows so the gap is visible.
            if not field.get('required') and (value is None or value == ''):
                continue

            # Long text gets the full width, on its own row. The field is told
            # so, because how its value is sized has to match the row it lands
            # in: a whole row is only worth taking if the value spreads across
            # it rather than being pinned to the text it happens to hold.
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
        """Website sits immediately after the name so the link is always in the
        same spot, whatever order the category declares its fields in."""
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
        """Builds one field's label / value block. full_width says the caller has
        given this field the whole grid row, so its value wraps freely instead of
        being pinned to the width of the text it happens to show."""
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
            display = QLabel(format_value(value, field))
            display.setTextInteractionFlags(Qt.TextSelectableByMouse)
            level = self._age_level(field, value)
            if level in LEVEL_SUFFIX:
                # Object name, not an inline colour, so the value repaints when
                # the theme changes — see the note above LEVEL_COLORS' removal
                # in ui/theme.py. Which levels get one is LEVEL_SUFFIX's to say,
                # so this pane and the account list cannot drift apart on it.
                display.setObjectName(level_object_name('Level', level))
            elif level == 'lapsed':
                # Deliberately left in the ordinary text colour. A date this far
                # past is a record that was never rolled forward, not a payment
                # anyone can make today, and dressing it in the same red as a
                # bill due on Tuesday is what taught people to stop reading the
                # colour. The tooltip carries what the colour no longer says.
                display.setToolTip(
                    f'More than {LAPSED_AFTER_DAYS} days past and never rolled '
                    f'forward. Set Repeats to Monthly, or edit the date.'
                )
            fills = self._size_value(display, full_width)

        if field.get('copy'):
            # Closes over the decrypted value, not over what the label is
            # currently showing, so Copy works while a sensitive field is still
            # masked. That is the point of offering it on card, account and
            # routing numbers: the value reaches the clipboard without ever
            # being put on screen.
            #
            # It is also the private path — along with the Ctrl+B / Ctrl+Shift+C
            # shortcuts, which route through the same copy_to_clipboard and get
            # the same protection. Those fields had no Copy button on the
            # reasoning that a number nobody can copy is a number nobody can
            # leak — but the values are exactly the ones
            # people retype into a form, and the labels are selectable, so the
            # real workflow was Show, drag-select, Ctrl+C. That route never
            # touches _secret_mime, so the value landed in the Windows
            # clipboard-history pane, synced to the cloud clipboard where that
            # is on, and was never cleared by the timer. Withholding the button
            # did not prevent the copy; it just made every copy the unprotected
            # kind.
            copy = QPushButton('Copy')
            copy.setObjectName('Small')
            copy.setAccessibleName(f"Copy {field['label']}")
            copy.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            copy.clicked.connect(partial(
                self.copy_requested.emit, str(value or ''), field['label']))
            buttons.append(copy)

        if fills:
            # Long text owns the full row, so its buttons go up beside the field
            # label — trailing them would strand them at the far right edge.
            for button in buttons:
                label_row.addWidget(button)
            label_row.addStretch(1)
            row.addWidget(display, 1)
        else:
            # Short value: buttons sit right after the text, and the stretch
            # holds the spare width so nothing drifts to the column edge.
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
        """Sizes a value label and reports whether it takes the whole row. A
        field the grid has already given the full row always does, whatever its
        text currently measures; anything else is decided by the text."""
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
        row (True) or just fits (False). Only for values shown in full — a masked
        one has no text worth measuring until it's revealed.

        A word-wrapped QLabel reports a heuristic width well below its text's
        real single-line width, so leaving wrapping on for everything makes
        short values wrap early and long ones claim too little. Short values
        therefore don't wrap at all and are pinned to their exact text width,
        which is what lets a following button sit right beside them; only
        genuinely long text wraps, and that takes the full row."""
        natural = label.fontMetrics().horizontalAdvance(label.text())
        fills = natural > _WRAP_OVER
        if fills:
            cls._wrap_label(label)
        else:
            label.setWordWrap(False)
            label.setFixedWidth(natural + 4)
        return fills

    def _age_level(self, field: dict, value) -> str:
        """How urgent a dated field is. An instance method, not a static one:
        staleness now reads its thresholds from the category, so grading a field
        needs to know which account it belongs to."""
        if not value:
            return ''
        if field['key'] == 'due_date':
            # Already resolved to the next occurrence by the caller, so the
            # cycle is spent and what is left is a plain date to grade.
            return due_date_level(value, None)
        if field['key'] == 'last_updated':
            return updated_age_level(value, account_category(self._account or {}))
        return ''

    def _sensitive_value(self, field: dict, value) -> tuple[QLabel, QPushButton]:
        """The masked value label and its Show/Hide toggle. The value always
        takes the full width it is given, unlike the fields sized from their
        text: the mask is short and fixed-length whatever it hides, so measuring
        it pins the row to a sliver the revealed secret then has to wrap inside
        — and the reveal is far too late to re-lay the row out around it.

        Wrapping from the start is also what keeps a masked field's on-screen
        width independent of the real secret's length, so it can't leak a length
        hint before the user ever clicks Show."""
        real = '' if value is None else str(value)
        display = QLabel(MASK if real else '—')
        display.setObjectName('MaskedValue')
        display.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # The mask reads aloud as a run of bullet characters, which says nothing
        # about what the field is or that it is deliberately concealed.
        display.setAccessibleName(f"{field['label']}: hidden" if real
                                  else f"{field['label']}: empty")

        self._wrap_label(display)

        def on_change(shown: bool):
            display.setText(format_value(real, field) if shown
                            else (MASK if real else '—'))
            display.setObjectName('' if shown else 'MaskedValue')
            # The value says whether it is currently readable; reveal_toggle
            # moves the button's own name to match.
            display.setAccessibleName(
                f"{field['label']}: {'shown' if shown else 'hidden'}" if real
                else f"{field['label']}: empty"
            )
            # Re-polish so the object-name-driven style change takes effect.
            display.style().unpolish(display)
            display.style().polish(display)

        # The button, its labels and the auto-re-mask timer are shared with the
        # account form — see widgets.reveal_toggle. Parented to the label so the
        # timer dies with the row rather than firing into a deleted widget.
        return display, reveal_toggle(field['label'], on_change, parent=display)

    def _link_value(self, value) -> QLabel:
        """A website value, opened in the desktop browser when clicked.

        Qt's own rich-text link handling rather than an assignment over the
        instance's mousePressEvent. That assignment shadowed the bound method on
        one object, which bypasses event delivery for every other button and
        modifier, kept the closure alive on the widget, and left the label
        unreachable by keyboard. linkActivated gives the same click, plus focus
        and Enter, and openExternalLinks stays off so nothing is dispatched to
        the OS without passing through normalize_url first."""
        url = normalize_url(value)
        label = QLabel()
        label.setObjectName('LinkValue')
        label.setWordWrap(True)
        if not url:
            # Refused by normalize_url (a script-ish scheme) — shown as the
            # plain text it is, with no way to follow it.
            label.setText(str(value))
            return label

        label.setText(f'<a href="{escape(url, quote=True)}">{escape(str(value))}</a>')
        label.setTextFormat(Qt.RichText)
        label.setOpenExternalLinks(False)
        label.setTextInteractionFlags(
            Qt.LinksAccessibleByMouse | Qt.LinksAccessibleByKeyboard)
        label.setCursor(Qt.PointingHandCursor)
        label.setToolTip(f'Open {url}')
        label.linkActivated.connect(lambda target: QDesktopServices.openUrl(QUrl(target)))
        return label

    # -- export ------------------------------------------------------------

    def _export_account(self, account: dict, config: dict):
        """Writes one account to a plaintext CSV, at a scope the user picks.

        Lives here rather than beside the history table because two of the three
        scopes cover the whole account. The dialog owns the choice and the
        warning that goes with it; this only carries out what it returns."""
        from ui.dialogs import ExportAccountDialog

        category = account.get('_category') or account.get('category')
        noun = value_noun(category)
        # Categories with no balance field have no history section on screen and
        # none to offer here either — see show_account, which builds the two on
        # the same condition.
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

        # Decrypted after the file dialog closes, not before. A native file
        # picker is outside Qt's modal stack, so the idle lock cannot dismiss
        # one; rendering first left this account's password decrypted in a local
        # for as long as the dialog was open and then wrote it to disk even if
        # the vault had locked meanwhile. Reading here puts both calls behind
        # require_session, so a lock during the dialog raises instead.
        #
        # Re-read rather than trusting the dict on screen either way: it was
        # loaded when the account was opened, and a Quick Update since then
        # would otherwise export a figure the detail view is no longer showing.
        entries = []
        try:
            fresh = accounts_service.get_account(account['id'])
            if scope != account_export.SCOPE_ACCOUNT:
                entries = accounts_service.list_balance_history(account['id'])
        except VaultLockedError:
            # The case this ordering exists to catch: the idle lock can fire
            # while the native file picker is up, and it must lock the window
            # rather than report itself as a failed export in a toast.
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

    def _build_history(self, account_id: int, noun: str) -> QWidget:
        from ui.history_table import BalanceHistory

        history = BalanceHistory(account_id, self._theme, noun)
        history.changed.connect(self.history_changed.emit)
        history.error.connect(self.error.emit)
        history.locked.connect(self.locked.emit)
        # After the connections, never in its constructor: the first load reads
        # the vault, and a lock found there has to reach the window rather than
        # be emitted into a signal nothing is listening to yet.
        history.reload()
        return history


class BulkView(QWidget):
    """The detail pane when more than one account is selected.

    Replaces the single-account view rather than sitting beside it: with several
    rows highlighted there is no one record the pane could be showing, and
    picking the first would make the actions here look like they applied to it
    alone.

    Every action is one the whole selection can take without opening anything:
    the full form edits one account at a time by nature, and Delete is left out
    entirely — it is irreversible, and a mis-shift-click is exactly the way to
    select more than you meant."""

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
    """The detail pane with no account on it.

    Four states rather than one, because "Select an account to view details" is
    wrong advice in three of them: a new vault has nothing to select, an empty
    category has nothing to select, and a search that found nothing has nothing
    to select. Only the fourth — a populated list with no row picked — is
    actually asking the user to choose."""

    add_requested = Signal()

    # Wide enough for a readable measure, narrow enough not to sprawl across a
    # maximised window.
    _TEXT_WIDTH = 380

    def __init__(self, parent=None):
        super().__init__(parent)

        self._icon = ThemedIcon('lock', role='accent', size=48)

        self._title = QLabel()
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
        # The whole pane is one announcement for a screen reader, so it carries
        # the message rather than leaving four separate labels to be stitched
        # together in whatever order they happen to be read.
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
        """A category with nothing in it. The message is the same wording the
        list itself shows, so the two panes cannot drift apart."""
        self._set(title=message, action='Add one' if can_add else '')

    def show_no_results(self, query: str):
        self._set(
            title=f'No accounts match “{query}”',
            body='Search covers names, usernames, websites and notes. It never '
                 'looks at passwords, card numbers or other secrets.',
        )

