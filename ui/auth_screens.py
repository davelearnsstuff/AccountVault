# -----------------------------------------------------------------------------
# ui/auth_screens.py
# The three pre-unlock screens: choosing a vault, creating one, and unlocking
# it. With more than one vault possible, the setup and login screens name the
# file they are about, and the login screen offers a way back to the chooser.
# -----------------------------------------------------------------------------

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QPushButton, QVBoxLayout, QWidget,
)

from core import auth
from core import vaultfile
from ui.widgets import (
    IconButton, ThemedIcon, password_field, password_requirements, plain_label,
)


class _AuthScreen(QWidget):
    """Centred card holding a logo, a title, and a form."""

    def __init__(self, subtitle: str, parent=None):
        super().__init__(parent)

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

        # Which vault this screen is about, hidden until there is one to name.
        self.vault_caption = plain_label()
        self.vault_caption.setObjectName('AuthVault')
        self.vault_caption.setAlignment(Qt.AlignCenter)
        self.vault_caption.setWordWrap(True)
        self.vault_caption.hide()

        # Errors quote file names and exception text.
        self.error = plain_label()
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
        """Names the vault this screen is about: the filename, with the full
        path as the tooltip."""
        if not path:
            self.vault_caption.hide()
            return
        path = Path(path)
        self.vault_caption.setText(path.name)
        self.vault_caption.setToolTip(str(path))
        self.vault_caption.show()

    @staticmethod
    def _password_field(placeholder: str) -> QLineEdit:
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
    """Shown when no vault is open: at a first launch, and whenever the recorded
    one has moved. It says which vault and why, rather than quietly opening a
    different one."""

    open_requested = Signal()
    create_requested = Signal()
    demo_requested = Signal()
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

        # The same request as the context menu's Clear recent vaults, out where
        # it can be found. Forgets paths only; no vault file is touched.
        self.clear_button = QPushButton('Clear')
        self.clear_button.setObjectName('Small')
        self.clear_button.setAccessibleName('Clear recent vaults')
        self.clear_button.setToolTip('Forget the vaults listed here. The files '
                                     'themselves are left where they are.')
        self.clear_button.clicked.connect(self.clear_requested.emit)
        self.clear_button.hide()

        recent_header = QHBoxLayout()
        recent_header.addWidget(self.recent_label)
        recent_header.addStretch(1)
        recent_header.addWidget(self.clear_button)

        open_button = QPushButton('Open Vault…')
        open_button.setObjectName('Primary')
        open_button.setMinimumHeight(38)
        open_button.clicked.connect(self.open_requested.emit)

        create_button = QPushButton('Create New Vault…')
        create_button.setMinimumHeight(38)
        create_button.clicked.connect(self.create_requested.emit)

        # Ghost, under the two real choices: a way to look around, not a third
        # kind of vault.
        demo_button = QPushButton('Create Demo Vault…')
        demo_button.setObjectName('Ghost')
        demo_button.setMinimumHeight(38)
        demo_button.setToolTip('A new vault full of made-up accounts, to try the app with.')
        demo_button.clicked.connect(self.demo_requested.emit)

        quit_button = IconButton('power', role='ghost', text='Quit', size=15)
        quit_button.setObjectName('Ghost')
        quit_button.setMinimumHeight(38)
        quit_button.clicked.connect(self.quit_requested.emit)

        self.form.addLayout(recent_header)
        self.form.addWidget(self.recent)
        self.form.addWidget(open_button)
        self.form.addWidget(create_button)
        self.form.addWidget(demo_button)
        self.form.addWidget(quit_button)

    def reset(self, notice: str = ''):
        """Redraws the recent list and shows why we are here, if there is a why.

        Every entry's status is read from disk on each pass — a drive gets
        unplugged between launches — concurrently and with a time limit, so a
        dead network share cannot freeze the screen."""
        self.clear_error()
        if notice:
            self.show_error(notice)

        self.recent.clear()
        paths = vaultfile.recent()
        for path, status in zip(paths, vaultfile.probe_all(paths)):
            item = QListWidgetItem(Path(path).name)
            item.setData(Qt.UserRole, str(path))
            if status == 'ok':
                item.setToolTip(str(path))
            else:
                # Listed greyed rather than dropped: a vault that vanishes off
                # the list reads as lost, one marked unavailable as a drive to
                # plug in.
                item.setText(f"{Path(path).name}  —  {_SHORT_STATUS.get(status, 'unavailable')}")
                item.setToolTip(vaultfile.describe(status, path))
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self.recent.addItem(item)

        # With remembering off there are no paths, but the list stays on screen:
        # the switch to turn it back on is in its context menu.
        remembering = vaultfile.remembering()
        if not remembering:
            item = QListWidgetItem('Not remembering recent vaults')
            item.setToolTip('Right-click to start remembering them again.')
            item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self.recent.addItem(item)

        showing = bool(paths) or not remembering
        self.recent.setVisible(showing)
        self.recent_label.setVisible(showing)
        self.clear_button.setVisible(bool(paths))

    def _activate(self, item: QListWidgetItem):
        if item and item.flags() & Qt.ItemIsEnabled:
            self.recent_activated.emit(item.data(Qt.UserRole))

    def _context_menu(self, point):
        """Remove-from-list, empty the list, stop keeping it. Nothing here
        touches a vault file."""
        self._build_context_menu(point).exec(self.recent.mapToGlobal(point))

    def _build_context_menu(self, point) -> QMenu:
        """The menu itself, wired but not shown, so tests can inspect it without
        a modal event loop."""
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


# A status in a few words, for an entry that carries the full sentence as its
# tooltip.
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
