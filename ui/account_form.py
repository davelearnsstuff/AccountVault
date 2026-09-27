# -----------------------------------------------------------------------------
# ui/account_form.py
# The add/edit form. Fields come from the category schema, so switching the
# category while adding rebuilds the field set.
# -----------------------------------------------------------------------------

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from categories import CATEGORY_CONFIG, get_config, parse_money, trim_number
from ui.widgets import (
    NO_DATE, GeneratorPanel, clear_layout, date_edit, field_label, reveal_toggle,
    to_qdate,
)

CUSTOM_CATEGORY = '__custom__'


def _is_masked(field: dict) -> bool:
    """Whether this field's input hides its value behind a Show toggle.

    One definition, because _build_field and _make_input both have to reach the
    same answer — they used to carry the expression separately, and a field the
    two disagreed about would get a toggle that toggled nothing, or an echo mode
    with no way to lift it.

    Deliberately narrower than the detail pane's, which masks anything marked
    sensitive. A sensitive *textarea* — a secure note's body — is a QTextEdit
    with no echo mode to set, and a note you cannot read is a note you cannot
    edit. The detail pane is where that value is masked; this is the form you
    opened in order to see it."""
    return field['type'] == 'password' or (
        field['type'] == 'text' and bool(field.get('sensitive')))


class AccountForm(QWidget):
    """Emits `saved` with a flat account dict; the caller writes it to the vault."""

    saved = Signal(dict)
    cancelled = Signal()

    def __init__(self, account: dict | None, category: str | None, parent=None):
        super().__init__(parent)
        self._account = account
        self._is_edit = account is not None
        self._inputs: dict[str, QWidget] = {}
        # The field definition each live widget was built from. Kept because a
        # rebuild reads the old widgets while self._category already names the
        # new schema, and _read_widget's answer depends on the field's type.
        self._input_fields: dict[str, dict] = {}
        self._dirty = False

        # When adding from the aggregate views there is no category yet, so the
        # user picks one and the field set follows.
        self._category = (
            (account.get('_category') or account.get('category')) if account else category
        )
        self._show_category_picker = self._is_edit or category is None
        if not self._category:
            self._category = next(iter(CATEGORY_CONFIG))

        self._title = QLabel()
        self._title.setObjectName('FormTitle')

        self._fields_layout = QVBoxLayout()
        self._fields_layout.setSpacing(12)

        self._closed = QCheckBox('Closed — account must be closed before it can be deleted')
        self._closed.setChecked(bool(account.get('closed')) if account else False)
        self._closed.checkStateChanged.connect(self._mark_dirty)

        save = QPushButton('Save')
        save.setObjectName('Primary')
        save.clicked.connect(self.submit)

        cancel = QPushButton('Cancel')
        cancel.clicked.connect(self._cancel)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(save)
        actions.addWidget(cancel)
        actions.addStretch(1)

        self._error = QLabel()
        self._error.setObjectName('ErrorLabel')
        self._error.setWordWrap(True)
        self._error.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)
        layout.addWidget(self._title)
        if self._show_category_picker:
            layout.addLayout(self._build_category_picker())
        layout.addLayout(self._fields_layout)
        layout.addWidget(self._closed)
        layout.addWidget(self._error)
        layout.addLayout(actions)
        layout.addStretch(1)

        self._rebuild_fields()

    # -- category picker ---------------------------------------------------

    def _build_category_picker(self) -> QVBoxLayout:
        self._category_combo = QComboBox()
        for key, config in CATEGORY_CONFIG.items():
            self._category_combo.addItem(config['title'], key)
        self._category_combo.addItem('Custom…', CUSTOM_CATEGORY)

        self._custom_category = QLineEdit()
        self._custom_category.setPlaceholderText('Enter category name…')

        is_custom = self._category not in CATEGORY_CONFIG
        if is_custom:
            self._category_combo.setCurrentIndex(self._category_combo.count() - 1)
            self._custom_category.setText(self._category)
        else:
            index = self._category_combo.findData(self._category)
            self._category_combo.setCurrentIndex(max(0, index))
        self._custom_category.setVisible(is_custom)

        self._category_combo.currentIndexChanged.connect(self._on_category_changed)
        self._custom_category.editingFinished.connect(self._on_custom_category_changed)

        layout = QVBoxLayout()
        layout.setSpacing(4)
        layout.addWidget(field_label('Category'))
        layout.addWidget(self._category_combo)
        layout.addWidget(self._custom_category)
        return layout

    def _on_category_changed(self):
        key = self._category_combo.currentData()
        self._mark_dirty()
        if key == CUSTOM_CATEGORY:
            self._custom_category.setVisible(True)
            self._custom_category.setFocus()
            return
        self._custom_category.setVisible(False)
        self._category = key
        self._rebuild_fields()

    def _on_custom_category_changed(self):
        name = self._custom_category.text().strip()
        if name and name != self._category:
            self._category = name
            self._rebuild_fields()

    def category(self) -> str:
        if not self._show_category_picker:
            return self._category
        if self._category_combo.currentData() == CUSTOM_CATEGORY:
            return self._custom_category.text().strip()
        return self._category_combo.currentData()

    # -- fields ------------------------------------------------------------

    def _current_input_values(self) -> dict:
        """What is typed into the form right now, keyed by field.

        Read before a rebuild so a category switch does not throw it away. The
        schemas overlap heavily — name, username, password, website and notes
        are in every one of them — so switching from Banking to Credit Cards
        used to silently empty five fields the user had already filled in, with
        the dirty flag set and nothing said.

        Read against the field definitions the widgets were actually built from,
        not against the schema now selected: by the time this runs self._category
        already names the new one, and _read_widget's answer depends on the
        field's type."""
        return {
            key: _read_widget(widget, self._input_fields[key])
            for key, widget in self._inputs.items()
            if key in self._input_fields
        }

    def _rebuild_fields(self):
        # Captured before clear_layout deletes the widgets it reads from.
        typed = self._current_input_values()

        clear_layout(self._fields_layout)
        self._inputs.clear()
        self._input_fields.clear()

        config = get_config(self._category)
        self._title.setText(
            f"Edit {config['item_label']}" if self._is_edit else f"Add {config['item_label']}"
        )

        first = None
        for field in config['fields']:
            if field.get('hide_in_form'):
                continue
            # What was typed wins over what was stored: the user has been
            # editing, and the rebuild is a category switch rather than a reload.
            if field['key'] in typed:
                value = typed[field['key']]
            else:
                value = self._account.get(field['key']) if self._account else ''
            widget = self._build_field(field, value)
            self._fields_layout.addWidget(widget)
            if first is None:
                first = self._inputs.get(field['key'])

        if first is not None:
            first.setFocus()

    def _build_field(self, field: dict, value) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        label = field['label'] + (' *' if field.get('required') else '')
        layout.addWidget(field_label(label))

        widget = self._make_input(field, value)
        self._inputs[field['key']] = widget
        self._input_fields[field['key']] = field

        # Password and masked text fields get a Show toggle; passwords also get
        # the generator.
        if _is_masked(field):
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(widget, 1)
            row.addWidget(self._show_toggle(widget, field['label']))
            if field['type'] == 'password':
                row.addWidget(self._generator_toggle(widget, layout))
            layout.addLayout(row)
        else:
            layout.addWidget(widget)

        return container

    def _make_input(self, field: dict, value) -> QWidget:
        kind = field['type']

        if kind == 'textarea':
            widget = QTextEdit()
            widget.setPlainText('' if value is None else str(value))
            widget.setMinimumHeight(90 if field.get('multiline') else 70)
            widget.textChanged.connect(self._mark_dirty)
            return widget

        if kind == 'select':
            widget = QComboBox()
            widget.addItem('', '')
            for option in field['options']:
                widget.addItem(option, option)
            # A schema default only ever seeds a *new* record. Applying it to an
            # existing one would rewrite what that record's stored data means
            # the moment its form was opened: a card filed years ago against a
            # single due date would come back monthly on the next save, without
            # anyone having decided that.
            if not value and not self._is_edit:
                value = field.get('default', value)
            index = widget.findData(value)
            widget.setCurrentIndex(index if index >= 0 else 0)
            widget.currentIndexChanged.connect(self._mark_dirty)
            return widget

        if kind == 'date':
            # The minimum doubles as the "no date" sentinel via no_date_text.
            widget = date_edit(to_qdate(value, default=NO_DATE),
                               minimum=NO_DATE, no_date_text=' ')
            widget.dateChanged.connect(self._mark_dirty)
            return widget

        widget = QLineEdit('' if value is None else str(value))
        if _is_masked(field):
            widget.setEchoMode(QLineEdit.Password)
        widget.textEdited.connect(self._mark_dirty)
        return widget

    @staticmethod
    def _show_toggle(widget: QLineEdit, label: str = '') -> QPushButton:
        """The Show/Hide button beside a masked input.

        widgets.reveal_toggle, shared with the detail pane — which is how this
        button acquired the auto-re-mask timer it never had. A password revealed
        here used to stay revealed until the form was closed, while the same
        value revealed one panel to the left re-masked itself after twenty
        seconds."""
        widget.setAccessibleName(label)
        return reveal_toggle(
            label,
            lambda shown: widget.setEchoMode(
                QLineEdit.Normal if shown else QLineEdit.Password),
            parent=widget,
        )

    def _generator_toggle(self, widget: QLineEdit, layout: QVBoxLayout) -> QPushButton:
        panel = GeneratorPanel()
        panel.hide()
        layout.addWidget(panel)

        button = QPushButton('Generate')
        button.setObjectName('Small')
        button.setCheckable(True)
        button.toggled.connect(panel.setVisible)
        # The button is captured by the closure that needs it. This used to be a
        # dict keyed on id(panel), which meant carrying a comment about recycled
        # ids and clearing the dict on every rebuild to keep a dead button from
        # being found under a live panel's address.
        panel.accepted.connect(
            lambda password: self._use_generated(widget, button, password))
        return button

    def _use_generated(self, widget: QLineEdit, button: QPushButton, password: str):
        widget.setText(password)
        self._mark_dirty()
        # Collapsing the panel also clears the preview it was holding — see
        # GeneratorPanel.hideEvent.
        button.setChecked(False)

    # -- state -------------------------------------------------------------

    def _mark_dirty(self, *_args):
        self._dirty = True

    def is_dirty(self) -> bool:
        return self._dirty

    def clear_dirty(self):
        self._dirty = False

    # Keys never carried forward from the stored record. 'id' is passed
    # separately, 'category' and 'closed' are read off this form, and anything
    # underscored is a view's own bookkeeping ('_category', '_match') that would
    # otherwise be written into extra_data as if it were a field of the account.
    _NOT_CARRIED = frozenset({'id', 'category', 'closed', 'date_added'})

    def values(self) -> dict:
        """Reads the form into a flat account dict.

        Starts from what the record already holds rather than from an empty
        dict. The form only builds inputs for the *current* category's schema,
        so a payload assembled from those inputs alone silently dropped every
        other key the record carried — and the save path takes the flat dict as
        the whole truth, rewriting extra_data from it. Opening a Websites
        account that came in from an import carrying card_number, or one moved
        out of Credit Cards, and pressing Save with no edits at all destroyed
        those fields.

        Every other path already preserves them: _decrypt_row merges them back
        onto the record, Quick Update starts from dict(account), the detail view
        offers them, and account_export writes them under their raw key on the
        grounds that a scope saying 'everything' has to mean it. This was the
        one place that disagreed."""
        payload = {}
        if self._account:
            payload = {
                key: value for key, value in self._account.items()
                if key not in self._NOT_CARRIED and not key.startswith('_')
            }

        payload['category'] = self.category()
        payload['closed'] = self._closed.isChecked()
        if self._account and self._account.get('id'):
            payload['id'] = self._account['id']

        # The form's own inputs overlay whatever was carried forward, so an
        # edited value wins and a cleared one reaches the save as None rather
        # than as the stored value coming back around.
        payload.update(self._current_input_values())
        return payload

    def submit(self):
        """Validates and emits `saved`. Public because the unsaved-changes prompt
        drives it from outside when the user picks Save."""
        self._error.hide()
        if not self.category():
            self.show_error('Please select or enter a category.')
            return
        values = self.values()
        if not str(values.get('name') or '').strip():
            self.show_error('Name is required.')
            return
        self.saved.emit(values)

    def _cancel(self):
        self.clear_dirty()
        self.cancelled.emit()

    def show_error(self, message: str):
        """Shows a validation or save failure under the fields.

        Public: submit() uses it for its own checks, and MainWindow uses it to
        report a write the service refused, so the two land in the same place."""
        self._error.setText(message)
        self._error.show()


def _read_widget(widget: QWidget, field: dict):
    kind = field['type']

    if isinstance(widget, QTextEdit):
        text = widget.toPlainText().strip()
        return text or None

    if isinstance(widget, QDateEdit):
        if widget.date() == widget.minimumDate():
            return None
        return widget.date().toString('yyyy-MM-dd')

    if isinstance(widget, QComboBox):
        return widget.currentData() or None

    text = widget.text().strip()
    if not text:
        return None
    if kind == 'number':
        # Money fields tolerate '$2,501.99'; a value we can't read is kept as
        # typed rather than silently dropped.
        number = parse_money(text) if field.get('money') else _to_float(text)
        if number is None:
            return text
        return trim_number(number)
    return text


def _to_float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None

