# -----------------------------------------------------------------------------
# ui/account_form.py
# The add/edit form. Fields come from the category schema, so switching the
# category while adding rebuilds the field set.
# -----------------------------------------------------------------------------

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QGridLayout, QHBoxLayout, QLineEdit,
    QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from core.categories import CATEGORY_CONFIG, get_config, parse_money, trim_number
from services.accounts import MAX_LENGTHS
from ui.widgets import (
    NO_DATE, GeneratorPanel, clear_layout, date_edit, field_label, plain_label,
    reveal_toggle, to_qdate,
)

CUSTOM_CATEGORY = '__custom__'

# The form stops widening past this, so a maximised window does not stretch a
# name field across the whole screen.
FORM_MAX_WIDTH = 760

# Hints for what an empty input expects, by field type. Money and percent are
# number fields, so they are looked up by flag first.
_PLACEHOLDERS = {
    'money':   '0.00',
    'percent': 'e.g. 19.99',
    'url':     'https://',
    'email':   'name@example.com',
}


def _placeholder(field: dict) -> str:
    for flag in ('money', 'percent'):
        if field.get(flag):
            return _PLACEHOLDERS[flag]
    return _PLACEHOLDERS.get(field['type'], '')


def _is_masked(field: dict) -> bool:
    """Whether this field's input hides its value behind a Show toggle.

    Narrower than the detail pane's rule, which masks anything sensitive: a
    sensitive textarea (a secure note's body) has no echo mode to set, and this
    is the form you open in order to read and edit it."""
    return field['type'] == 'password' or (
        field['type'] == 'text' and bool(field.get('sensitive')))


def _is_full_width(field: dict) -> bool:
    """Whether a field takes both grid columns: a textarea, and a password,
    whose generator panel needs the room when it opens."""
    return field['type'] in ('textarea', 'password')


class AccountForm(QWidget):
    """Emits `saved` with a flat account dict; the caller writes it to the vault."""

    saved = Signal(dict)
    cancelled = Signal()

    def __init__(self, account: dict | None, category: str | None, parent=None):
        super().__init__(parent)
        self._account = account
        self._is_edit = account is not None
        self._inputs: dict[str, QWidget] = {}
        # The field definition each live widget was built from. A rebuild reads
        # the old widgets after self._category already names the new schema.
        self._input_fields: dict[str, dict] = {}
        self._dirty = False

        # Adding from an aggregate view has no category yet, so the user picks
        # one and the field set follows.
        self._category = (
            (account.get('_category') or account.get('category')) if account else category
        )
        self._show_category_picker = self._is_edit or category is None
        if not self._category:
            self._category = next(iter(CATEGORY_CONFIG))

        # A custom category's name is vault content.
        self._title = plain_label()
        self._title.setObjectName('FormTitle')

        # Two columns, so a dozen short fields read as six rows rather than a
        # dozen. Equal stretch keeps the columns lined up across rows.
        self._fields_layout = QGridLayout()
        self._fields_layout.setHorizontalSpacing(16)
        self._fields_layout.setVerticalSpacing(12)
        self._fields_layout.setColumnStretch(0, 1)
        self._fields_layout.setColumnStretch(1, 1)

        self._closed = QCheckBox('Closed — account must be closed before it can be deleted')
        self._closed.setChecked(bool(account.get('closed')) if account else False)
        self._closed.checkStateChanged.connect(self._mark_dirty)

        # Ctrl+S from any input in the form. Scoped to the form so it never
        # competes with a window-wide binding.
        shortcut = QShortcut(QKeySequence.Save, self)
        shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        shortcut.activated.connect(self.submit)

        # Save and Cancel both beside the title and under the last field, so
        # neither end of a long form needs a scroll to reach them.
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(self._title, 1)
        self._add_actions(header)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addStretch(1)
        self._add_actions(footer)

        # One beside each pair of buttons, so a failed save shows next to the
        # Save that was clicked, whichever end of the form is in view.
        self._errors = [self._error_label(), self._error_label()]

        column = QWidget()
        column.setMaximumWidth(FORM_MAX_WIDTH)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)
        layout.addLayout(header)
        layout.addWidget(self._errors[0])
        if self._show_category_picker:
            layout.addLayout(self._build_category_picker())
        layout.addLayout(self._fields_layout)
        layout.addWidget(self._closed)
        layout.addWidget(self._errors[1])
        layout.addLayout(footer)
        layout.addStretch(1)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(column, 1)
        outer.addStretch(0)

        self._rebuild_fields()

    def _add_actions(self, row: QHBoxLayout):
        save = QPushButton('Save')
        save.setObjectName('Primary')
        save.setToolTip('Save (Ctrl+S)')
        save.clicked.connect(self.submit)

        cancel = QPushButton('Cancel')
        cancel.clicked.connect(self._cancel)

        row.addWidget(save)
        row.addWidget(cancel)

    @staticmethod
    def _error_label():
        label = plain_label()
        label.setObjectName('ErrorLabel')
        label.setWordWrap(True)
        label.hide()
        return label

    # -- category picker ---------------------------------------------------

    def _build_category_picker(self) -> QGridLayout:
        self._category_combo = QComboBox()
        for key, config in CATEGORY_CONFIG.items():
            self._category_combo.addItem(config['title'], key)
        self._category_combo.addItem('Custom…', CUSTOM_CATEGORY)

        self._custom_category = QLineEdit()
        self._custom_category.setPlaceholderText('Enter category name…')
        self._custom_category.setMaxLength(MAX_LENGTHS['category'])

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

        # The custom name takes the second column, so the picker lines up with
        # the field grid below it.
        layout = QGridLayout()
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(4)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        layout.addWidget(field_label('Category'), 0, 0)
        layout.addWidget(self._category_combo, 1, 0)
        layout.addWidget(self._custom_category, 1, 1)
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
        """What is typed into the form right now, keyed by field — read before a
        rebuild so a category switch keeps the fields the schemas share. Read
        against the field definitions the widgets were built from."""
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
        row = col = 0
        for field in config['fields']:
            if field.get('hide_in_form'):
                continue
            # What was typed wins over what was stored.
            if field['key'] in typed:
                value = typed[field['key']]
            else:
                value = self._account.get(field['key']) if self._account else ''
            widget = self._build_field(field, value)

            # Row-major, so Tab order (creation order) reads across then down.
            if _is_full_width(field):
                if col:
                    row, col = row + 1, 0
                self._fields_layout.addWidget(widget, row, 0, 1, 2)
                row += 1
            else:
                # Top-aligned: a masked field's Show button makes its cell
                # taller than a plain neighbour's.
                self._fields_layout.addWidget(widget, row, col, Qt.AlignTop)
                col += 1
                if col == 2:
                    row, col = row + 1, 0

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

        # Masked fields get a Show toggle; passwords also get the generator.
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
            # No length cap on a QTextEdit; an over-long value is refused on
            # save with an error naming the field.
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
            # A schema default only seeds a *new* record; applied to an existing
            # one it would change what the stored data means.
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
        widget.setPlaceholderText(_placeholder(field))
        if field['key'] in MAX_LENGTHS:
            widget.setMaxLength(MAX_LENGTHS[field['key']])
        if _is_masked(field):
            widget.setEchoMode(QLineEdit.Password)
        widget.textEdited.connect(self._mark_dirty)
        return widget

    @staticmethod
    def _show_toggle(widget: QLineEdit, label: str = '') -> QPushButton:
        """The Show/Hide button beside a masked input — widgets.reveal_toggle,
        with the same auto-re-mask the detail pane has."""
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
        panel.accepted.connect(
            lambda password: self._use_generated(widget, button, password))
        return button

    def _use_generated(self, widget: QLineEdit, button: QPushButton, password: str):
        widget.setText(password)
        self._mark_dirty()
        # Collapsing the panel also clears its preview.
        button.setChecked(False)

    # -- state -------------------------------------------------------------

    def _mark_dirty(self, *_args):
        self._dirty = True

    def is_dirty(self) -> bool:
        return self._dirty

    def clear_dirty(self):
        self._dirty = False

    # Keys never carried forward from the stored record: 'id' is passed
    # separately, and 'category' and 'closed' are read off this form.
    _NOT_CARRIED = frozenset({'id', 'category', 'closed', 'date_added'})

    def values(self) -> dict:
        """Reads the form into a flat account dict.

        Starts from what the record already holds, not from an empty dict: the
        form only has inputs for the current schema, and the save rewrites
        extra_data from this dict, so keys outside the schema (a card number on
        a record moved out of Credit Cards) would otherwise be destroyed by a
        save with no edits. Underscored view bookkeeping is never carried."""
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

        # The inputs overlay what was carried, so a cleared field reaches the
        # save as None rather than as the stored value.
        payload.update(self._current_input_values())
        return payload

    def submit(self):
        """Validates and emits `saved`. Public because the unsaved-changes prompt
        drives it when the user picks Save."""
        for label in self._errors:
            label.hide()
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
        """Shows a validation or save failure beside both sets of buttons."""
        for label in self._errors:
            label.setText(message)
            label.show()


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

    raw = widget.text()
    if kind == 'password':
        # Exactly as typed: a leading or trailing space is part of a password,
        # and trimming it would store one that does not log in.
        return raw if raw.strip() else None

    text = raw.strip()
    if not text:
        return None
    if kind == 'number':
        # Money fields tolerate '$2,501.99'. A value that is not a finite number
        # is kept as typed rather than dropped.
        number = parse_money(text) if field.get('money') else _to_float(text)
        if number is None:
            return text
        return trim_number(number)
    return text


def _to_float(text: str) -> float | None:
    """A finite number, or None — 'inf' and 'nan' are not amounts anyone typed
    on purpose."""
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None
