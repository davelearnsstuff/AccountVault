# -----------------------------------------------------------------------------
# ui/dialogs.py
# Modal dialogs: settings, warnings, the readme and design document viewer,
# about, the demo vault's password, CSV warning, change master password, the
# quick balance/due-date update, the history entry editor, the account and
# history imports, and the per-account export.
# -----------------------------------------------------------------------------

from pathlib import Path

from PySide6.QtCore import QDate, QEvent, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QGuiApplication, QKeySequence, QShortcut,
    QTextCharFormat, QTextCursor,
)
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSpacerItem, QSpinBox,
    QTableWidget, QTableWidgetItem, QTextBrowser, QTextEdit,
    QVBoxLayout, QWidget,
)

from core import auth
from core import categories
from core import preferences
from core.categories import (
    CATEGORY_CONFIG, DEFAULT_VALUE_NOUN, DUE_CYCLE_KEY, advance_cycle, cycle_noun,
    format_date, format_money, get_config,
    has_field, next_due_date, parse_money, trim_number, value_noun,
)
from core.urls import normalize_url
from core.version import VERSION
from services import account_export, account_import, history_io
from ui.theme import THEMES, palette
from ui.widgets import (
    NO_DATE, ThemedIcon, date_edit, field_label, password_field,
    password_requirements, plain_label, to_qdate,
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

        # Errors can quote file contents and exception text.
        self.error = plain_label()
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
        # Keeps short forms pinned to the top; expand_body() drops it.
        self._filler = QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding)
        self._outer.addItem(self._filler)
        self._outer.addLayout(self.buttons)

    def expand_body(self):
        """Gives the body the spare height, for bodies holding a scrollable view."""
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

        # Inline colour on purpose: this previews some *other* theme, so it must
        # not follow the active palette.
        preview = QFrame()
        preview.setFixedHeight(30)
        preview.setStyleSheet(
            f"background-color: {theme['bg_primary']};"
            f"border: 1px solid {theme['border']}; border-radius: 4px;"
        )
        stripe = QFrame(preview)
        stripe.setStyleSheet(f"background-color: {theme['accent']}; border-radius: 2px;")
        stripe.setGeometry(8, 11, 60, 6)

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
    """Theme, font size, list density, the timeouts and automatic backups. The
    appearance settings preview live; cancelling reverts them."""

    preview_requested = Signal(str, str, str)   # theme, font_size, list_density

    def __init__(self, parent, settings: dict):
        super().__init__(parent, 'Settings', width=520)
        # Exactly what preview_requested carries, so reject() can undo a preview.
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
        for key, label in preferences.FONT_SIZE_CHOICES:
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
        """Automatic copies of the vault: whether, how often, where, how long."""
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
        self._location_label = plain_label()
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

        self._backup_status = plain_label()
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
        start = (self._location if self._location != preferences.BACKUP_LOCATION_DEFAULT
                 else '')
        folder = QFileDialog.getExistingDirectory(self, 'Backup Folder', start)
        if folder:
            self._location = folder
            self._sync_location_label()

    def _use_default_location(self):
        self._location = preferences.BACKUP_LOCATION_DEFAULT
        self._sync_location_label()

    def _show_last_backup(self):
        """The one line that says whether any of this is actually working. A
        failure stays on screen: silently failing backups are believed in."""
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
        # Every preview carries all three; the two not clicked read back as
        # what is on screen.
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
# Warnings
#
# Its own dialog rather than part of Settings: a row per category, custom ones
# included — vault content, so it is only offered while the vault is open.
# -----------------------------------------------------------------------------

class WarningsDialog(_BaseDialog):
    """When due dates and last-updated stamps turn orange and red, and which
    categories get either check. Opens on the settings in force and validates
    on Save; the caller stores what values() returns."""

    def __init__(self, parent, category_names: list[str]):
        super().__init__(parent, 'Warnings', width=600)
        self.expand_body()
        self._names = list(category_names)
        self._rows = {}

        current = categories.warning_settings(self._names)

        # --- Due dates: one set of thresholds for every category ---
        self.body.addWidget(field_label('Due Dates'))
        due_grid = QGridLayout()
        due_grid.setHorizontalSpacing(10)
        due_grid.setVerticalSpacing(6)
        self._due_warn = self._spin()
        self._due_danger = self._spin()
        self._lapsed = self._spin()
        for row, (caption, spin, tail) in enumerate((
            ('Orange within', self._due_warn, 'days before the due date'),
            ('Red within', self._due_danger, 'days before, or once overdue'),
            ('Lapsed after', self._lapsed, 'days overdue (one-off dates only)'),
        )):
            due_grid.addWidget(QLabel(caption), row, 0)
            due_grid.addWidget(spin, row, 1)
            hint = QLabel(tail)
            hint.setObjectName('FieldHint')
            hint.setWordWrap(True)
            due_grid.addWidget(hint, row, 2)
        due_grid.setColumnStretch(2, 1)
        self.body.addLayout(due_grid)

        # --- Per category ---
        self.body.addSpacing(6)
        self.body.addWidget(field_label('By Category'))
        explainer = QLabel(
            'Stale means the Last Updated date is older than this many days. '
            'Due-date switches appear only where the category has a due date.')
        explainer.setObjectName('FieldHint')
        explainer.setWordWrap(True)
        self.body.addWidget(explainer)

        table = QWidget()
        grid = QGridLayout(table)
        grid.setContentsMargins(0, 0, 8, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)
        for column, caption in enumerate(
                ('Category', 'Due Dates', 'Stale', 'Orange After', 'Red After')):
            grid.addWidget(field_label(caption), 0, column)

        for row, name in enumerate(self._names, start=1):
            grid.addWidget(plain_label(get_config(name).get('title', name)), row, 0)

            due = None
            if has_field(name, 'due_date'):
                due = QCheckBox()
                due.setAccessibleName(f'{name} due-date warnings')
                grid.addWidget(due, row, 1)
            else:
                dash = QLabel('—')
                dash.setObjectName('FieldHint')
                grid.addWidget(dash, row, 1)

            stale = QCheckBox()
            stale.setAccessibleName(f'{name} stale warnings')
            warn, danger = self._spin(), self._spin()
            stale.toggled.connect(warn.setEnabled)
            stale.toggled.connect(danger.setEnabled)
            grid.addWidget(stale, row, 2)
            grid.addWidget(warn, row, 3)
            grid.addWidget(danger, row, 4)
            self._rows[name] = (due, stale, warn, danger)

        grid.setColumnStretch(0, 1)
        grid.setRowStretch(len(self._names) + 1, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(table)
        # Wide enough for every column, since sideways scrolling is off.
        scroll.setMinimumWidth(table.sizeHint().width()
                               + scroll.verticalScrollBar().sizeHint().width())
        scroll.setMinimumHeight(260)
        self.body.addWidget(scroll, 1)

        self._load(current)

        reset = QPushButton('Reset to Defaults')
        reset.clicked.connect(
            lambda: self._load(categories.default_warning_settings(self._names)))
        # Left of the stretch, away from Save: it changes every field at once.
        self.buttons.insertWidget(0, reset)
        self.add_button('Cancel', self.reject)
        self.add_button('Save', self.accept, 'Primary')

    @staticmethod
    def _spin() -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(categories.WARNING_DAYS_MIN, categories.WARNING_DAYS_MAX)
        spin.setFixedWidth(84)
        return spin

    def _load(self, settings: dict):
        self.clear_error()
        self._due_warn.setValue(settings['due_warn'])
        self._due_danger.setValue(settings['due_danger'])
        self._lapsed.setValue(settings['lapsed_after'])
        for name, (due, stale, warn, danger) in self._rows.items():
            entry = settings['categories'][name]
            if due is not None:
                due.setChecked(entry['due'])
            warn.setValue(entry['stale_warn'])
            danger.setValue(entry['stale_danger'])
            stale.setChecked(entry['stale'])
            # toggled only fires on a change, so set enablement explicitly.
            warn.setEnabled(entry['stale'])
            danger.setEnabled(entry['stale'])

    def values(self) -> dict:
        entries = {}
        for name, (due, stale, warn, danger) in self._rows.items():
            entries[name] = {
                # A category with no due-date field keeps whatever it had.
                'due': (due.isChecked() if due is not None
                        else categories.category_warnings(name)['due']),
                'stale': stale.isChecked(),
                'stale_warn': warn.value(),
                'stale_danger': danger.value(),
            }
        return {
            'due_warn': self._due_warn.value(),
            'due_danger': self._due_danger.value(),
            'lapsed_after': self._lapsed.value(),
            'categories': entries,
        }

    def accept(self):
        try:
            categories.validate_warning_settings(self.values())
        except ValueError as exc:
            self.fail(str(exc))
            return
        super().accept()


# -----------------------------------------------------------------------------
# About
# -----------------------------------------------------------------------------

class DocumentDialog(_BaseDialog):
    """Renders one of the bundled Markdown documents, with a find bar.

    Opens no links itself: anything external goes to the desktop browser, after
    the same check a stored website gets."""

    # The docs are a few thousand words; this only stops a one-letter search
    # from building an unbounded list of highlights.
    MAX_MATCHES = 999

    def __init__(self, parent, title: str, path: Path):
        super().__init__(parent, title, width=760)
        self.setMinimumHeight(560)
        self._matches = []
        self._current = -1
        self._capped = False
        self._formats = self._match_formats(getattr(parent, 'settings', {}).get('theme'))

        self.search = QLineEdit()
        self.search.setPlaceholderText('Find in this document (Ctrl+F)')
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName(f'Find in {title}')
        self.search.textChanged.connect(self._on_query_changed)
        # Enter and Shift+Enter step through matches. Caught here rather than
        # through returnPressed, which cannot tell them apart and would let the
        # dialog treat Enter as a press of its Close button.
        self.search.installEventFilter(self)

        self._previous = QPushButton('Previous')
        self._previous.setObjectName('Small')
        self._previous.setToolTip('Previous match (Shift+Enter)')
        self._previous.clicked.connect(lambda: self._step(-1))
        self._next = QPushButton('Next')
        self._next.setObjectName('Small')
        self._next.setToolTip('Next match (Enter)')
        self._next.clicked.connect(lambda: self._step(1))

        self._count = plain_label()
        self._count.setObjectName('FieldHint')
        self._count.setMinimumWidth(90)

        find_row = QHBoxLayout()
        find_row.setSpacing(8)
        find_row.addWidget(self.search, 1)
        find_row.addWidget(self._count)
        find_row.addWidget(self._previous)
        find_row.addWidget(self._next)
        self.body.addLayout(find_row)

        self.view = QTextBrowser()
        # Not setOpenExternalLinks, which would hand any anchor straight to the
        # OS without normalize_url in front of it.
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self._follow)
        self.view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        try:
            self.view.setMarkdown(Path(path).read_text(encoding='utf-8'))
        except OSError as exc:
            self.view.setPlainText(f"Could not read {Path(path).name}: {exc}")
        self.body.addWidget(self.view, 1)
        self.expand_body()

        for sequence, slot in ((QKeySequence.Find, self._focus_search),
                               (QKeySequence.FindNext, lambda: self._step(1)),
                               (QKeySequence.FindPrevious, lambda: self._step(-1))):
            QShortcut(QKeySequence(sequence), self).activated.connect(slot)

        self.add_button('Close', self.accept)
        self.resize(self._preferred_size(parent))
        self._show_current()
        # The document rather than the find bar, so the arrow and page keys
        # scroll it straight away.
        self.view.setFocus()

    # -- finding -----------------------------------------------------------

    @staticmethod
    def _match_formats(theme) -> tuple[QTextCharFormat, QTextCharFormat]:
        """(every match, the current match). The current one takes the same
        colors as a selection; the rest a faint wash of the warning color, which
        stays readable under the text in both themes."""
        colors = palette(theme)
        others = QTextCharFormat()
        wash = QColor(colors['level_warn'])
        wash.setAlpha(80)
        others.setBackground(wash)
        current = QTextCharFormat()
        current.setBackground(QColor(colors['accent']))
        current.setForeground(QColor('#ffffff'))
        return others, current

    def _focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def _on_query_changed(self, text: str):
        """Finds every match. Typing keeps the place: the first match at or after
        the current one, so narrowing a word does not jump back to the top."""
        anchor = (self._matches[self._current].selectionStart()
                  if self._current >= 0 else 0)
        self._matches, self._capped = self._find_all(text)
        self._current = next((index for index, match in enumerate(self._matches)
                              if match.selectionStart() >= anchor),
                             0 if self._matches else -1)
        self._show_current()

    def _find_all(self, text: str) -> tuple[list, bool]:
        """Every match of text in the rendered document, case-insensitive, and
        whether the list stopped at MAX_MATCHES."""
        if not text:
            return [], False
        document = self.view.document()
        matches = []
        cursor = document.find(text)
        while not cursor.isNull():
            if len(matches) == self.MAX_MATCHES:
                return matches, True
            matches.append(cursor)
            cursor = document.find(text, cursor)
        return matches, False

    def _step(self, step: int):
        """Moves to the next or previous match, wrapping at either end."""
        if not self._matches:
            return
        self._current = (self._current + step) % len(self._matches)
        self._show_current()

    def _show_current(self):
        others, current = self._formats
        selections = []
        for index, match in enumerate(self._matches):
            selection = QTextEdit.ExtraSelection()
            selection.cursor = match
            selection.format = current if index == self._current else others
            selections.append(selection)
        self.view.setExtraSelections(selections)

        if self._current >= 0:
            # A bare cursor at the match rather than the match selected: the
            # widget paints its own selection over the highlights, in a grey
            # whenever the find bar has the focus.
            cursor = QTextCursor(self._matches[self._current])
            cursor.clearSelection()
            # Off screen, the match is brought to the middle with the text
            # around it, not to whichever edge it was nearest.
            rect = self.view.cursorRect(cursor)
            if not self.view.viewport().rect().contains(rect):
                bar = self.view.verticalScrollBar()
                bar.setValue(bar.value() + rect.center().y()
                             - self.view.viewport().height() // 2)
            self.view.setTextCursor(cursor)

        total = len(self._matches)
        if not self.search.text():
            self._count.setText('')
        elif not total:
            self._count.setText('No matches')
        else:
            self._count.setText(
                f"{self._current + 1} of {total}{'+' if self._capped else ''}")
        self._previous.setEnabled(total > 1)
        self._next.setEnabled(total > 1)

    def eventFilter(self, watched, event):
        if (watched is self.search and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)):
            self._step(-1 if event.modifiers() & Qt.ShiftModifier else 1)
            return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        """Escape clears an active search first, as it does in the main window,
        and closes the window once there is nothing to clear."""
        if event.key() == Qt.Key_Escape and self.search.text():
            self.search.clear()
            self.view.setFocus()
            return
        super().keyPressEvent(event)

    # -- links and size ----------------------------------------------------

    @staticmethod
    def _follow(url: QUrl):
        """Opens a link from the README, through normalize_url."""
        target = normalize_url(url.toString())
        if target:
            QDesktopServices.openUrl(QUrl(target))

    @staticmethod
    def _preferred_size(parent) -> QSize:
        """Large — this is a document — but never larger than the window it came
        from, or the screen if there is no parent."""
        available = (parent.frameGeometry() if parent is not None
                     else QGuiApplication.primaryScreen().availableGeometry())
        return QSize(min(1000, max(760, int(available.width() * 0.8))),
                     min(880, max(560, int(available.height() * 0.9))))


# Anchored to this file: AccountVault_Windows.bat, a shortcut and a shell all
# launch from somewhere different.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ReadmeDialog(DocumentDialog):
    """The README: features, installation, and the security details."""

    PATH = _PROJECT_ROOT / 'README.md'

    def __init__(self, parent):
        super().__init__(parent, 'Readme', self.PATH)


class DesignDocumentDialog(DocumentDialog):
    """The design document: why the app is built the way it is."""

    PATH = _PROJECT_ROOT / 'docs' / 'AccountVault_Design_Document.md'

    def __init__(self, parent):
        super().__init__(parent, 'Design Document', self.PATH)


class AboutDialog(_BaseDialog):
    ROWS = [
        ('Version',           VERSION),
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


class DemoVaultDialog(_BaseDialog):
    """Shown once a demo vault is built, with its master password: the login
    screen comes next, and nothing else on screen says what the password is.

    The password can be selected, or copied with the button. The button goes
    through the window's copy_to_clipboard, as the detail view's do, so the copy
    stays out of clipboard history and clears itself."""

    copy_requested = Signal(str, str)      # value, field label

    def __init__(self, parent, name: str, total: int, password: str):
        super().__init__(parent, 'Demo Vault', width=420)

        intro = plain_label(f"{name} holds {total} made-up accounts for a fictional "
                            f"household, so every screen has something on it.")
        intro.setWordWrap(True)
        self.body.addWidget(intro)

        self.password = plain_label(password)
        self.password.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)

        self.copy_button = QPushButton('Copy')
        self.copy_button.setObjectName('Small')
        self.copy_button.setAccessibleName('Copy master password')
        self.copy_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.copy_button.clicked.connect(self._copy)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.password)
        row.addWidget(self.copy_button)
        row.addStretch(1)
        self.body.addWidget(field_label('Master password'))
        self.body.addLayout(row)

        hint = plain_label("You'll be asked for it next, to unlock the vault.")
        hint.setObjectName('Subtle')
        hint.setWordWrap(True)
        self.body.addWidget(hint)

        self.add_button('Continue', self.accept, 'Primary')

    def _copy(self):
        self.copy_requested.emit(self.password.text(), 'Master password')
        self.copy_button.setText('Copied')


class CsvWarningDialog(_BaseDialog):
    """Spells out that a CSV export is unencrypted before letting it proceed."""

    # 'alert' carries the theme's danger colour, 'info' the muted one.
    WARNINGS = [
        ('alert', 'This file is not encrypted. Anyone with access to it can read all '
                  'your passwords and account details.'),
        ('info', 'Balance history is not included. Only the current state of each '
                 'account is exported.'),
        ('info', 'Values a spreadsheet would run as a formula — those starting '
                 '= + - @ or with whitespace — and values starting with an '
                 'apostrophe are written with a leading apostrophe, so they are '
                 'shown instead of evaluated. Importing the file back removes it.'),
        ('info', 'For an encrypted backup, use Save a Copy in the Manage menu. '
                 'CSV is for spreadsheets and moving to another app.'),
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
    """Collects the current and new master password, checking the new one
    against the password rules as it is typed. Re-keying is the caller's job."""

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
    and the due date, shown only where the category has them.

    The 'as of' date lets a statement be filed under the day it covers. It
    defaults to today and cannot run ahead of it: a balance from the future is
    always a typo."""

    def __init__(self, parent, account: dict):
        super().__init__(parent, 'Quick Update', width=380)
        # Captured once, so a dialog left open across midnight does not read as
        # backdated.
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
                'The date this figure is from. A backdated figure becomes the '
                'current value only if nothing newer is already on file.'
            )
            self.add_field('As Of', self._as_of)

        if self.has_due:
            # The occurrence still ahead, matching the list and the detail pane.
            # The minimum doubles as "no date", so an account without one does
            # not acquire one by passing through here.
            effective = next_due_date(account.get('due_date'),
                                      account.get(DUE_CYCLE_KEY))
            self._due = date_edit(to_qdate(effective, default=NO_DATE),
                                  minimum=NO_DATE, no_date_text=' ')

            # Steps by the account's own cycle rather than always a month.
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
        # From an empty field, one period from today.
        current = None if self._due_is_empty() else self._due.date().toString('yyyy-MM-dd')
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
                # trim_number, so a whole figure is stored as 1200, the same
                # spelling every other path writes.
                result['current_balance'] = trim_number(parse_money(text))
        if self._due:
            # None, not a missing key, so an emptied field clears the stored date.
            result['due_date'] = (None if self._due_is_empty()
                                  else self._due.date().toString('yyyy-MM-dd'))
        return result

    def balance_date(self) -> str | None:
        """The date the entered amount is from, or None to record it as of now.
        An untouched field reads as None, so a dialog left open past midnight
        does not quietly backdate."""
        if self._as_of is None or self._as_of.date() == self._opened_on:
            return None
        return self._as_of.date().toString('yyyy-MM-dd')


class BalanceEntryDialog(_BaseDialog):
    """Edits one balance-history entry's amount and date. Deleting lives here
    too, behind a confirmation. Editing the newest entry restates what the
    account holds now."""

    def __init__(self, parent, balance, recorded_at, noun=DEFAULT_VALUE_NOUN):
        super().__init__(parent, f'Edit {noun} Entry', width=340)
        self.delete_requested = False
        self._noun = noun

        self._amount = QLineEdit(str(balance))
        self.add_field(noun, self._amount)
        self._amount.returnPressed.connect(self._submit)

        # No future dates, as in QuickUpdateDialog — except that an entry
        # already dated ahead (written before that rule) keeps its own date as
        # the cap, so fixing its amount does not silently move it to today.
        stored = to_qdate(recorded_at)
        cap = QDate.currentDate()
        if stored > cap:
            cap = stored
        self._date = date_edit(stored, maximum=cap)
        self.add_field('Date', self._date)

        # Left of the stretch, clear of Cancel/Save.
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
        return (history_io.canonical_amount(parse_money(self._amount.text())),
                self._date.date().toString('yyyy-MM-dd'))


# -----------------------------------------------------------------------------
# History import
# -----------------------------------------------------------------------------

class ImportHistoryDialog(_BaseDialog):
    """Loads a table of dates and amounts into the selected account's history,
    from a CSV file or rows pasted from a spreadsheet.

    Below the box is a live preview: the counts, which column was taken for
    what, and the entries — so a wrong guess about which column holds the money
    is visible before Import is pressed."""

    # Enough preview to check the parse without a table of every row.
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

        self._source = plain_label('Nothing loaded')
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

        # textChanged fires per keystroke.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(200)
        self._debounce.timeout.connect(self._refresh_preview)

        # Both quote column headings from the file.
        self._summary = plain_label()
        self._summary.setWordWrap(True)
        self._detail = plain_label()
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
        # Into the same box the paste goes into, so it can be corrected here.
        self._text.setPlainText(text)
        self._refresh_now()

    # -- preview -----------------------------------------------------------

    def _schedule_preview(self):
        self._debounce.start()

    def _refresh_now(self):
        """Brings the preview up to date without waiting out the debounce: after
        a file loads, and before Import reads what was parsed."""
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
        # A paste followed at once by a click lands inside the debounce window.
        self._refresh_now()
        if not self._parsed['entries']:
            self.fail('Nothing here can be read as a date and an amount.')
            return
        self.accept()

    def entries(self) -> list[tuple[str, str]]:
        """(amount, YYYY-MM-DD) pairs, oldest first — the order to insert in."""
        return list(self._parsed['entries'])


class ImportAccountsDialog(_BaseDialog):
    """Loads a table of accounts into the vault, built like the history import:
    the same two ways in, the same live preview.

    It also names the column mapping in full — which column is read as the
    password and which as the security answer — so a file headed in someone
    else's vocabulary cannot be misread unseen."""

    PREVIEW_ROWS = 100

    # The columns the preview shows. A password column would put every secret
    # in the file on screen at once.
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

        self._source = plain_label('Nothing loaded')
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

        # Where a row with no category is filed — offered as a choice, since it
        # is the one value this dialog supplies rather than reads.
        self._category = QComboBox()
        for name in (categories or list(CATEGORY_CONFIG)):
            self._category.addItem(name)
        index = self._category.findText('Websites')
        self._category.setCurrentIndex(max(0, index))
        self._category.currentIndexChanged.connect(self._refresh_now)
        self.add_field('File Rows With No Category Under', self._category)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(200)
        self._debounce.timeout.connect(self._refresh_preview)

        # All three quote headings and category names from the file.
        self._summary = plain_label()
        self._summary.setWordWrap(True)
        self._columns = plain_label()
        self._columns.setObjectName('Subtle')
        self._columns.setWordWrap(True)
        self._detail = plain_label()
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

        # The values are moving into the vault, the safe direction; what is
        # worth saying is that the file is a plaintext copy of them.
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
        self._text.setPlainText(text)
        self._refresh_now()

    # -- preview -----------------------------------------------------------

    def _schedule_preview(self):
        self._debounce.start()

    def _refresh_now(self):
        """Brings the preview up to date without waiting out the debounce: after
        a file loads, the default category changes, or Import is pressed."""
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

    Two of the three scopes carry the account's password, so the warning under
    the options follows the selection and the button names what it is about to
    do. History is the default: it is the scope that cannot leak a credential.
    Categories with no balance are offered only the account scope."""

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
            # toggled, so a keyboard selection counts too, but only the half
            # that turns one on.
            radio.toggled.connect(
                lambda checked: self._refresh_warning() if checked else None)
            self._group.addButton(radio)
            self._scopes[radio] = scope

            detail = QLabel(scope['detail'])
            detail.setObjectName('Subtle')
            detail.setWordWrap(True)
            # Indented under its own option.
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

        # Chosen last: checking a radio fires the slot that reads the warning
        # box and the button built just above.
        default = account_export.SCOPE_HISTORY if has_history else account_export.SCOPE_ACCOUNT
        for radio, scope in self._scopes.items():
            if scope['key'] == default:
                radio.setChecked(True)
                break

        self._refresh_warning()

    def _refresh_warning(self):
        """Restates the risk of whatever is currently selected."""
        scope = self.scope()
        credentials = account_export.includes_credentials(scope)

        self._warning_icon.set_icon_name(
            'alert' if credentials else 'info', 'danger' if credentials else 'subtle')
        self._warning_text.setText(
            'This file will contain this account’s password and every other '
            'field, in plain text. Anyone who can read the file can read them.'
            if credentials else
            'This file is not encrypted, but it holds only dates and figures — '
            'no name, username or password.'
        )
        self._confirm.setObjectName('Danger' if credentials else 'Primary')
        self._confirm.setText('Export With Password' if credentials else 'Export')
        # An object name change is invisible until the widget is restyled.
        self._confirm.style().unpolish(self._confirm)
        self._confirm.style().polish(self._confirm)

    def scope(self) -> str:
        """The selected scope, falling back to the first one actually offered."""
        checked = self._group.checkedButton()
        if checked is not None:
            return self._scopes[checked]['key']
        if self._scopes:
            return next(iter(self._scopes.values()))['key']
        return account_export.SCOPE_ACCOUNT
