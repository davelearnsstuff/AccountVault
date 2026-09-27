# -----------------------------------------------------------------------------
# ui/auth_screens.py
# The three pre-unlock screens: choosing a vault, creating one, and unlocking it.
#
# The chooser is the newest of the three and the reason the other two changed.
# Once there can be more than one vault, "enter your master password" is an
# ambiguous request — so both of the others name the file they are talking
# about, and the login screen offers a way back here.
# -----------------------------------------------------------------------------

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QPushButton, QVBoxLayout, QWidget,
)

import auth
import database
import vaultfile
from ui.widgets import IconButton, ThemedIcon, password_field, password_requirements


class _AuthScreen(QWidget):
    """Centred card holding a logo, a title, and a form."""

    def __init__(self, subtitle: str, parent=None):
        super().__init__(parent)

        # The same padlock as the nav bar and the window icon.
        logo = QHBoxLayout()
        logo.addStretch(1)
        logo.addWidget(ThemedIcon('lock', role='accent', size=44))
        logo.addStretch(1)

        title = QLabel('AccountVault')
        title.setObjectName('AuthTitle')
        title.setAlignment(Qt.AlignCenter)

        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName('AuthSubtitle')
        self.subtitle.setAlignment(Qt.AlignCenter)
        self.subtitle.setWordWrap(True)

        # Which vault this screen is about. Hidden until there is something to
        # say, so a single-vault user never sees a line they do not need — and
        # present the moment there is more than one, because "enter your master
        # password" stops being a complete sentence as soon as the answer
        # depends on which file is open.
        self.vault_caption = QLabel()
        self.vault_caption.setObjectName('AuthVault')
        self.vault_caption.setAlignment(Qt.AlignCenter)
        self.vault_caption.setWordWrap(True)
        self.vault_caption.hide()

        self.error = QLabel()
        self.error.setObjectName('ErrorLabel')
        self.error.setAlignment(Qt.AlignCenter)
        self.error.setWordWrap(True)
        self.error.hide()

        self.form = QVBoxLayout()
        self.form.setSpacing(10)

        card = QFrame()
        card.setObjectName('AuthCard')
        card.setFixedWidth(400)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(34, 34, 34, 34)
        card_layout.setSpacing(12)
        card_layout.addLayout(logo)
        card_layout.addWidget(title)
        card_layout.addWidget(self.subtitle)
        card_layout.addWidget(self.vault_caption)
        card_layout.addSpacing(10)
        card_layout.addLayout(self.form)
        card_layout.addWidget(self.error)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card)
        row.addStretch(1)

        outer = QVBoxLayout(self)
        outer.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)

    def show_error(self, message: str):
        self.error.setText(message)
        self.error.show()

    def clear_error(self):
        self.error.clear()
        self.error.hide()

    def set_vault(self, path):
        """Names the vault this screen is about.

        The filename is shown and the full path is the tooltip: several vaults
        can share a basename, but the basename is what the user thinks of it as
        and the card is 400px wide."""
        if not path:
            self.vault_caption.hide()
            return
        path = Path(path)
        self.vault_caption.setText(path.name)
        self.vault_caption.setToolTip(str(path))
        self.vault_caption.show()

    @staticmethod
    def _password_field(placeholder: str) -> QLineEdit:
        """widgets.password_field at the height these screens use. The two auth
        screens and the four dialogs all build one; only the height differs."""
        return password_field(placeholder, min_height=36)


class SetupScreen(_AuthScreen):
    """First launch only — creates the master password."""

    submitted = Signal(str)

    def __init__(self, parent=None):
        super().__init__('Create your master password to get started', parent)

        self.password = self._password_field('Create master password')
        self.confirm = self._password_field('Confirm master password')

        self.requirements = password_requirements(self.password)

        create = QPushButton('Create Password')
        create.setObjectName('Primary')
        create.setMinimumHeight(38)
        create.clicked.connect(self._submit)

        self.form.addWidget(self.password)
        for requirement in self.requirements:
            self.form.addWidget(requirement)
        self.form.addWidget(self.confirm)
        self.form.addWidget(create)

        self.password.returnPressed.connect(self.confirm.setFocus)
        self.confirm.returnPressed.connect(self._submit)

    def _submit(self):
        self.clear_error()
        password = self.password.text()
        # Report what the validator actually returned. Restating a single rule
        # here means every rule added to it later fails with the wrong message,
        # which is exactly what happened when the space rule arrived.
        errors = auth.validate_password_strength(password)
        if errors:
            self.show_error('Master ' + ', '.join(errors) + '.')
            return
        if password.strip() != self.confirm.text().strip():
            self.show_error('Passwords do not match.')
            return
        self.submitted.emit(password)

    def reset(self):
        self.password.clear()
        self.confirm.clear()
        self.clear_error()
        self.password.setFocus()


class ChooserScreen(_AuthScreen):
    """Shown when no vault is open: at a first-ever launch, and whenever the
    recorded one has been moved, renamed or deleted.

    Never reached by falling back from a vault that would not open — the app
    says which vault and why, here, rather than quietly opening a different one.
    """

    open_requested = Signal()
    create_requested = Signal()
    recent_activated = Signal(str)
    forget_requested = Signal(str)
    clear_requested = Signal()
    remember_toggled = Signal(bool)
    quit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__('Open a vault to get started', parent)

        self.recent = QListWidget()
        self.recent.setObjectName('RecentVaults')
        self.recent.setMaximumHeight(190)
        self.recent.setContextMenuPolicy(Qt.CustomContextMenu)
        self.recent.customContextMenuRequested.connect(self._context_menu)
        self.recent.itemActivated.connect(self._activate)
        self.recent.itemClicked.connect(self._activate)
        self.recent.hide()

        self.recent_label = QLabel('Recent')
        self.recent_label.setObjectName('AuthSubtitle')
        self.recent_label.hide()

        open_button = QPushButton('Open Vault…')
        open_button.setObjectName('Primary')
        open_button.setMinimumHeight(38)
        open_button.clicked.connect(self.open_requested.emit)

        create_button = QPushButton('Create New Vault…')
        create_button.setMinimumHeight(38)
        create_button.clicked.connect(self.create_requested.emit)

        quit_button = IconButton('power', role='ghost', text='Quit', size=15)
        quit_button.setObjectName('Ghost')
        quit_button.setMinimumHeight(38)
        quit_button.clicked.connect(self.quit_requested.emit)

        self.form.addWidget(self.recent_label)
        self.form.addWidget(self.recent)
        self.form.addWidget(open_button)
        self.form.addWidget(create_button)
        self.form.addWidget(quit_button)

    def reset(self, notice: str = ''):
        """Redraws the recent list and shows why we are here, if there is a why.

        Every entry's status is read from disk on each pass rather than cached:
        a drive gets unplugged between one launch and the next, and a list that
        remembered last week's answer would offer a vault that is not there."""
        self.clear_error()
        if notice:
            self.show_error(notice)

        self.recent.clear()
        paths = vaultfile.recent()
        for path in paths:
            status = database.inspect_vault(path)
            item = QListWidgetItem(Path(path).name)
            item.setData(Qt.UserRole, str(path))
            if status == 'ok':
                item.setToolTip(str(path))
            else:
                # Shown greyed rather than dropped. A vault that silently
                # vanishes off this list reads as data that has been lost; one
                # that is listed as unavailable reads as a drive to plug in.
                item.setText(f"{Path(path).name}  —  {_SHORT_STATUS.get(status, 'unavailable')}")
                item.setToolTip(vaultfile.describe(status, path))
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self.recent.addItem(item)

        # With remembering off there are no paths, but the list still has to be
        # on screen: the switch that turns it back on lives in its context menu,
        # and a list that hid itself the moment it emptied would be a one-way
        # door. The placeholder is what the user right-clicks to get back.
        remembering = vaultfile.remembering()
        if not remembering:
            item = QListWidgetItem('Not remembering recent vaults')
            item.setToolTip('Right-click to start remembering them again.')
            item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self.recent.addItem(item)

        showing = bool(paths) or not remembering
        self.recent.setVisible(showing)
        self.recent_label.setVisible(showing)

    def _activate(self, item: QListWidgetItem):
        if item and item.flags() & Qt.ItemIsEnabled:
            self.recent_activated.emit(item.data(Qt.UserRole))

    def _context_menu(self, point):
        """Remove-from-list, a way to empty it, and a way to stop keeping it.

        Only ever touches the list. Nothing on this screen deletes a vault
        file — forgetting where one is and destroying it are not two clicks
        that belong next to each other.

        Turning remembering off also empties the list, which is the point of
        the switch rather than a side effect: leaving the paths on disk and
        merely declining to show them would keep the file that names them."""
        self._build_context_menu(point).exec(self.recent.mapToGlobal(point))

    def _build_context_menu(self, point) -> QMenu:
        """The menu itself, wired but not shown.

        Separate from _context_menu because exec() is a modal event loop: the
        tests can ask what this screen offers, and in which state, without one
        of them opening a menu that never closes."""
        menu = QMenu(self)
        item = self.recent.itemAt(point)
        if item is not None and item.data(Qt.UserRole):
            path = item.data(Qt.UserRole)
            remove = menu.addAction('Remove from list')
            remove.triggered.connect(lambda: self.forget_requested.emit(path))

        remembering = vaultfile.remembering()
        if remembering:
            clear = menu.addAction('Clear recent vaults')
            clear.triggered.connect(self.clear_requested.emit)

        remember = menu.addAction('Remember recent vaults')
        remember.setCheckable(True)
        remember.setChecked(remembering)
        remember.triggered.connect(self.remember_toggled.emit)
        return menu


# The listed form of a status, for an entry that is 400px wide and already
# carries the full sentence as its tooltip.
_SHORT_STATUS = {
    'missing': 'not found',
    'unavailable': 'drive not available',
    'empty': 'empty file',
    'not-sqlite': 'not a vault',
    'not-vault': 'not a vault',
    'unreadable': 'unreadable',
}


class LoginScreen(_AuthScreen):
    """Shown on every later launch and whenever the vault locks."""

    submitted = Signal(str)
    switch_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__('Enter your master password to unlock', parent)

        self.password = self._password_field('Master password')

        unlock = QPushButton('Unlock')
        unlock.setObjectName('Primary')
        unlock.setMinimumHeight(38)
        unlock.clicked.connect(self._submit)

        switch = QPushButton('Open a different vault…')
        switch.setObjectName('Ghost')
        switch.setMinimumHeight(38)
        switch.clicked.connect(self.switch_requested.emit)

        quit_button = IconButton('power', role='ghost', text='Quit', size=15)
        quit_button.setObjectName('Ghost')
        quit_button.setMinimumHeight(38)
        quit_button.clicked.connect(self.quit_requested.emit)

        self.form.addWidget(self.password)
        self.form.addWidget(unlock)
        self.form.addWidget(switch)
        self.form.addWidget(quit_button)

        self.password.returnPressed.connect(self._submit)

    def _submit(self):
        self.clear_error()
        if not self.password.text().strip():
            self.show_error('Enter your master password.')
            return
        self.submitted.emit(self.password.text())

    def reset(self):
        self.password.clear()
        self.clear_error()
        self.password.setFocus()
