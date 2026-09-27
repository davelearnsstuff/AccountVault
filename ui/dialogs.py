# -----------------------------------------------------------------------------
# ui/dialogs.py
# Modal dialogs: settings, about, CSV warning, change master password, the quick
# balance/due-date update, the whole-vault account import, and the per-account
# history import and export.
#
# The backup export and import dialogs are gone with the format they collected a
# password for. A backup is a copy of the vault now, so there is nothing to ask:
# Save a Copy is a file dialog, and opening one is opening a vault.
# -----------------------------------------------------------------------------

from pathlib import Path

from PySide6.QtCore import QDate, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QRadioButton, QSizePolicy, QSpacerItem, QTableWidget,
    QTableWidgetItem, QTextBrowser,
    QVBoxLayout, QWidget,
)

import auth
import preferences
from categories import (
    CATEGORY_CONFIG, DEFAULT_VALUE_NOUN, DUE_CYCLE_KEY, advance_cycle, cycle_noun,
    format_date, format_money,
    has_field, next_due_date, parse_money, trim_number, value_noun,
)
from services import account_export, account_import, history_io
from urls import normalize_url
from ui.theme import THEMES
from ui.widgets import (
    NO_DATE, ThemedIcon, date_edit, field_label, password_field,
    password_requirements, to_qdate,
)


class _BaseDialog(QDialog):
    """Shared frame: a title, a body layout, an error line, and a button row."""

    def __init__(self, parent, title: str, width: int = 460):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(width)

        heading = QLabel(title)
        heading.setObjectName('DetailTitle')

        self.error = QLabel()
        self.error.setObjectName('ErrorLabel')
        self.error.setWordWrap(True)
        self.error.hide()

        self.body = QVBoxLayout()
        self.body.setSpacing(10)

        self.buttons = QHBoxLayout()
        self.buttons.addStretch(1)

        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(22, 20, 22, 20)
        self._outer.setSpacing(12)
        self._outer.addWidget(heading)
        self._outer.addLayout(self.body)
        self._outer.addWidget(self.error)
        # Filler that keeps short forms pinned to the top. Dialogs whose body
        # should own the leftover height drop it via expand_body().
        self._filler = QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding)
        self._outer.addItem(self._filler)
        self._outer.addLayout(self.buttons)

    def expand_body(self):
        """Gives the body the spare height instead of the filler stretch, for
        bodies that hold a scrollable view rather than a short form."""
        self._outer.removeItem(self._filler)
        self._outer.setStretchFactor(self.body, 1)

    def add_field(self, label: str, widget: QWidget):
        self.body.addWidget(field_label(label))
        self.body.addWidget(widget)

    def add_button(self, text: str, slot, object_name: str = '') -> QPushButton:
        button = QPushButton(text)
        if object_name:
            button.setObjectName(object_name)
        button.clicked.connect(slot)
        self.buttons.addWidget(button)
        return button

    def fail(self, message: str):
        self.error.setText(message)
        self.error.show()

    def clear_error(self):
        self.error.clear()
        self.error.hide()


# -----------------------------------------------------------------------------
# Settings
#
# There is no Manage dialog. The vault's actions are a menu on the nav bar's
# gear button (see MainWindow._build_manage_menu), which is one click rather
# than two modals and gives the keyboard shortcuts somewhere to advertise
# themselves.
# -----------------------------------------------------------------------------

class ThemeSwatch(QPushButton):
    """Checkable theme preview: a colour block with an accent stripe and a name."""

    def __init__(self, theme_key: str, parent=None):
        super().__init__(parent)
        self.theme_key = theme_key
        theme = THEMES[theme_key]

        self.setObjectName('Swatch')
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(92, 74)

        # The one place inline colour is the only option: these two paint a
        # preview of some *other* theme — the one this swatch offers — so they
        # must not follow the active palette the way everything else does.
        preview = QFrame()
        preview.setFixedHeight(30)
        preview.setStyleSheet(
            f"background-color: {theme['bg_primary']};"
            f"border: 1px solid {theme['border']}; border-radius: 4px;"
        )
        stripe = QFrame(preview)
        stripe.setStyleSheet(f"background-color: {theme['accent']}; border-radius: 2px;")
        stripe.setGeometry(8, 11, 60, 6)

        # #SwatchName rather than #Subtle plus an inline size: the inline one
        # was the last hardcoded pixel font size in the app, and it left this
        # caption at 10px while every other size followed the font setting.
        name = QLabel(theme['label'])
        name.setAlignment(Qt.AlignCenter)
        name.setObjectName('SwatchName')
        name.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 4)
        layout.setSpacing(4)
        layout.addWidget(preview)
        layout.addWidget(name)


class SettingsDialog(_BaseDialog):
    """Theme, font size, list density, and the three timeouts. The three
    appearance settings preview live so the change is visible before saving;
    cancelling reverts them."""

    preview_requested = Signal(str, str, str)   # theme, font_size, list_density

    def __init__(self, parent, settings: dict):
        super().__init__(parent, 'Settings', width=520)
        # Exactly the trio preview_requested carries, so reject() can undo a
        # live preview by splatting it.
        self._original = (settings['theme'], settings['font_size'],
                          settings.get('list_density', 'compact'))

        # --- Theme swatches ---
        self.body.addWidget(field_label('Theme'))
        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(8)
        self._theme_group = QButtonGroup(self)
        self._theme_group.setExclusive(True)
        for key in preferences.VALID_THEMES:
            swatch = ThemeSwatch(key)
            swatch.setChecked(key == settings['theme'])
            swatch.clicked.connect(lambda _=False, k=key: self._preview(theme=k))
            self._theme_group.addButton(swatch)
            swatch_row.addWidget(swatch)
        swatch_row.addStretch(1)
        self.body.addLayout(swatch_row)

        # --- Font size ---
        self.body.addWidget(field_label('Font Size'))
        font_row = QHBoxLayout()
        font_row.setSpacing(8)
        self._font_group = QButtonGroup(self)
        self._font_group.setExclusive(True)
        for key, label in (('small', 'Small'), ('default', 'Default'),
                           ('large', 'Large'), ('xl', 'XL')):
            button = QPushButton(label)
            button.setObjectName('Toggle')
            button.setCheckable(True)
            button.setChecked(key == settings['font_size'])
            button.clicked.connect(lambda _=False, k=key: self._preview(font_size=k))
            button.setProperty('sizeKey', key)
            self._font_group.addButton(button)
            font_row.addWidget(button)
        font_row.addStretch(1)
        self.body.addLayout(font_row)

        # --- List density ---
        self.body.addWidget(field_label('List Density'))
        density_row = QHBoxLayout()
        density_row.setSpacing(8)
        self._density_group = QButtonGroup(self)
        self._density_group.setExclusive(True)
        for key, label, hint in (
            ('compact', 'Compact',
             'Balance, updated and due on one line — more accounts on screen'),
            ('comfortable', 'Comfortable', 'Each of them on its own line'),
        ):
            button = QPushButton(label)
            button.setObjectName('Toggle')
            button.setCheckable(True)
            button.setChecked(key == settings.get('list_density'))
            button.setToolTip(hint)
            button.setProperty('densityKey', key)
            button.clicked.connect(lambda _=False, k=key: self._preview(list_density=k))
            self._density_group.addButton(button)
            density_row.addWidget(button)
        density_row.addStretch(1)
        self.body.addLayout(density_row)

        # --- Timeouts ---
        self._lock = self._combo(
            [(v, f"{v} minutes" + (' (default)' if v == 15 else '')) for v in preferences.VALID_LOCK_TIMEOUTS],
            settings['lock_timeout_minutes'],
        )
        self.add_field('Auto-Lock Timeout', self._lock)

        self._clipboard = self._combo(
            [(v, f"{v} seconds" + (' (default)' if v == 30 else '')) for v in preferences.VALID_CLIPBOARD_CLEARS],
            settings['clipboard_clear_seconds'],
        )
        self.add_field('Clipboard Auto-Clear', self._clipboard)

        shutdown_labels = {30: '30 minutes', 60: '1 hour (default)', 120: '2 hours',
                           240: '4 hours', 0: 'Never'}
        self._shutdown = self._combo(
            [(v, shutdown_labels[v]) for v in preferences.VALID_SHUTDOWN_TIMEOUTS],
            settings['shutdown_after_lock_minutes'],
        )
        self.add_field('Auto-Shutdown (after lock)', self._shutdown)

        self._build_backup_section(settings)

        self.add_button('Cancel', self.reject)
        self.add_button('Save', self.accept, 'Primary')

    # -----------------------------------------------------------------
    # Backups
    # -----------------------------------------------------------------

    def _build_backup_section(self, settings: dict):
        """Automatic copies of the vault: whether, how often, where, how long.

        A backup here is a copy of the vault file, so it opens with the master
        password and needs no second one. What it does not do is survive a
        password change, which is why that flow offers a fresh copy rather than
        this screen carrying a warning nobody reads twice."""
        self.body.addSpacing(6)
        self.body.addWidget(field_label('Automatic Backups'))

        self._auto_backup = QCheckBox('Back up this vault automatically')
        self._auto_backup.setChecked(bool(settings.get('auto_backup_enabled')))
        self._auto_backup.toggled.connect(self._sync_backup_enabled)
        self.body.addWidget(self._auto_backup)

        self._frequency = self._combo(
            [('daily', 'Every day'), ('weekly', 'Every week'),
             ('monthly', 'Every month')],
            settings.get('backup_frequency', 'weekly'),
        )
        self.add_field('Frequency', self._frequency)

        retention_labels = {1: '1 month', 3: '3 months', 6: '6 months',
                            12: '12 months (default)', 24: '2 years',
                            0: 'Keep forever'}
        self._retention = self._combo(
            [(v, retention_labels[v]) for v in preferences.VALID_BACKUP_RETENTION_MONTHS],
            settings.get('backup_retention_months', 12),
        )
        self.add_field('Keep Backups For', self._retention)

        self._location = settings.get('backup_location',
                                      preferences.BACKUP_LOCATION_DEFAULT)
        self._location_label = QLabel()
        self._location_label.setObjectName('FieldHint')
        self._location_label.setWordWrap(True)

        browse = QPushButton('Choose Folder…')
        browse.clicked.connect(self._browse_location)
        self._reset_location = QPushButton('Use Default')
        self._reset_location.clicked.connect(self._use_default_location)

        location_row = QHBoxLayout()
        location_row.setSpacing(8)
        location_row.addWidget(browse)
        location_row.addWidget(self._reset_location)
        location_row.addStretch(1)

        self.add_field('Backup Folder', self._location_label)
        self.body.addLayout(location_row)

        self._backup_status = QLabel()
        self._backup_status.setObjectName('FieldHint')
        self._backup_status.setWordWrap(True)
        self.body.addWidget(self._backup_status)

        self._sync_location_label()
        self._sync_backup_enabled(self._auto_backup.isChecked())
        self._show_last_backup()

    def _sync_backup_enabled(self, enabled: bool):
        for widget in (self._frequency, self._retention):
            widget.setEnabled(enabled)

    def _sync_location_label(self):
        if self._location == preferences.BACKUP_LOCATION_DEFAULT:
            self._location_label.setText(
                'A "backups" folder beside the vault (follows the vault if you move it).')
        else:
            self._location_label.setText(self._location)
        self._reset_location.setEnabled(
            self._location != preferences.BACKUP_LOCATION_DEFAULT)

    def _browse_location(self):
        folder = QFileDialog.getExistingDirectory(self, 'Backup Folder', self._location
                                                  if self._location != preferences.BACKUP_LOCATION_DEFAULT
                                                  else '')
        if folder:
            self._location = folder
            self._sync_location_label()

    def _use_default_location(self):
        self._location = preferences.BACKUP_LOCATION_DEFAULT
        self._sync_location_label()

    def _show_last_backup(self):
        """The one line that says whether any of this is actually working.

        A failure is reported as a failure and kept on screen. Backups that have
        been silently failing are worse than backups nobody turned on, because
        only one of those two is believed in."""
        state = preferences.get_backup_state()
        outcome = state.get('outcome') or {}
        if not outcome:
            self._backup_status.setText('No backup has been taken yet.')
            return
        when = outcome.get('date', '')
        if outcome.get('ok'):
            self._backup_status.setText(f"Last backup: {when} — {outcome.get('name', 'saved')}")
        else:
            self._backup_status.setText(
                f"Last backup FAILED on {when}: {outcome.get('message', 'unknown error')}")

    @staticmethod
    def _combo(options, current) -> QComboBox:
        combo = QComboBox()
        for value, label in options:
            combo.addItem(label, value)
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        return combo

    def _preview(self, theme: str | None = None, font_size: str | None = None,
                 list_density: str | None = None):
        # Every preview carries all three: the toggles are already checked by
        # the time this runs, so the two the user did not touch read back as
        # what is on screen and only the one they clicked actually moves.
        self.preview_requested.emit(theme or self.theme(),
                                    font_size or self.font_size(),
                                    list_density or self.list_density())

    def theme(self) -> str:
        checked = self._theme_group.checkedButton()
        return checked.theme_key if checked else self._original[0]

    def font_size(self) -> str:
        checked = self._font_group.checkedButton()
        return checked.property('sizeKey') if checked else self._original[1]

    def list_density(self) -> str:
        checked = self._density_group.checkedButton()
        return checked.property('densityKey') if checked else self._original[2]

    def values(self) -> dict:
        return {
            'lock_timeout_minutes':        self._lock.currentData(),
            'clipboard_clear_seconds':     self._clipboard.currentData(),
            'shutdown_after_lock_minutes': self._shutdown.currentData(),
            'theme':                       self.theme(),
            'font_size':                   self.font_size(),
            'list_density':                self.list_density(),
            'auto_backup_enabled':         int(self._auto_backup.isChecked()),
            'backup_frequency':            self._frequency.currentData(),
            'backup_retention_months':     self._retention.currentData(),
            'backup_location':             self._location,
        }

    def reject(self):
        # Undo the live preview before closing.
        self.preview_requested.emit(*self._original)
        super().reject()


# -----------------------------------------------------------------------------
# About
# -----------------------------------------------------------------------------

class ReadmeDialog(_BaseDialog):
    """Renders the bundled README.md. Read-only, and it opens no links itself —
    anything external goes to the desktop browser, not this window, and only
    after passing the same check a stored website does."""

    PATH = Path(__file__).resolve().parent.parent / 'README.md'

    def __init__(self, parent):
        super().__init__(parent, 'Readme', width=760)
        self.setMinimumHeight(560)

        view = QTextBrowser()
        # Not setOpenExternalLinks: that hands any anchor in the file straight
        # to QDesktopServices, which made this the one link dispatch in the app
        # that urls.normalize_url did not stand in front of. The file is
        # bundled, so it is trusted about as far as the directory it sits in —
        # and that directory, unlike the vault, is not restricted to the owner.
        view.setOpenLinks(False)
        view.anchorClicked.connect(self._follow)
        view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        try:
            view.setMarkdown(self.PATH.read_text(encoding='utf-8'))
        except OSError as exc:
            view.setPlainText(f"Could not read {self.PATH.name}: {exc}")
        self.body.addWidget(view, 1)
        self.expand_body()

        self.add_button('Close', self.accept)
        self.resize(self._preferred_size(parent))

    @staticmethod
    def _follow(url: QUrl):
        """Opens a link from the README, through the check every stored website
        already goes through.

        normalize_url returns '' for the schemes that execute rather than
        navigate, which arrives here as a click that does nothing."""
        target = normalize_url(url.toString())
        if target:
            QDesktopServices.openUrl(QUrl(target))

    @staticmethod
    def _preferred_size(parent) -> QSize:
        """Opens large — this is a document, not a form — but never larger than
        the window it came from, or the screen if there is no parent."""
        available = (parent.frameGeometry() if parent is not None
                     else QGuiApplication.primaryScreen().availableGeometry())
        return QSize(min(1000, max(760, int(available.width() * 0.8))),
                     min(880, max(560, int(available.height() * 0.9))))


class AboutDialog(_BaseDialog):
    ROWS = [
        ('Version',           '2.0.0'),
        ('Interface',         'PySide6 (Qt) desktop application'),
        ('Key Derivation',    'Argon2id'),
        ('Field Encryption',  'AES-256-GCM'),
        ('Storage',           'Local SQLite database'),
        ('Network',           'No network listener, no internet required'),
        ('Data leaves device', 'Never'),
    ]

    def __init__(self, parent):
        super().__init__(parent, 'About AccountVault', width=440)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)
        for row, (label, value) in enumerate(self.ROWS):
            grid.addWidget(field_label(label), row, 0, Qt.AlignLeft)
            grid.addWidget(QLabel(value), row, 1, Qt.AlignRight)
        grid.setColumnStretch(1, 1)
        self.body.addLayout(grid)

        self.add_button('Close', self.accept)


class CsvWarningDialog(_BaseDialog):
    """Spells out that a CSV export is unencrypted before letting it proceed."""

    # 'alert' carries the theme's danger colour, 'info' the muted one, so
    # severity survives a theme change instead of riding on an emoji's own
    # palette — which no theme could touch and which differed per platform.
    WARNINGS = [
        ('alert', 'This file is not encrypted. Anyone with access to it can read all '
                  'your passwords and account details.'),
        ('info', 'Balance history is not included. Only the current state of each '
                 'account is exported.'),
        ('info', 'Values a spreadsheet would run as a formula — those starting '
                 '= + - @ or with whitespace — are written with a leading '
                 'apostrophe, so they are shown instead of evaluated.'),
        ('info', 'Use Export Backup for secure encrypted backups. CSV is for '
                 'spreadsheet import or migration only.'),
    ]

    def __init__(self, parent):
        super().__init__(parent, 'Export CSV', width=500)

        box = QFrame()
        box.setObjectName('WarningBox')
        inner = QVBoxLayout(box)
        inner.setContentsMargins(14, 12, 14, 12)
        inner.setSpacing(10)

        for icon, text in self.WARNINGS:
            row = QHBoxLayout()
            row.setSpacing(10)
            icon_label = ThemedIcon(
                icon, role='danger' if icon == 'alert' else 'subtle', size=17
            )
            message = QLabel(text)
            message.setWordWrap(True)
            row.addWidget(icon_label)
            row.addWidget(message, 1)
            inner.addLayout(row)

        self.body.addWidget(box)

        self.add_button('Cancel', self.reject)
        self.add_button('Export Anyway', self.accept, 'Danger')


# -----------------------------------------------------------------------------
# Change master password
# -----------------------------------------------------------------------------

class ChangePasswordDialog(_BaseDialog):
    """Collects the current and new master password, validating length locally.
    Re-keying every encrypted field is the caller's job."""

    def __init__(self, parent):
        super().__init__(parent, 'Change Master Password', width=480)

        self._current = password_field()
        self._new = password_field()
        self._confirm = password_field()

        self.add_field('Current Password', self._current)
        self.add_field('New Password', self._new)

        for requirement in password_requirements(self._new):
            self.body.addWidget(requirement)

        self.add_field('Confirm New Password', self._confirm)

        self._current.returnPressed.connect(self._new.setFocus)
        self._new.returnPressed.connect(self._confirm.setFocus)
        self._confirm.returnPressed.connect(self._submit)

        self.add_button('Cancel', self.reject)
        self.add_button('Change Password', self._submit, 'Primary')

    def _submit(self):
        self.clear_error()
        if not self._current.text().strip():
            self.fail('Current password is required.')
            return
        errors = auth.validate_password_strength(self._new.text())
        if errors:
            self.fail('New ' + ', '.join(errors) + '.')
            return
        if self._new.text().strip() != self._confirm.text().strip():
            self.fail('Passwords do not match.')
            return
        self.accept()

    def current_password(self) -> str:
        return self._current.text()

    def new_password(self) -> str:
        return self._new.text().strip()


# -----------------------------------------------------------------------------
# Quick update
# -----------------------------------------------------------------------------

class QuickUpdateDialog(_BaseDialog):
    """Fast path for the two fields that change most often: the current balance
    and the due date. Shows only the fields the account's category defines.

    Every category that tracks a balance gets the same 'as of' date, so a
    statement can be logged on the day it covers rather than the day it is
    typed in. It defaults to today, which is the common case, and cannot run
    ahead of it: a balance from the future is always a typo, and one filed
    years out stretches the trend chart's axis to nothing."""

    def __init__(self, parent, account: dict):
        super().__init__(parent, 'Quick Update', width=380)
        # Captured once: every later comparison against 'today' has to use the
        # same day the field was filled in with, or a dialog left open across
        # midnight would read as backdated and drop the amount just entered.
        self._opened_on = QDate.currentDate()
        category = account.get('_category') or account.get('category')
        self.has_balance = has_field(category, 'current_balance')
        self.has_due = has_field(category, 'due_date')

        self._balance = None
        self._as_of = None
        self._due = None

        if self.has_balance:
            self._balance = QLineEdit()
            self._balance.setPlaceholderText('0.00')
            value = account.get('current_balance')
            self._balance.setText('' if value is None else str(value))
            self.add_field(f'New {value_noun(category)}', self._balance)
            self._balance.returnPressed.connect(self._submit)

            self._as_of = date_edit(self._opened_on, maximum=self._opened_on)
            self._as_of.setToolTip(
                'The date this figure is from. Backdating logs it to history '
                'without changing the current value.'
            )
            self.add_field('As Of', self._as_of)

        if self.has_due:
            # An account with no due date must not acquire one just by passing
            # through here. The minimum doubles as the "no date" sentinel, the
            # same way the account form does it, so an empty field reads back as
            # empty instead of as whatever day the dialog happened to open on.
            # The occurrence still ahead, not the anchor on file, so this field
            # agrees with what the list and the detail pane are showing. Saving
            # it back re-anchors to the same day of the month, which changes
            # nothing for a recurring account and is exactly right for a one-off.
            effective = next_due_date(account.get('due_date'),
                                      account.get(DUE_CYCLE_KEY))
            self._due = date_edit(to_qdate(effective, default=NO_DATE),
                                  minimum=NO_DATE, no_date_text=' ')

            # Steps by the account's own cycle rather than always a month.
            # A quarterly bill pressed four times to reach the next one is the
            # button doing arithmetic the record already states, and an annual
            # one is twelve presses — at which point nobody uses it.
            self._cycle = account.get(DUE_CYCLE_KEY)
            noun = cycle_noun(self._cycle)
            advance = QPushButton(f'+1 {noun}')
            advance.setObjectName('Small')
            advance.setToolTip(f'Move the due date on by one {noun}')
            advance.clicked.connect(self._advance)

            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(self._due, 1)
            row.addWidget(advance)

            self.body.addWidget(field_label('Due Date'))
            self.body.addLayout(row)

        self.add_button('Cancel', self.reject)
        self.add_button('Save', self._submit, 'Primary')

        if self._balance:
            self._balance.selectAll()
            self._balance.setFocus()
        elif self._due:
            self._due.setFocus()

    def _advance(self):
        # From an empty field this means one period from today, not one period
        # past the sentinel that stands in for no date at all.
        current = None if self._due_is_empty() else self._due.date().toString('yyyy-MM-dd')
        # ONE_OFF is passed through rather than special-cased: advance_cycle
        # reads it as a month, which is what this button did before any other
        # cycle existed.
        self._due.setDate(to_qdate(advance_cycle(current, self._cycle)))

    def _due_is_empty(self) -> bool:
        return self._due.date() == NO_DATE

    def _submit(self):
        self.clear_error()
        if self._balance and self._balance.text().strip():
            if parse_money(self._balance.text()) is None:
                self.fail('Enter a valid number (e.g. 2,501.99 or $2,501.99).')
                return
        self.accept()

    def changes(self) -> dict:
        """The edited values, keyed to match the flat account dict."""
        result = {}
        if self._balance:
            text = self._balance.text().strip()
            if text:
                # trim_number, so a whole figure is stored as 1200 rather than
                # 1200.0 — the same spelling the account form writes through
                # _read_widget, and the one _sync_current_balance copies back
                # off a history entry. categories.trim_number is the single
                # definition of it; this path used to hand the raw float
                # straight through, so the figure a vault held depended on
                # which of the two forms last touched it.
                result['current_balance'] = trim_number(parse_money(text))
        if self._due:
            # None, not a missing key: an emptied field has to reach the save as
            # a value so it clears the stored date, which is the same reading the
            # account form gives an empty date field.
            result['due_date'] = (None if self._due_is_empty()
                                  else self._due.date().toString('yyyy-MM-dd'))
        return result

    def balance_date(self) -> str | None:
        """The date the entered amount is from, or None to record it as of now.

        An untouched field reads as None rather than as its own default, so a
        dialog that sat open past midnight still files the amount under the day
        it is saved instead of quietly backdating it. Kept out of changes()
        because it describes the history entry, not a field on the account."""
        if self._as_of is None or self._as_of.date() == self._opened_on:
            return None
        return self._as_of.date().toString('yyyy-MM-dd')


class BalanceEntryDialog(_BaseDialog):
    """Edits one balance-history entry's amount and date. Deleting the entry
    lives here too, behind a confirmation, so it can't be hit by accident from
    the history table.

    Editing the newest entry re-states what the account holds now, so a change
    here can move the account's own value — see _sync_current_balance."""

    def __init__(self, parent, balance, recorded_at, noun=DEFAULT_VALUE_NOUN):
        super().__init__(parent, f'Edit {noun} Entry', width=340)
        self.delete_requested = False
        self._noun = noun

        self._amount = QLineEdit(str(balance))
        self.add_field(noun, self._amount)
        self._amount.returnPressed.connect(self._submit)

        # No future dates, as in QuickUpdateDialog: the latest-dated entry is
        # the account's current figure, so one dated ahead would hold the
        # account on it.
        #
        # An entry that is *already* future-dated raises the cap to its own date
        # instead. Nothing writes one now — the CSV import drops future rows and
        # this dialog caps them — but vaults written before those checks still
        # hold them, which is why services/accounts._newest_entry exists to
        # ignore them. Clamping one here rewrote the date to today on a save
        # meant only to fix a typo in the amount, which then promoted the entry
        # to the account's current balance. The date change was never shown and
        # never confirmed.
        stored = to_qdate(recorded_at)
        cap = QDate.currentDate()
        if stored > cap:
            cap = stored
        self._date = date_edit(stored, maximum=cap)
        self.add_field('Date', self._date)

        # Sits left of the stretch so it stays clear of Cancel/Save.
        delete = QPushButton('Delete')
        delete.setObjectName('Danger')
        delete.setToolTip('Remove this entry')
        delete.clicked.connect(self._delete)
        self.buttons.insertWidget(0, delete)

        self.add_button('Cancel', self.reject)
        self.add_button('Save', self._submit, 'Primary')

        self._amount.selectAll()
        self._amount.setFocus()

    def _delete(self):
        confirmed = QMessageBox.question(
            self, 'Delete Entry',
            f'Delete this {self._noun.lower()} entry? This cannot be undone.',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirmed != QMessageBox.Yes:
            return
        self.delete_requested = True
        self.accept()

    def _submit(self):
        self.clear_error()
        if parse_money(self._amount.text()) is None:
            self.fail('Enter a valid amount.')
            return
        self.accept()

    def values(self) -> tuple[str, str]:
        # canonical_amount rather than str(): it is trim_number in the form
        # history is stored in, so an edited entry is spelled exactly as an
        # imported one — see services/history_io.
        return (history_io.canonical_amount(parse_money(self._amount.text())),
                self._date.date().toString('yyyy-MM-dd'))


# -----------------------------------------------------------------------------
# History import
# -----------------------------------------------------------------------------

class ImportHistoryDialog(_BaseDialog):
    """Loads a table of dates and amounts into the selected account's history.

    Two ways in, because the data arrives both ways: a CSV a bank exported, or
    a block of rows copied straight out of a spreadsheet. Either lands in the
    same box and is parsed the same way.

    Everything below the box is a preview that re-reads on every change — the
    counts, which column was taken for what, and the entries themselves. The
    columns are named on purpose: this guesses at which one holds the money
    when the headers do not say, and a wrong guess has to be visible before the
    Import button is pressed rather than found later in the chart."""

    # Enough preview to check the parse against the file without building a
    # table of every row on each keystroke.
    PREVIEW_ROWS = 100

    def __init__(self, parent, noun: str = DEFAULT_VALUE_NOUN, existing=None):
        super().__init__(parent, f'Import {noun} History', width=580)
        self._noun = noun
        self._existing = existing or []
        self._parsed = history_io.prepare('', noun=noun)

        description = QLabel(
            f'Paste rows of dates and {noun.lower()} figures, or choose a CSV file. '
            'One row per entry, in any common layout — the date and amount columns '
            'are found for you. A month on its own (2024-04) is filed on the 1st.'
        )
        description.setWordWrap(True)
        description.setObjectName('Subtle')
        self.body.addWidget(description)

        choose = QPushButton('Choose File…')
        choose.setToolTip('Load a .csv or .txt file of dates and amounts')
        choose.clicked.connect(self._choose_file)

        self._source = QLabel('Nothing loaded')
        self._source.setObjectName('Subtle')

        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        source_row.addWidget(choose)
        source_row.addWidget(self._source, 1)
        self.body.addLayout(source_row)

        self._text = QPlainTextEdit()
        self._text.setPlaceholderText(
            '2024-01-05, 1200.00\n02/01/2024, 1,340.50\n'
            'Mar 1 2024  $1,410\n2024-04, 1,500'
        )
        self._text.setMinimumHeight(110)
        self._text.textChanged.connect(self._schedule_preview)
        self.body.addWidget(field_label('Rows'))
        self.body.addWidget(self._text)

        # Re-parsing is cheap but not free, and textChanged fires per keystroke.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(200)
        self._debounce.timeout.connect(self._refresh_preview)

        self._summary = QLabel()
        self._summary.setWordWrap(True)
        self._detail = QLabel()
        self._detail.setObjectName('Subtle')
        self._detail.setWordWrap(True)
        self.body.addWidget(self._summary)
        self.body.addWidget(self._detail)

        self._preview = QTableWidget(0, 2)
        self._preview.setHorizontalHeaderLabels(['Date', noun])
        self._preview.verticalHeader().setVisible(False)
        self._preview.setEditTriggers(QTableWidget.NoEditTriggers)
        self._preview.setSelectionMode(QTableWidget.NoSelection)
        self._preview.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._preview.setMinimumHeight(150)
        self.body.addWidget(self._preview, 1)
        self.expand_body()

        self.add_button('Cancel', self.reject)
        self._import = self.add_button('Import', self._submit, 'Primary')

        self._refresh_preview()
        self._text.setFocus()

    # -- source ------------------------------------------------------------

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, f'Select {self._noun} History', '',
            'History Files (*.csv *.tsv *.txt);;All Files (*)',
        )
        if not path:
            return
        try:
            text = history_io.read_file(path)
        except Exception as exc:
            self.fail(str(exc))
            return
        self.clear_error()
        self._source.setText(Path(path).name)
        # Into the same box the paste goes into, so a file whose columns were
        # read wrongly can be corrected here instead of in a spreadsheet.
        self._text.setPlainText(text)
        self._refresh_now()

    # -- preview -----------------------------------------------------------

    def _schedule_preview(self):
        self._debounce.start()

    def _refresh_now(self):
        """Brings the preview up to date without waiting out the debounce, for
        the two moments that cannot use a stale one: a file just loaded, and
        the Import button about to read what was parsed."""
        self._debounce.stop()
        self._refresh_preview()

    def _refresh_preview(self):
        self._parsed = history_io.prepare(
            self._text.toPlainText(), self._existing, self._noun,
        )
        entries = self._parsed['entries']

        self._summary.setText(history_io.summarise(self._parsed))
        self._detail.setText(self._describe())
        self._import.setEnabled(bool(entries))

        # Newest first, matching the history table these will join.
        shown = list(reversed(entries))[:self.PREVIEW_ROWS]
        self._preview.setRowCount(len(shown))
        for index, (amount, recorded_at) in enumerate(shown):
            self._preview.setItem(index, 0, QTableWidgetItem(format_date(recorded_at) or ''))
            self._preview.setItem(index, 1, QTableWidgetItem(format_money(amount)))
        self._preview.resizeRowsToContents()

    def _describe(self) -> str:
        """The columns that were used and the rows that were not, on one line."""
        parts = []
        date_column = self._parsed['date_column']
        amount_column = self._parsed['amount_column']
        if date_column and amount_column:
            parts.append(f"Reading '{date_column}' as the date and "
                         f"'{amount_column}' as the {self._noun.lower()}.")
        skipped = history_io.skipped_summary(self._parsed)
        if skipped:
            parts.append(skipped)
        hidden = len(self._parsed['entries']) - self.PREVIEW_ROWS
        if hidden > 0:
            parts.append(f'Showing the newest {self.PREVIEW_ROWS} of them.')
        return ' '.join(parts)

    # -- result ------------------------------------------------------------

    def _submit(self):
        self.clear_error()
        # A paste followed straight away by a click lands inside the debounce
        # window, where self._parsed still describes the empty box.
        self._refresh_now()
        if not self._parsed['entries']:
            self.fail('Nothing here can be read as a date and an amount.')
            return
        self.accept()

    def entries(self) -> list[tuple[str, str]]:
        """(amount, YYYY-MM-DD) pairs, oldest first — the order to insert in."""
        return list(self._parsed['entries'])


class ImportAccountsDialog(_BaseDialog):
    """Loads a table of accounts into the vault.

    The mirror of the whole-vault CSV export, and built to look like the
    history import beside it: the same two ways in, the same live preview, the
    same rule that a row it is unsure of is reported rather than invented.

    What it shows that the history import does not is the column mapping, named
    in full. This decides where every value in the file lands — which column is
    the password and which is the security answer — and a file headed in
    somebody else's vocabulary can easily be read a way the user did not mean.
    That has to be readable before Import is pressed, not discovered later in
    an account that will not log in.
    """

    # Enough preview to check the mapping against the file without building a
    # table of every row on each keystroke.
    PREVIEW_ROWS = 100

    # The columns the preview shows, out of however many the file has. These
    # are what identify an account at a glance; a password column would only
    # put every secret in the file on screen at once, which is the one thing
    # this dialog has no reason to do.
    PREVIEW_COLUMNS = (('name', 'Name'), ('category', 'Category'),
                       ('username', 'Username'), ('website', 'Website'))

    def __init__(self, parent, existing=None, categories=None):
        super().__init__(parent, 'Import Accounts', width=620)
        self._existing = existing or []
        self._parsed = account_import.prepare('')

        description = QLabel(
            'Paste rows of accounts, or choose a CSV file — an export from this '
            'app, from another password manager, or a spreadsheet of your own. '
            'The first row has to name the columns; which field each one is read '
            'as is shown below before anything is written.'
        )
        description.setWordWrap(True)
        description.setObjectName('Subtle')
        self.body.addWidget(description)

        choose = QPushButton('Choose File…')
        choose.setToolTip('Load a .csv or .txt file of accounts')
        choose.clicked.connect(self._choose_file)

        self._source = QLabel('Nothing loaded')
        self._source.setObjectName('Subtle')

        source_row = QHBoxLayout()
        source_row.setSpacing(8)
        source_row.addWidget(choose)
        source_row.addWidget(self._source, 1)
        self.body.addLayout(source_row)

        self._text = QPlainTextEdit()
        self._text.setPlaceholderText(
            'name,category,username,password,website\n'
            'Chase Visa,Credit Cards,jsmith,secret,https://chase.com'
        )
        self._text.setMinimumHeight(110)
        self._text.textChanged.connect(self._schedule_preview)
        self.body.addWidget(field_label('Rows'))
        self.body.addWidget(self._text)

        # Where a row with no category of its own is filed. Offered as a choice
        # rather than defaulted silently, because it is the one value this
        # dialog supplies instead of reading — see the note in
        # services/account_import.
        self._category = QComboBox()
        for name in (categories or list(CATEGORY_CONFIG)):
            self._category.addItem(name)
        index = self._category.findText('Websites')
        self._category.setCurrentIndex(max(0, index))
        self._category.currentIndexChanged.connect(self._refresh_now)
        self.add_field('File Rows With No Category Under', self._category)

        # Re-parsing is cheap but not free, and textChanged fires per keystroke.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(200)
        self._debounce.timeout.connect(self._refresh_preview)

        self._summary = QLabel()
        self._summary.setWordWrap(True)
        self._columns = QLabel()
        self._columns.setObjectName('Subtle')
        self._columns.setWordWrap(True)
        self._detail = QLabel()
        self._detail.setObjectName('Subtle')
        self._detail.setWordWrap(True)
        self.body.addWidget(self._summary)
        self.body.addWidget(self._columns)
        self.body.addWidget(self._detail)

        headings = [label for _, label in self.PREVIEW_COLUMNS]
        self._preview = QTableWidget(0, len(headings))
        self._preview.setHorizontalHeaderLabels(headings)
        self._preview.verticalHeader().setVisible(False)
        self._preview.setEditTriggers(QTableWidget.NoEditTriggers)
        self._preview.setSelectionMode(QTableWidget.NoSelection)
        self._preview.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._preview.setMinimumHeight(160)
        self.body.addWidget(self._preview, 1)

        # Not a red warning, because nothing dangerous is about to happen here:
        # the values are moving into the vault, which is the safe direction.
        # What is worth saying is what the *file* is, since it is a plaintext
        # copy of everything in it sitting wherever the user left it.
        note = QLabel(
            'The file you are importing from holds these passwords in plain text. '
            'Delete it once the accounts are in.'
        )
        note.setObjectName('FieldHint')
        note.setWordWrap(True)
        self.body.addWidget(note)
        self.expand_body()

        self.add_button('Cancel', self.reject)
        self._import = self.add_button('Import', self._submit, 'Primary')

        self._refresh_preview()
        self._text.setFocus()

    # -- source ------------------------------------------------------------

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select Account List', '',
            'Account Files (*.csv *.tsv *.txt);;All Files (*)',
        )
        if not path:
            return
        try:
            text = account_import.read_file(path)
        except Exception as exc:
            self.fail(str(exc))
            return
        self.clear_error()
        self._source.setText(Path(path).name)
        # Into the same box the paste goes into, so a file whose columns were
        # read wrongly can be corrected here instead of in a spreadsheet.
        self._text.setPlainText(text)
        self._refresh_now()

    # -- preview -----------------------------------------------------------

    def _schedule_preview(self):
        self._debounce.start()

    def _refresh_now(self):
        """Brings the preview up to date without waiting out the debounce, for
        the moments that cannot use a stale one: a file just loaded, the default
        category changed, and the Import button about to read what was parsed."""
        self._debounce.stop()
        self._refresh_preview()

    def _refresh_preview(self):
        try:
            self._parsed = account_import.prepare(
                self._text.toPlainText(), self._existing,
                self._category.currentText(),
            )
        except account_import.AccountImportError as exc:
            self.fail(str(exc))
            self._parsed = account_import.prepare('')
        else:
            self.clear_error()

        rows = self._parsed['rows']
        self._summary.setText(account_import.summarise(self._parsed))
        self._columns.setText(account_import.columns_summary(self._parsed))
        self._detail.setText(self._describe())
        self._import.setEnabled(bool(rows))

        shown = rows[:self.PREVIEW_ROWS]
        self._preview.setRowCount(len(shown))
        for index, row in enumerate(shown):
            for column, (key, _) in enumerate(self.PREVIEW_COLUMNS):
                self._preview.setItem(
                    index, column, QTableWidgetItem(str(row.get(key) or '')))
        self._preview.resizeRowsToContents()

    def _describe(self) -> str:
        """The rows that were not imported, and how much of the rest is shown."""
        parts = []
        skipped = account_import.skipped_summary(self._parsed)
        if skipped:
            parts.append(skipped)
        hidden = len(self._parsed['rows']) - self.PREVIEW_ROWS
        if hidden > 0:
            parts.append(f'Showing the first {self.PREVIEW_ROWS} of them.')
        return ' '.join(parts)

    # -- result ------------------------------------------------------------

    def _submit(self):
        self.clear_error()
        # A paste followed straight away by a click lands inside the debounce
        # window, where self._parsed still describes the empty box.
        self._refresh_now()
        if not self._parsed['rows']:
            self.fail(account_import.summarise(self._parsed)
                      if not self._parsed['has_header']
                      else 'Nothing here can be read as an account.')
            return
        self.accept()

    def rows(self) -> list[dict]:
        """Flat account dicts, in file order — ready for create_accounts."""
        return list(self._parsed['rows'])


class ExportAccountDialog(_BaseDialog):
    """Asks what to put in a plaintext export of one account.

    The three scopes are not equally consequential, and the dialog's job is to
    make that visible rather than to list them evenly: two of these files carry
    the account's password, one carries dates and figures. The warning under the
    options changes with the selection and says which kind is about to be
    written, so the choice is made against its consequence rather than against
    a label.

    History is the default for the same reason. It is the option that cannot
    leak a credential, and it is what this button did before it grew the other
    two — a click straight through to Export is the safe outcome, not the
    dangerous one. Categories that track no balance are offered only the
    account scope, since there is no history for them to be a default of."""

    def __init__(self, parent, noun: str = DEFAULT_VALUE_NOUN, has_history: bool = True):
        super().__init__(parent, 'Export Account', width=540)

        description = QLabel(
            'Choose what to include. The file is a plaintext CSV — useful in a '
            'spreadsheet, and readable by anything that can open it.'
        )
        description.setWordWrap(True)
        description.setObjectName('Subtle')
        self.body.addWidget(description)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._scopes = {}

        for scope in account_export.scopes(noun):
            if not has_history and scope['key'] != account_export.SCOPE_ACCOUNT:
                continue
            radio = QRadioButton(scope['label'])
            # toggled, not clicked, so a keyboard selection counts too — but only
            # the half that turns one on: the pair otherwise rebuilt the warning
            # and re-polished the button twice per change.
            radio.toggled.connect(
                lambda checked: self._refresh_warning() if checked else None)
            self._group.addButton(radio)
            self._scopes[radio] = scope

            detail = QLabel(scope['detail'])
            detail.setObjectName('Subtle')
            detail.setWordWrap(True)
            # Indented under its own option so it reads as that option's
            # explanation rather than as the next one's heading.
            detail.setContentsMargins(24, 0, 0, 6)

            self.body.addWidget(radio)
            self.body.addWidget(detail)

        self._warning = QFrame()
        self._warning.setObjectName('WarningBox')
        warning_layout = QHBoxLayout(self._warning)
        warning_layout.setContentsMargins(14, 12, 14, 12)
        warning_layout.setSpacing(10)
        self._warning_icon = ThemedIcon('alert', role='danger', size=17)
        self._warning_text = QLabel()
        self._warning_text.setWordWrap(True)
        warning_layout.addWidget(self._warning_icon)
        warning_layout.addWidget(self._warning_text, 1)
        self.body.addWidget(self._warning)

        self.add_button('Cancel', self.reject)
        self._confirm = self.add_button('Export', self.accept, 'Primary')

        # Chosen last on purpose: checking a radio emits toggled, and the slot
        # that handles it reads the warning box and the button this line follows.
        default = account_export.SCOPE_HISTORY if has_history else account_export.SCOPE_ACCOUNT
        for radio, scope in self._scopes.items():
            if scope['key'] == default:
                radio.setChecked(True)
                break

        self._refresh_warning()

    def _refresh_warning(self):
        """Restates the risk of whatever is currently selected.

        Rebuilt rather than shown and hidden: an empty box that keeps its place
        reads as a box with nothing in it, and one that disappears makes the
        dialog jump under the pointer between two options."""
        scope = self.scope()
        credentials = account_export.includes_credentials(scope)

        # Same pairing CsvWarningDialog uses: the alert glyph in the danger
        # colour, or the info one muted, so severity survives a theme change.
        self._warning_icon.set_icon_name(
            'alert' if credentials else 'info', 'danger' if credentials else 'subtle')
        self._warning_text.setText(
            'This file will contain this account’s password and every other '
            'field, in plain text. Anyone who can read the file can read them.'
            if credentials else
            'This file is not encrypted, but it holds only dates and figures — '
            'no name, username or password.'
        )
        # The button says what it is about to do, so a dangerous export cannot
        # be finished without the word for it passing under the cursor.
        self._confirm.setObjectName('Danger' if credentials else 'Primary')
        self._confirm.setText('Export With Password' if credentials else 'Export')
        # An object name change is invisible until the widget is restyled.
        self._confirm.style().unpolish(self._confirm)
        self._confirm.style().polish(self._confirm)

    def scope(self) -> str:
        """The selected scope, falling back to an option actually on offer.

        The fallback used to be SCOPE_HISTORY unconditionally, which a
        no-history dialog never built a radio for — so an unchecked group would
        have answered with a history-only export for an account that has none."""
        checked = self._group.checkedButton()
        if checked is not None:
            return self._scopes[checked]['key']
        return next(iter(self._scopes.values()))['key'] if self._scopes             else account_export.SCOPE_ACCOUNT
