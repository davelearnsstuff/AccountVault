# -----------------------------------------------------------------------------
# ui/retirement_details.py
# The retirement planner's Your details page: the household, savings,
# contributions, spending and income, extra spending, Social Security, health
# costs, pensions, and market assumptions.
#
# Each change is saved as soon as it is committed, by Enter or by leaving the
# field. There is no Save button: there is one plan, and any figure is easy to
# put back. An entry that cannot be read is refused under its field and never
# replaces what is stored.
#
# Savings and contributions come from the accounts. A figure typed into one of
# those fields replaces the accounts' figure until it is cleared.
# -----------------------------------------------------------------------------

import copy
import math
import secrets

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from core import retirement
from core.categories import parse_money, trim_number
from services import retirement as retirement_service
from ui.theme import current_theme, palette
from ui.widgets import field_label, plain_label, plain_text

PAGE_MAX_WIDTH = 760

MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
          'August', 'September', 'October', 'November', 'December']

# The two keys a birth field writes, by field.
BIRTH_KEYS = {'birth': ('birth_year', 'birth_month'),
              'partner_birth': ('partner_birth_year', 'partner_birth_month')}

# (key, label, kind, hint), section by section. kind is how the field is
# entered: 'partner' (one or two people), 'birth' (month and year), 'age' (a
# whole number in a spin box), 'money', 'number', 'flag' (a checkbox),
# 'override' (a savings figure the accounts supply), or 'benefit' (a monthly
# benefit the Income records supply, typed over in the details). Fields fill a
# section two to a row.
SECTIONS = (
    ('Household', (
        ('partner', 'Planning for', 'partner', ''),
        ('plan_to', 'Plan to age', 'age', 'How long the money should last'),
        ('birth', 'Your birth month and year', 'birth', ''),
        ('partner_birth', "Your partner's birth month and year", 'birth', ''),
        ('retire_age', 'Retire at', 'age', ''),
        ('partner_retire_age', 'Partner retires at', 'age',
         'Your partner\'s own age. Blank means the year you retire.'),
        ('pay', 'Your pay per year', 'money',
         'Before tax. Counts once your partner retires, until you do.'),
        ('partner_pay', "Partner's pay per year", 'money',
         'Before tax. Counts once you retire, until your partner does.'),
    )),
    ('Savings today', (
        ('pretax', '401(k) / Traditional IRA', 'override', ''),
        ('roth', 'Roth', 'override', ''),
        ('taxable', 'Taxable brokerage and cash', 'override', ''),
        ('hsa', 'HSA', 'override', ''),
        ('basis_pct', 'Cost basis of taxable (%)', 'number',
         'What you paid in; the rest is gains'),
    )),
    # Yours, then your partner's: each stops when that person retires.
    ('Saving until you retire', (
        ('save_pretax', 'Into 401(k) / IRA per year', 'override', ''),
        ('save_roth', 'Into Roth per year', 'override', ''),
        ('save_taxable', 'Into taxable per year', 'override', ''),
        ('save_hsa', 'Into HSA per year', 'override', ''),
        ('partner_save_pretax', 'Partner into 401(k) / IRA per year', 'override', ''),
        ('partner_save_roth', 'Partner into Roth per year', 'override', ''),
        ('partner_save_taxable', 'Partner into taxable per year', 'override', ''),
        ('partner_save_hsa', 'Partner into HSA per year', 'override', ''),
    )),
    ('Spending and income in retirement', (
        ('spending', 'Yearly spending', 'money',
         "Today's dollars, before taxes. Leave out health costs."),
        None,
        ('part_time', 'Part-time income per year', 'money', ''),
        ('part_time_years', 'Part-time years after retiring', 'age', ''),
    )),
    # Yours down the left, your partner's down the right: the benefit at 67,
    # what it comes to at each claim age, and the age chosen.
    ('Social Security', (
        ('ss', 'Your Social Security at 67, monthly', 'benefit', 'From your statement at ssa.gov'),
        ('partner_ss', "Partner's Social Security at 67, monthly", 'benefit', ''),
        ('ss_table', '', 'claim_table', ''),
        ('partner_ss_table', '', 'claim_table', ''),
        ('ss_age', 'You claim at', 'age', ''),
        ('partner_ss_age', 'Partner claims at', 'age', ''),
    )),
    # Yours down the left, your partner's down the right. Each person's switch
    # from premiums to Medicare at their own 65.
    ('Health costs', (
        ('health', 'Your health premiums before 65, per year', 'money',
         'Added to spending until you reach 65'),
        ('partner_health', "Partner's health premiums before 65, per year", 'money',
         'Until your partner reaches 65'),
        ('health_65', 'Your health costs from 65, per year', 'money',
         'Medicare Part B, Part D and a supplement'),
        ('partner_health_65', "Partner's health costs from 65, per year", 'money', ''),
    )),
    ('Pensions', (
        ('pension', 'Your pension, monthly', 'benefit', 'From your pension statement'),
        ('partner_pension', "Partner's pension, monthly", 'benefit', ''),
        ('pension_age', 'Yours starts at', 'age', ''),
        ('partner_pension_age', "Partner's starts at", 'age', ''),
        ('pension_cola', 'Rises with inflation', 'flag',
         'Leave unticked if the payment stays the same. It then buys a little less '
         'each year.'),
        ('partner_pension_cola', 'Rises with inflation', 'flag', ''),
    )),
    ('Market assumptions', (
        ('return_mean', 'Average yearly return (%)', 'number', 'Before inflation'),
        ('volatility', 'Yearly ups and downs (%)', 'number', 'About 12 for a 60/40 mix'),
        ('inflation', 'Inflation (%)', 'number', ''),
    )),
)

# Shown only while the plan is for two.
PARTNER_ONLY = {'partner_birth', 'partner_retire_age', 'pay', 'partner_pay', 'partner_save_pretax',
                'partner_save_roth', 'partner_save_taxable', 'partner_save_hsa', 'partner_health', 'partner_health_65', 'partner_ss', 'partner_ss_age', 'partner_pension',
                'partner_pension_age', 'partner_pension_cola'}

# Ages that may be left blank, and what the field says while they are.
BLANK_AGES = {'partner_retire_age': 'Same year as you'}

# Each claim table, by the benefit and claim age it reads.
CLAIM_TABLES = {'ss_table': ('ss', 'ss_age'),
                'partner_ss_table': ('partner_ss', 'partner_ss_age')}

CLAIM_NOTE = ('Claiming before 67 pays less for life, and waiting past it pays more, up '
              'to 70. Crossover is the age by which a claim has paid as much in total '
              'as claiming at 67: live past it and the later claim comes out ahead. '
              'These are the rates for anyone born in 1960 or later. A Social '
              'Security record in Income fills these in; if it is ticked Receiving Now, '
              'set the age you claimed at.')

# What each savings bucket is called where the accounts table names it.
BUCKET_NAMES = {'pretax': 'Traditional', 'roth': 'Roth', 'taxable': 'Taxable', 'hsa': 'HSA'}

EXTRAS_NOTE = ("Trips, a car, a wedding: spending on top of your yearly spending. Each "
               "item is so much a year in today's dollars, starting so many years after "
               "you retire (0 is the year you retire). A purchase is 1 year.")

NOTE_TEXT = {
    retirement_service.ASSUMED: 'Assumed Traditional; set its tax treatment to change this',
    retirement_service.ESTIMATED: 'Estimated: its Traditional and Roth amounts no longer '
                                  'add up to its balance',
    retirement_service.NO_BALANCE: 'No balance entered',
    retirement_service.NO_PERIOD: 'Contribution not counted: no period set',
}


def dollars(value) -> str:
    """Whole dollars, as the planner shows every figure: they are estimates,
    and cents would claim otherwise."""
    return f"-${abs(value):,.0f}" if value < 0 else f"${value:,.0f}"


def _age_text(age) -> str:
    """An age in years and months, 78y 8m, or a dash for none."""
    if age is None:
        return '—'
    years, months = divmod(round(age * 12), 12)
    return f"{years}y {months}m" if months else f"{years}y"


def _number_text(value) -> str:
    return '' if value is None else str(trim_number(float(value)))


def _money_text(value) -> str:
    """An amount as it is typed back in: 84,000 or 1,250.5. parse_money reads
    the commas."""
    if value is None:
        return ''
    number = trim_number(float(value))
    return f"{number:,}" if isinstance(number, int) else f"{number:,.2f}"


class _Field:
    """One entry on the page: its block, its input, and what it writes."""

    def __init__(self, key, label, kind, block, widget, hint, error, default_hint):
        self.key, self.label, self.kind = key, label, kind
        self.block, self.widget, self.hint, self.error = block, widget, hint, error
        self.default_hint = default_hint


class DetailsPage(QWidget):
    """Emits `changed` with the whole plan after each committed edit; the
    caller stores it."""

    changed = Signal(dict)
    sample_requested = Signal()
    account_activated = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plan = None
        self._savings = None
        self._fields: dict[str, _Field] = {}
        self._claim_tables: dict[str, QTableWidget] = {}
        # Rows of the accounts table, for opening the one clicked.
        self._table_rows: list[dict] = []
        # The extra spending rows, in order: the item's id and its inputs.
        self._extra_rows: list[dict] = []
        self._loading = False

        intro = plain_label('Rough numbers are fine. Each change is saved as soon as you '
                            'press Enter or leave the field.')
        intro.setObjectName('Subtle')
        intro.setWordWrap(True)
        sample = QPushButton('View sample household')
        sample.setToolTip('See a worked example. Your own plan is kept as it is.')
        sample.clicked.connect(self.sample_requested.emit)

        header = QHBoxLayout()
        header.setSpacing(12)
        header.addWidget(intro, 1)
        header.addWidget(sample, 0, Qt.AlignTop)

        column = QWidget()
        column.setMaximumWidth(PAGE_MAX_WIDTH)
        self._column = QVBoxLayout(column)
        self._column.setContentsMargins(18, 16, 18, 16)
        self._column.setSpacing(14)
        self._column.addLayout(header)

        # A save that failed. A refused entry is reported under its own field.
        self._error = plain_label()
        self._error.setObjectName('ErrorLabel')
        self._error.setWordWrap(True)
        self._error.hide()
        self._column.addWidget(self._error)

        self._accounts = self._build_accounts_table()
        for title, fields in SECTIONS:
            card = self._build_section(title, fields)
            if title == 'Savings today':
                card.layout().addWidget(self._accounts_note)
                card.layout().addWidget(self._accounts)
            elif title == 'Social Security':
                note = plain_label(CLAIM_NOTE)
                note.setObjectName('FieldHint')
                note.setWordWrap(True)
                card.layout().addWidget(note)
            self._column.addWidget(card)
            if title == 'Spending and income in retirement':
                self._column.addWidget(self._build_extras_card())
        self._column.addStretch(1)

        holder = QWidget()
        outer = QHBoxLayout(holder)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(column, 1)
        outer.addStretch(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

    # -- building -----------------------------------------------------------

    def _build_section(self, title: str, fields) -> QFrame:
        card = QFrame()
        card.setObjectName('WarningBox')  # the bordered panel the dashboard cards use
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName('CardTitle')
        layout.addWidget(heading)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        for index, entry in enumerate(fields):
            if entry is None:  # an empty cell, to keep the next pair on one row
                continue
            key, label, kind, hint = entry
            if kind == 'claim_table':
                table = self._build_claim_table()
                self._claim_tables[key] = table
                grid.addWidget(table, index // 2, index % 2, Qt.AlignTop)
                continue
            field = self._build_field(key, label, kind, hint)
            self._fields[key] = field
            grid.addWidget(field.block, index // 2, index % 2, Qt.AlignTop)
        layout.addLayout(grid)
        return card

    def _build_field(self, key, label, kind, hint) -> _Field:
        block = QWidget()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        if kind != 'flag':  # a checkbox carries its own label
            layout.addWidget(field_label(label))

        widget = self._make_input(key, kind, label)
        layout.addWidget(widget)

        hint_label = plain_label(hint)
        hint_label.setObjectName('FieldHint')
        hint_label.setWordWrap(True)
        hint_label.setVisible(bool(hint))
        layout.addWidget(hint_label)

        error = plain_label()
        error.setObjectName('ErrorLabel')
        error.setWordWrap(True)
        error.hide()
        layout.addWidget(error)
        return _Field(key, label, kind, block, widget, hint_label, error, hint)

    def _make_input(self, key, kind, label) -> QWidget:
        if kind == 'partner':
            combo = QComboBox()
            combo.addItem('Just me', False)
            combo.addItem('Me + partner', True)
            combo.activated.connect(lambda _=0, k=key: self._commit(k))
            return combo

        if kind == 'birth':
            month = QComboBox()
            month.addItem('', None)
            for number, name in enumerate(MONTHS, start=1):
                month.addItem(name, number)
            month.activated.connect(lambda _=0, k=key: self._commit(k))
            year = QLineEdit()
            year.setPlaceholderText('Year')
            year.setMaxLength(4)
            year.editingFinished.connect(lambda k=key: self._commit(k))
            box = QWidget()
            row = QHBoxLayout(box)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            row.addWidget(month, 3)
            row.addWidget(year, 2)
            box.month, box.year = month, year
            return box

        if kind == 'age':
            spin = QSpinBox()
            low, high = retirement.DETAIL_RULES[key][1:]
            spin.setRange(low, high)
            if key in BLANK_AGES:
                # One below the lowest age stands for none entered.
                spin.setRange(low - 1, high)
                spin.setSpecialValueText(BLANK_AGES[key])
            # Only Enter, leaving the field, or the arrows commit; not every
            # digit on the way to one.
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(lambda _=0, k=key: self._commit(k))
            return spin

        if kind == 'flag':
            check = QCheckBox(label)
            # clicked, not toggled: only a click or the space bar commits, not
            # filling the page.
            check.clicked.connect(lambda _=False, k=key: self._commit(k))
            return check

        edit = QLineEdit()
        edit.editingFinished.connect(lambda k=key: self._commit(k))
        return edit

    def _build_extras_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName('WarningBox')
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        heading = QLabel('Extra spending in retirement')
        heading.setObjectName('CardTitle')
        layout.addWidget(heading)
        note = plain_label(EXTRAS_NOTE)
        note.setObjectName('FieldHint')
        note.setWordWrap(True)
        layout.addWidget(note)

        self._extras_grid = QGridLayout()
        self._extras_grid.setHorizontalSpacing(10)
        self._extras_grid.setVerticalSpacing(8)
        self._extras_grid.setColumnStretch(0, 3)
        self._extras_grid.setColumnStretch(1, 2)
        self._extras_headers = [field_label(text) for text in
                                ('Item', 'Per year', 'Years after retiring', 'For years')]
        for column, label in enumerate(self._extras_headers):
            self._extras_grid.addWidget(label, 0, column)
        layout.addLayout(self._extras_grid)

        self._extras_error = plain_label()
        self._extras_error.setObjectName('ErrorLabel')
        self._extras_error.setWordWrap(True)
        self._extras_error.hide()
        layout.addWidget(self._extras_error)

        self._add_extra_button = QPushButton('Add item')
        self._add_extra_button.clicked.connect(self._add_extra)
        layout.addWidget(self._add_extra_button, 0, Qt.AlignLeft)
        return card

    def _fill_extras(self, items: list):
        """One row an item, built afresh: items come and go."""
        for row in self._extra_rows:
            for widget in row['widgets']:
                # Off the page now; deleted once the click that got here is over.
                self._extras_grid.removeWidget(widget)
                widget.hide()
                widget.deleteLater()
        self._extra_rows = []
        for index, item in enumerate(items, start=1):
            name = QLineEdit(item['name'])
            name.setMaxLength(retirement.EXTRA_NAME_MAX)
            name.setAccessibleName('Extra spending item')
            name.editingFinished.connect(self._commit_extras)
            amount = QLineEdit(_money_text(item['amount']))
            amount.setAccessibleName(f"{item['name']}, per year")
            amount.editingFinished.connect(self._commit_extras)
            start, years = QSpinBox(), QSpinBox()
            for spin, key, text in ((start, 'start', 'years after retiring'),
                                    (years, 'years', 'for years')):
                spin.setRange(*retirement.EXTRA_RULES[key][1:])
                spin.setValue(item[key])
                spin.setKeyboardTracking(False)
                spin.setAccessibleName(f"{item['name']}, {text}")
                spin.valueChanged.connect(self._commit_extras)
            delete = QPushButton('Delete')
            delete.setObjectName('Small')
            delete.setAccessibleName(f"Delete {item['name']}")
            delete.clicked.connect(lambda _=False, i=item['id']: self._delete_extra(i))
            widgets = (name, amount, start, years, delete)
            for column, widget in enumerate(widgets):
                self._extras_grid.addWidget(widget, index, column)
            self._extra_rows.append({'id': item['id'], 'name': name, 'amount': amount,
                                     'start': start, 'years': years, 'widgets': widgets})
        for label in self._extras_headers:
            label.setVisible(bool(items))
        self._add_extra_button.setEnabled(len(items) < retirement.MAX_EXTRAS)
        self._add_extra_button.setToolTip(
            '' if len(items) < retirement.MAX_EXTRAS else
            f"There's room for {retirement.MAX_EXTRAS} items. Delete one to add another.")

    @staticmethod
    def _build_claim_table() -> QTableWidget:
        """Each claim age, its share of the benefit at 67, what that comes to
        a month, and the age at which it and claiming at 67 have paid the
        same. The shares are the planner's own, so the two can't disagree."""
        table = QTableWidget(len(retirement.SS_FACTORS), 4)
        table.setHorizontalHeaderLabels(['Claim at', 'Share', 'Monthly', 'Crossover'])
        table.horizontalHeaderItem(3).setToolTip(
            'The age by which this claim has paid as much in total as claiming at 67. '
            'Live past it and the later claim comes out ahead.')
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.setFocusPolicy(Qt.NoFocus)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for column in range(4):
            table.horizontalHeaderItem(column).setTextAlignment(
                Qt.AlignLeft | Qt.AlignVCenter if column == 0 else Qt.AlignRight | Qt.AlignVCenter)
        for row, (age, factor) in enumerate(retirement.SS_FACTORS.items()):
            share = QTableWidgetItem(f"{round(factor * 100, 2):g}%")
            share.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            amount = QTableWidgetItem()
            amount.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            crossover = QTableWidgetItem(_age_text(retirement.claim_crossover(age)))
            crossover.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            table.setItem(row, 0, QTableWidgetItem(str(age)))
            table.setItem(row, 1, share)
            table.setItem(row, 2, amount)
            table.setItem(row, 3, crossover)
        table.resizeRowsToContents()
        table.setFixedHeight(table.verticalHeader().length()
                             + table.horizontalHeader().height() + 2 * table.frameWidth())
        return table

    def _fill_claim_tables(self, details: dict):
        """Each claim age's monthly amount, the chosen age's row in bold. A
        dash until there is a benefit at 67, typed or from the Income records."""
        figures = (self._savings or {}).get('figures', {})
        for key, (amount_key, age_key) in CLAIM_TABLES.items():
            table = self._claim_tables[key]
            benefit = retirement.benefit(details, figures, amount_key)
            chosen = details.get(age_key, retirement.DETAIL_DEFAULTS[age_key])
            for row, (age, factor) in enumerate(retirement.SS_FACTORS.items()):
                table.item(row, 2).setText('—' if not benefit else dollars(benefit * factor))
                for column in range(4):
                    item = table.item(row, column)
                    font = item.font()
                    font.setBold(age == chosen)
                    item.setFont(font)

    def _build_accounts_table(self) -> QTableWidget:
        self._accounts_note = plain_label()
        self._accounts_note.setObjectName('Subtle')
        self._accounts_note.setWordWrap(True)

        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(['Account', 'Counted as', 'Balance', 'Note'])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.setFocusPolicy(Qt.NoFocus)
        table.setWordWrap(True)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        table.cellClicked.connect(self._open_row)
        return table

    # -- loading ------------------------------------------------------------

    def load(self, plan: dict, savings: dict):
        """Fills the page from a stored plan and the accounts' figures, without
        treating any of it as an edit."""
        self._plan = copy.deepcopy(plan)
        self._savings = savings
        self._loading = True
        try:
            details = self._plan['details']
            for key, field in self._fields.items():
                field.error.hide()
                self._fill(field, details)
            self._extras_error.hide()
            self._fill_extras(details.get('extras', []))
            self._refresh_hints()
            self._fill_accounts_table()
        finally:
            self._loading = False

    def adopt(self, plan: dict):
        """Takes the plan as stored, after a save from here or from another
        page, without refilling the fields. The next commit starts from it, so
        a setting changed elsewhere (Roth conversions) is not put back."""
        if self._plan is not None:
            self._plan = copy.deepcopy(plan)

    def show_error(self, message: str):
        """Reports a save that failed, or clears the report with ''."""
        self._error.setText(message)
        self._error.setVisible(bool(message))

    def refresh_colors(self):
        """Redraws the accounts table, whose names carry the theme's accent."""
        if self._savings is not None:
            self._fill_accounts_table()

    def clear(self):
        """Forgets the plan and empties every field: they hold vault content."""
        self._plan = None
        self._savings = None
        self._loading = True
        try:
            for field in self._fields.values():
                field.error.hide()
                self._fill(field, {})
            self._accounts.setRowCount(0)
            self._table_rows = []
            self._extras_error.hide()
            self._fill_extras([])
            self._fill_claim_tables({})
            self.show_error('')
        finally:
            self._loading = False

    def _fill(self, field: _Field, details: dict):
        widget, key = field.widget, field.key
        if field.kind == 'partner':
            widget.setCurrentIndex(1 if details.get('partner') else 0)
        elif field.kind == 'birth':
            year_key, month_key = BIRTH_KEYS[key]
            month = details.get(month_key)
            widget.month.setCurrentIndex(month if month else 0)
            year = details.get(year_key)
            widget.year.setText('' if year is None else str(year))
        elif field.kind == 'age':
            widget.blockSignals(True)
            widget.setValue(details.get(key, retirement.DETAIL_DEFAULTS.get(key,
                                                                           widget.minimum())))
            widget.blockSignals(False)
        elif field.kind == 'flag':
            widget.setChecked(bool(details.get(key, retirement.DETAIL_DEFAULTS.get(key))))
        elif field.kind == 'override':
            overrides = (self._plan or {}).get('overrides', {})
            widget.setText(_money_text(overrides.get(key)))
        else:
            text = _money_text if field.kind in ('money', 'benefit') else _number_text
            widget.setText(text(details.get(key)))
            default = retirement.DETAIL_DEFAULTS.get(key)
            if key in retirement.missing_details({}):
                widget.setPlaceholderText('Needed for your plan')
            elif default is not None:
                widget.setPlaceholderText(_number_text(default))

    def _refresh_hints(self):
        """The captions that depend on the plan: ages beside the birth dates,
        where each savings figure comes from, and which fields apply."""
        if self._plan is None:
            return
        details = self._plan['details']
        two = bool(details.get('partner'))
        for key in PARTNER_ONLY:
            self._fields[key].block.setVisible(two)
        self._claim_tables['partner_ss_table'].setVisible(two)
        self._fill_claim_tables(details)

        for key in BIRTH_KEYS:
            field = self._fields[key]
            year_key, month_key = BIRTH_KEYS[key]
            text = ''
            if details.get(year_key) is not None and details.get(month_key) is not None:
                try:
                    text = f"Age {retirement.age_on(details[year_key], details[month_key])}"
                except ValueError:
                    pass
            field.hint.setText(text)
            field.hint.setVisible(bool(text))

        pay = self._fields['pay']
        retiring = self._partner_retires_first(details)
        pay.hint.setText(pay.default_hint if not retiring else
                         f"{pay.default_hint} Your partner retires first, so the years "
                         f"you keep working count only with your pay entered.")

        figures = (self._savings or {}).get('figures', {})
        overrides = self._plan['overrides']
        for key in retirement.SAVINGS_KEYS:
            field = self._fields[key]
            figure = dollars(figures.get(key, 0))
            yearly = ' a year' if key.startswith('save_') else ''
            field.widget.setPlaceholderText(figure)
            if key in overrides:
                text = f"Your figure. Clear it to use your accounts' {figure}{yearly}."
            else:
                text = f"From your accounts: {figure}{yearly}. Type a figure to use it instead."
            field.hint.setText(text)
            field.hint.show()

        for key in retirement.BENEFIT_KEYS:
            field = self._fields[key]
            others = {name: value for name, value in details.items() if name != key}
            supplied = retirement.benefit(others, figures, key)
            figure = dollars(supplied)
            field.widget.setPlaceholderText(figure)
            if not supplied:
                text = field.default_hint
            elif key in details:
                text = f"Your figure. Clear it to use your Income records' {figure} a month."
            else:
                text = f"From your Income records: {figure} a month. Type a figure to use it instead."
            field.hint.setText(text)
            field.hint.setVisible(bool(text))

    @staticmethod
    def _partner_retires_first(details: dict) -> bool:
        """Whether a partner with an entered retirement age retires in an
        earlier year than you, by the birth dates as entered."""
        if not details.get('partner') or 'partner_retire_age' not in details:
            return False
        try:
            age = retirement.age_on(details['birth_year'], details['birth_month'])
            partner_age = retirement.age_on(details['partner_birth_year'],
                                            details['partner_birth_month'])
        except (KeyError, ValueError):
            return False
        retire_age = details.get('retire_age', retirement.DETAIL_DEFAULTS['retire_age'])
        return details['partner_retire_age'] - partner_age < retire_age - age

    def _fill_accounts_table(self):
        savings = self._savings or {'counted': [], 'skipped': []}
        rows = []
        for entry in savings['counted']:
            buckets = [bucket for bucket, value in entry['amounts'].items() if value] \
                or list(entry['amounts'])
            counted_as = ' + '.join(BUCKET_NAMES[bucket] for bucket in buckets)
            if entry['category'] == retirement_service.BANKING:
                counted_as = 'Cash'
            elif entry.get('partner'):
                counted_as += ", partner's"
            notes = '. '.join(NOTE_TEXT[note] for note in entry['notes'])
            rows.append((entry, counted_as, dollars(sum(entry['amounts'].values())), notes))
        for entry in savings['skipped']:
            reason = {retirement_service.NO_TYPE: 'No account type set',
                      retirement_service.OVERDRAWN: 'Overdrawn',
                      retirement_service.EXCLUDED: 'Exclude from Retirement is ticked'}.get(
                entry['reason'], f"Type is {entry['type']}")
            rows.append((entry, 'Not counted', '', reason))

        self._table_rows = [entry for entry, *_ in rows]
        accent = QBrush(QColor(palette(current_theme())['accent']))
        self._accounts.setRowCount(len(rows))
        for index, (entry, counted_as, amount, note) in enumerate(rows):
            # A table item never reads its text as HTML, so names are safe here.
            name = QTableWidgetItem(entry['name'])
            name.setForeground(accent)
            name.setToolTip(plain_text(f"Open {entry['name']}"))
            figure = QTableWidgetItem(amount)
            figure.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            for column, item in enumerate((name, QTableWidgetItem(counted_as), figure,
                                           QTableWidgetItem(note))):
                self._accounts.setItem(index, column, item)
        self._accounts.resizeRowsToContents()
        self._accounts.setVisible(bool(rows))
        self._accounts_note.setText(
            'Worked out from these accounts. Closed accounts are left out. Click one to '
            'open it.' if rows else
            'No open Investments or Banking accounts to work these out from.')
        height = self._accounts.verticalHeader().length() \
            + self._accounts.horizontalHeader().height() + 2 * self._accounts.frameWidth()
        self._accounts.setFixedHeight(height)

    def _open_row(self, row: int, _column: int):
        if 0 <= row < len(self._table_rows):
            entry = self._table_rows[row]
            self.account_activated.emit(entry['id'], entry['category'])

    # -- committing ---------------------------------------------------------

    def _commit(self, key: str):
        """Reads one field into a copy of the plan and emits it if anything
        changed. A refused entry shows why under the field and changes
        nothing stored."""
        if self._loading or self._plan is None:
            return
        field = self._fields[key]
        plan = copy.deepcopy(self._plan)
        try:
            self._write(field, plan)
        except ValueError as exc:
            field.error.setText(str(exc))
            field.error.show()
            return
        field.error.hide()
        if plan == self._plan:
            return
        self._plan = plan
        self._refresh_hints()
        self.changed.emit(copy.deepcopy(plan))

    def _store_extras(self, items: list):
        """Emits the plan with these extra spending items, if they changed it."""
        plan = copy.deepcopy(self._plan)
        _set(plan['details'], 'extras', items or None)
        if plan == self._plan:
            return
        self._plan = plan
        self.changed.emit(copy.deepcopy(plan))

    def _read_extras(self) -> list:
        """The items as the rows hold them, or ValueError naming the problem."""
        items = []
        for row in self._extra_rows:
            name = row['name'].text().strip()
            if not name:
                raise ValueError('Give each item a name, like Travel or New car.')
            text = row['amount'].text().strip()
            number = parse_money(text) if text else None
            if number is None:
                raise ValueError(f"{name}: enter a yearly amount, like 10,000.")
            items.append(retirement.check_extra({
                'id': row['id'], 'name': name,
                'amount': retirement.check_value(f"{name} per year", trim_number(number),
                                                 retirement.EXTRA_RULES['amount']),
                'start': row['start'].value(), 'years': row['years'].value()}))
        return items

    def _commit_extras(self, *_args):
        if self._loading or self._plan is None:
            return
        try:
            items = self._read_extras()
        except ValueError as exc:
            self._extras_error.setText(str(exc))
            self._extras_error.show()
            return
        self._extras_error.hide()
        self._store_extras(items)

    def _add_extra(self):
        """A new item, saved at once so it has a row to edit: named, at
        nothing a year, for the year you retire. Its name is selected to type
        over."""
        if self._plan is None:
            return
        items = list(self._plan['details'].get('extras', []))
        if len(items) >= retirement.MAX_EXTRAS:
            return
        items.append({'id': secrets.token_hex(6), 'name': 'New item',
                      'amount': 0, 'start': 0, 'years': 1})
        self._extras_error.hide()
        self._store_extras(items)
        self._loading = True
        try:
            self._fill_extras(items)
        finally:
            self._loading = False
        name = self._extra_rows[-1]['name']
        name.setFocus()
        name.selectAll()

    def _delete_extra(self, item_id: str):
        if self._plan is None:
            return
        items = [item for item in self._plan['details'].get('extras', [])
                 if item['id'] != item_id]
        self._extras_error.hide()
        self._store_extras(items)
        self._loading = True
        try:
            self._fill_extras(items)
        finally:
            self._loading = False

    def _write(self, field: _Field, plan: dict):
        """Puts what the field holds into plan, or ValueError naming the
        problem. A blank field removes its value."""
        details, key, widget = plan['details'], field.key, field.widget
        if field.kind == 'partner':
            details['partner'] = bool(widget.currentData())
            return
        if field.kind == 'birth':
            year_key, month_key = BIRTH_KEYS[key]
            text = widget.year.text().strip()
            month = widget.month.currentData()
            year = None
            if text:
                if not text.isdigit() or len(text) != 4:
                    raise ValueError('Enter the year as 4 digits, like 1978.')
                year = retirement.check_value('Birth year', int(text),
                                              retirement.DETAIL_RULES[year_key])
                # Refuses a date still to come; January when no month is set yet.
                retirement.age_on(year, month or 1)
            _set(details, year_key, year)
            _set(details, month_key, month)
            return
        if field.kind == 'age':
            if key in BLANK_AGES and widget.value() == widget.minimum():
                details.pop(key, None)
                return
            details[key] = retirement.check_value(field.label, widget.value(),
                                                  retirement.DETAIL_RULES[key])
            return
        if field.kind == 'flag':
            details[key] = widget.isChecked()
            return

        text = widget.text().strip()
        if field.kind == 'override':
            target, rule = plan['overrides'], retirement.INPUT_RULES[key]
            number = parse_money(text) if text else None
        elif field.kind in ('money', 'benefit'):
            target, rule = details, retirement.DETAIL_RULES[key]
            number = parse_money(text) if text else None
        else:
            target, rule = details, retirement.DETAIL_RULES[key]
            number = _to_number(text.rstrip('%').strip()) if text else None
        if text and number is None:
            raise ValueError(f"{field.label}: enter a number, like "
                             f"{'84,000' if field.kind != 'number' else '6.5'}.")
        _set(target, key, None if number is None
             else retirement.check_value(field.label, trim_number(number), rule))


def _set(target: dict, key: str, value):
    if value is None:
        target.pop(key, None)
    else:
        target[key] = value


def _to_number(text: str) -> float | None:
    """A finite number, or None: 'inf' and 'nan' are not figures anyone meant."""
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None
