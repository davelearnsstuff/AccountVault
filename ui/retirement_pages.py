# -----------------------------------------------------------------------------
# ui/retirement_pages.py
# The Retirement tab's result pages: Plan, Projection, and Withdrawals & taxes.
#
# Each draws one PlanResult, whichever household it is for, and is rebuilt
# from scratch when the result changes. Your details is in
# retirement_details.py; the tab that holds them all is retirement_view.py.
# -----------------------------------------------------------------------------

from typing import NamedTuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core import retirement
from ui.retirement_charts import BandChart, MixBar, OddsRing, StackedBars, Swatch
from ui.retirement_details import dollars
from ui.theme import RETIREMENT_SERIES
from ui.widgets import clear_layout, field_label, plain_label

# Each verdict's words and the object name that colours them.
VERDICTS = {
    'good': ('On track', 'VerdictGood'),
    'warn': ('Needs a tweak', 'VerdictWarn'),
    'bad':  ('At risk', 'VerdictBad'),
}

FOOTNOTE = (
    "Educational estimates, not financial, tax, or legal advice. Only federal income "
    "tax is modeled, using approximate 2026 brackets held steady in today's dollars. "
    "Social Security, pensions, required distributions, and market returns are simplified. "
    "State taxes, Medicare surcharges, and ACA subsidies aren't included."
)

# The Roth conversion brackets, as the Withdrawals page offers them.
BRACKETS = [(rate, f"{round(rate * 100)}% bracket") for rate in retirement.CONVERSION_RATES]


class PlanResult(NamedTuple):
    """Everything the result pages draw, worked out once per change."""
    inputs: dict
    simulation: retirement.Simulation
    path: retirement.Path              # steady returns, conversions as set
    without: retirement.Path           # steady returns, no conversions
    with_conversions: retirement.Path  # steady returns, conversions on
    sample: bool                       # the sample household, not the user's plan


def compute(inputs: dict, sample: bool) -> PlanResult:
    without = retirement.steady({**inputs, 'convert': False})
    with_conversions = retirement.steady({**inputs, 'convert': True})
    return PlanResult(inputs, retirement.simulate(inputs),
                      with_conversions if inputs['convert'] else without,
                      without, with_conversions, sample)


# -- building blocks -----------------------------------------------------------

def card() -> QFrame:
    frame = QFrame()
    frame.setObjectName('WarningBox')  # the bordered panel the dashboard cards use
    return frame


def subtle(text: str, wrap: bool = True) -> QLabel:
    label = plain_label(text)
    label.setObjectName('Subtle')
    label.setWordWrap(wrap)
    return label


def card_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName('CardTitle')
    return label


def legend(items) -> QHBoxLayout:
    """A row of (swatch, words). Words in the text colour: the swatch beside
    them carries the series."""
    row = QHBoxLayout()
    row.setSpacing(6)
    for swatch, text in items:
        row.addWidget(swatch)
        row.addWidget(subtle(text, wrap=False))
        row.addSpacing(14)
    row.addStretch(1)
    return row


class _FigureTable(QTableWidget):
    """Each column as wide as what it holds, with the room left over shared
    out. Columns of one width clipped the headers once the withdrawals table
    gained a pension column. A table too wide for its space scrolls sideways
    rather than cutting a figure short."""

    def __init__(self, rows: int, columns: int):
        super().__init__(rows, columns)
        self.body_height = 0

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_columns()

    def fit_columns(self):
        header = self.horizontalHeader()
        needs = [max(self.sizeHintForColumn(column), header.sectionSizeHint(column))
                 for column in range(self.columnCount())]
        room = self.viewport().width() - sum(needs)
        if room >= 0:
            widths = [need + room // len(needs) for need in needs]
            widths[-1] += self.viewport().width() - sum(widths)
        else:
            widths = needs
        for column, width in enumerate(widths):
            header.resizeSection(column, width)
        scrollbar = self.horizontalScrollBar().sizeHint().height() if room < 0 else 0
        self.setFixedHeight(self.body_height + scrollbar)


def figure_table(headers: list, rows: list, bold: set = frozenset()) -> QTableWidget:
    """A read-only table sized to its rows, the first column text and the rest
    figures, right-aligned so the digits line up. `bold` names rows to stand
    out. A table item never reads its text as HTML."""
    table = _FigureTable(len(rows), len(headers))
    table.setHorizontalHeaderLabels(headers)
    for column in range(len(headers)):
        table.horizontalHeaderItem(column).setTextAlignment(
            Qt.AlignLeft | Qt.AlignVCenter if column == 0 else Qt.AlignRight | Qt.AlignVCenter)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.setSelectionMode(QTableWidget.NoSelection)
    table.setFocusPolicy(Qt.NoFocus)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Fixed)
    # Figures never wrap: a column is as wide as its widest.
    table.setWordWrap(False)
    for row, cells in enumerate(rows):
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if column:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if row in bold:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            table.setItem(row, column, item)
    table.resizeRowsToContents()
    table.body_height = (table.verticalHeader().length()
                         + table.horizontalHeader().height() + 2 * table.frameWidth())
    table.setFixedHeight(table.body_height)
    return table


def _or_dash(value: float) -> str:
    return dollars(value) if value > 0.5 else '—'


def pension_text(inputs: dict) -> str:
    """The household's pensions in a line, or '' when it has none."""
    parts = []
    for prefix, who in (('', ''), ('partner_', 'partner ')):
        amount = inputs[prefix + 'pension']
        if not amount or (prefix and not inputs['partner']):
            continue
        rises = 'rises with inflation' if inputs[prefix + 'pension_cola'] else 'fixed'
        per = '' if parts else ' a month'
        parts.append(f"{who}{dollars(amount)}{per} at {inputs[prefix + 'pension_age']} "
                     f"({rises})")
    text = ', '.join(parts)
    return text[:1].upper() + text[1:]


def pension_starts(inputs: dict) -> list:
    """(your age, label) for each pension that starts after retiring. One
    that starts sooner is paid from retiring, which has its own marker."""
    starts = []
    if inputs['pension']:
        starts.append((inputs['pension_age'], 'Pension'))
    if inputs['partner'] and inputs['partner_pension']:
        starts.append((inputs['partner_pension_age'] + inputs['age'] - inputs['partner_age'],
                       "Partner's pension"))
    return [(age, label) for age, label in starts if age > inputs['retire_age']]


def partner_retires(inputs: dict) -> list:
    """(your age, label) for a partner who retires in a different year from
    you: the plan changes then, as their pay and contributions stop."""
    if not inputs['partner']:
        return []
    age = partner_retires_at(inputs)
    return [(age, 'Partner retires')] if age != inputs['retire_age'] else []


def partner_retires_at(inputs: dict) -> int:
    """Your age the year your partner retires."""
    return inputs['partner_retire_age'] + inputs['age'] - inputs['partner_age']


def pay_note(inputs: dict) -> str:
    """Why the years you work on count for nothing, when your partner retires
    first and no pay of yours is entered; '' otherwise."""
    if (inputs['partner'] and not inputs['pay']
            and partner_retires_at(inputs) < inputs['retire_age'] > inputs['age']):
        return ("Your partner retires first. Enter your pay in Your details to count "
                "the years you keep working.")
    return ''


def extras_note(inputs: dict) -> str:
    """'Plus $140,000 extra over the plan, 3 items', or '' for none. Years
    past the plan's end are not counted."""
    count = len(inputs.get('extras', ()))
    total = sum(retirement.extra_spending(inputs))
    if not count:
        return ''
    return f"Plus {dollars(total)} extra over the plan, {count} item{'s' if count != 1 else ''}"


def invested_note(savings: dict | None) -> str:
    """Why Invested today is not the dashboard's Investments figure: it
    counts bank cash too, and leaves out accounts the plan cannot use. '' for
    the sample household, or when neither applies."""
    if not savings:
        return ''
    cash = sum(sum(row['amounts'].values()) for row in savings['counted']
               if row['category'] == 'Banking')
    left_out = savings['skipped']
    parts = []
    if cash > 0.5:
        parts.append(f"Includes {dollars(cash)} bank cash.")
    if left_out:
        amount = sum(row.get('balance', 0) for row in left_out)
        count = len(left_out)
        accounts = f"{count} account{'s' if count != 1 else ''}"
        parts.append(f"Leaves out {dollars(amount)} in {accounts}." if amount > 0.5
                     else f"Leaves out {accounts}.")
    if not parts:
        return ''
    return ' '.join(parts) + ' See Your details.'


# How many of the next best moves the Plan page lists, as Runway listed them.
MOVES_SHOWN = 4


def describe_move(move: dict, inputs: dict) -> tuple[str, str, str, str]:
    """(title, detail, tag, button) for one of retirement.best_moves', in
    Runway's words. The tag is the short figure beside it, '' for none."""
    kind = move['kind']
    target = f"{round(retirement.ON_TRACK * 100)}% or better"
    if kind == 'work_longer':
        years = move['years']
        plural = 's' if years != 1 else ''
        if move.get('partner'):
            return (f"Your partner works {years} more year{plural}",
                    f"Your partner retiring at {move['retire_age']} gets you to {target}",
                    f"{round(retirement.ON_TRACK * 100)}%+", 'Try it')
        return (f"Work {years} more year{plural}",
                f"Retiring at {move['retire_age']} gets you to {target}",
                f"{round(retirement.ON_TRACK * 100)}%+", 'Try it')
    if kind == 'spend_less':
        return (f"Spend {dollars(move['cut'])} less a year",
                f"At {dollars(move['spending'])} a year you reach {target}",
                f"{round(retirement.ON_TRACK * 100)}%+", 'Try it')
    if kind == 'retire_earlier':
        years = move['years']
        who = 'Your partner' if move.get('partner') else 'You'
        return (f"{who} may be able to retire at {move['retire_age']}",
                f"That still keeps your odds at {target}",
                f"{years} year{'s' if years != 1 else ''} sooner", 'Try it')
    if kind == 'start_conversions':
        return ('Turn on Roth conversions',
                f"Leaves about {dollars(move['gain'])} more by {inputs['plan_to']} "
                f"with average returns", f"+{dollars(move['gain'])}", 'Open')
    if kind == 'conversions':
        detail = f"Fills the {round(move['rate'] * 100)}% bracket for {move['years']} years"
        if move['tax_saved'] > 0:
            detail += f", cutting lifetime tax by about {dollars(move['tax_saved'])}"
        return (f"Convert about {dollars(move['first'])} to Roth in your first retired year",
                detail, f"+{dollars(move['gain'])}" if move['gain'] > 0 else '', 'Open')
    if kind == 'delay_social_security':
        return ('Delay Social Security to 70', 'A bigger check for life raises your odds',
                f"+{move['points']} points", 'Try it')
    years = move['years']
    return ('Budget for health coverage until 65',
            f"{years} year{'s' if years != 1 else ''} at {dollars(move['premium'])} a year "
            f"before Medicare starts", '', 'Open')


class _Page(QWidget):
    """A result page: a scrolling column rebuilt for each result."""

    details_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._content = QWidget()
        self._body = QVBoxLayout(self._content)
        self._body.setContentsMargins(18, 16, 18, 16)
        self._body.setSpacing(16)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

    def clear(self):
        clear_layout(self._body)

    def show_result(self, result: PlanResult):
        clear_layout(self._body)
        self._build(result)
        self._body.addWidget(subtle(FOOTNOTE))
        self._body.addStretch(1)

    def _build(self, result: PlanResult):
        raise NotImplementedError

    def _heading(self, text: str, note: str = '', action: str = '') -> None:
        """The page's heading, a line under it, and a button beside it that
        opens Your details."""
        heading = QLabel(text)
        heading.setObjectName('SectionHeading')
        heading.setWordWrap(True)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addWidget(heading)
        if note:
            texts.addWidget(subtle(note))
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addLayout(texts, 1)
        if action:
            button = QPushButton(action)
            button.clicked.connect(self.details_requested.emit)
            row.addWidget(button, 0, Qt.AlignTop)
        self._body.addLayout(row)


# -- Plan ----------------------------------------------------------------------

class PlanPage(_Page):
    """The odds the money lasts, the figures behind them, the next best moves,
    and the portfolio by age."""

    # A next best move whose button was pressed.
    move_chosen = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._moves_box = None
        self._inputs = None
        self._savings = None

    def clear(self):
        super().clear()
        self._moves_box = None
        self._savings = None

    def set_savings(self, savings: dict | None):
        """The accounts the plan's savings came from, as
        savings_from_accounts returns them, or None for the sample household.
        Read by the next show_result."""
        self._savings = savings

    def _build(self, result: PlanResult):
        inputs = result.inputs
        self._inputs = inputs
        who = 'Two of you' if inputs['partner'] else 'Just you'
        if result.sample:
            self._heading(f"Sample household: {who.lower()}")
        else:
            retiring = f"retiring at {inputs['retire_age']}"
            if inputs['partner'] and inputs['partner_retire_age'] != inputs['retire_age']:
                retiring += f" (partner at {inputs['partner_retire_age']})"
            self._heading(f"{who}, {inputs['age']} now, {retiring}, "
                          f"planning to age {inputs['plan_to']}", action='Update details')

        top = QHBoxLayout()
        top.setSpacing(16)
        top.addWidget(self._odds_card(inputs, result.simulation), 1)
        top.addLayout(self._tiles(inputs, result.simulation,
                                  None if result.sample else self._savings), 1)
        self._body.addLayout(top)
        note = '' if result.sample else pay_note(inputs)
        if note:
            self._body.addWidget(subtle(note))
        self._body.addWidget(self._moves_card())
        self._body.addWidget(self._chart_card(inputs, result.simulation))

    def _moves_card(self) -> QFrame:
        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)
        box.addWidget(card_title('Next best moves'))
        self._moves_box = QVBoxLayout()
        self._moves_box.setSpacing(10)
        box.addLayout(self._moves_box)
        self.show_moves_pending()
        return frame

    def show_moves_pending(self):
        if self._moves_box is None:
            return
        clear_layout(self._moves_box)
        self._moves_box.addWidget(subtle('Working out your options…'))

    def show_moves(self, moves: list):
        """Lists the first few moves the search found, each with a button that
        tries it."""
        if self._moves_box is None:
            return
        clear_layout(self._moves_box)
        if not moves:
            self._moves_box.addWidget(subtle('Your plan looks solid. Try a what-if to '
                                             'explore other options.'))
            return
        for move in moves[:MOVES_SHOWN]:
            title, detail, tag, button_text = describe_move(move, self._inputs)
            texts = QVBoxLayout()
            texts.setSpacing(2)
            name = QLabel(title)
            name.setObjectName('CardTitle')
            name.setWordWrap(True)
            texts.addWidget(name)
            texts.addWidget(subtle(detail))
            row = QHBoxLayout()
            row.setSpacing(12)
            row.addLayout(texts, 1)
            if tag:
                figure = QLabel(tag)
                figure.setObjectName('CardValue')
                row.addWidget(figure)
            button = QPushButton(button_text)
            button.setObjectName('Small')
            button.setAccessibleName(f"{button_text}: {title}")
            button.clicked.connect(lambda _=False, m=move: self.move_chosen.emit(m))
            row.addWidget(button)
            self._moves_box.addLayout(row)

    @staticmethod
    def _odds_card(inputs: dict, simulation) -> QFrame:
        tone = retirement.verdict(simulation.success)
        words, object_name = VERDICTS[tone]
        title = card_title(f"Chance your money lasts to age {inputs['plan_to']}")
        title.setWordWrap(True)
        verdict = QLabel(words)
        verdict.setObjectName(object_name)

        texts = QVBoxLayout()
        texts.setSpacing(6)
        texts.addStretch(1)
        texts.addWidget(field_label(f"Retire at {inputs['retire_age']}"))
        texts.addWidget(title)
        texts.addWidget(verdict)
        texts.addWidget(subtle(f"Across {retirement.SIM_PATHS:,} simulated markets"))
        texts.addStretch(1)

        frame = card()
        row = QHBoxLayout(frame)
        row.setContentsMargins(16, 14, 16, 14)
        row.setSpacing(18)
        row.addWidget(OddsRing(simulation.success, tone))
        row.addLayout(texts, 1)
        return frame

    @staticmethod
    def _tiles(inputs: dict, simulation, savings: dict | None = None) -> QGridLayout:
        years = inputs['retire_age'] - inputs['age']
        at_retirement = simulation.p50[min(max(years, 0), len(simulation.p50) - 1)]
        tiles = [
            ('Invested today', dollars(sum(inputs[key] for key in
                                           ('pretax', 'roth', 'taxable', 'hsa'))),
             invested_note(savings)),
            ('Spending per year', dollars(inputs['spending']), extras_note(inputs)),
            ('Years until retirement', str(years), '') if years > 0 else ('Retired', 'Now', ''),
            ('Typical at retirement', dollars(at_retirement), ''),
        ]
        grid = QGridLayout()
        grid.setSpacing(12)
        for index, (label, value, note) in enumerate(tiles):
            frame = card()
            box = QVBoxLayout(frame)
            box.setContentsMargins(14, 10, 14, 10)
            box.setSpacing(2)
            box.addWidget(subtle(label))
            figure = QLabel(value)
            figure.setObjectName('StatValue')
            box.addWidget(figure)
            if note:
                box.addWidget(subtle(note))
            grid.addWidget(frame, index // 2, index % 2)
        return grid

    @staticmethod
    def _chart_card(inputs: dict, simulation) -> QFrame:
        tooltips = [f"Age {age}: typical {dollars(middle)}, middle 80% "
                    f"{dollars(low)} to {dollars(high)}"
                    for age, low, middle, high
                    in zip(simulation.ages, simulation.p10, simulation.p50, simulation.p90)]
        chart = BandChart(simulation.ages, [(simulation.p50, 'accent', 2, False)],
                          band=(simulation.p10, simulation.p90),
                          markers=[(inputs['retire_age'], 'Retire')], tooltips=tooltips)

        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(8)
        box.addWidget(card_title('Portfolio over time'))
        box.addWidget(subtle("Total of every account, by age, in today's dollars"))
        box.addWidget(chart)
        box.addLayout(legend([(Swatch('accent'), 'Typical (median)'),
                              (Swatch('accent', band=True), 'Middle 80% of outcomes')]))
        return frame


# -- Projection ----------------------------------------------------------------

class ProjectionPage(_Page):
    """The spread of outcomes by age, what the household holds today, and the
    assumptions behind it."""

    def _build(self, result: PlanResult):
        inputs, simulation = result.inputs, result.simulation
        self._heading('Total portfolio by age',
                      f"Across {retirement.SIM_PATHS:,} simulated markets, in today's dollars")
        self._body.addWidget(self._chart_card(inputs, simulation))

        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(self._holdings_card(inputs), 1)
        row.addWidget(self._assumptions_card(inputs, result.sample), 1)
        self._body.addLayout(row)
        self._body.addWidget(self._range_card(inputs, simulation))

    @staticmethod
    def _chart_card(inputs: dict, simulation) -> QFrame:
        markers = [(inputs['retire_age'], 'Retire')]
        if inputs['ss_age'] != inputs['retire_age']:
            markers.append((inputs['ss_age'], 'Social Security'))
        markers += partner_retires(inputs) + pension_starts(inputs)
        tooltips = [f"Age {age}: poor markets {dollars(low)}, typical {dollars(middle)}, "
                    f"strong markets {dollars(high)}"
                    for age, low, middle, high
                    in zip(simulation.ages, simulation.p10, simulation.p50, simulation.p90)]
        chart = BandChart(simulation.ages,
                          [(simulation.p10, 'level_danger', 2, True),
                           (simulation.p50, 'accent', 2, False)],
                          band=(simulation.p10, simulation.p90), markers=markers,
                          tooltips=tooltips, height=300)
        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(8)
        box.addWidget(chart)
        box.addLayout(legend([(Swatch('accent'), 'Typical (median)'),
                              (Swatch('accent', band=True), 'Middle 80% of outcomes'),
                              (Swatch('level_danger', dashed=True),
                               'Poor markets (10th percentile)')]))
        return frame

    @staticmethod
    def _holdings_card(inputs: dict) -> QFrame:
        parts = [
            ('taxable', 'Taxable brokerage and cash', inputs['taxable']),
            ('pretax', '401(k) / Traditional IRA', inputs['pretax']),
            ('taxfree', 'Roth and HSA', inputs['roth'] + inputs['hsa']),
        ]
        total = sum(value for *_, value in parts)

        header = QHBoxLayout()
        header.addWidget(card_title('Where your money sits today'), 1)
        figure = QLabel(dollars(total))
        figure.setObjectName('CardValue')
        header.addWidget(figure)

        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)
        box.addLayout(header)
        box.addWidget(MixBar([(RETIREMENT_SERIES[key], value) for key, _, value in parts]))
        for key, name, value in parts:
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(Swatch(RETIREMENT_SERIES[key], block=True))
            row.addWidget(plain_label(name), 1)
            share = f"{round(value / total * 100)}%" if total else '0%'
            row.addWidget(plain_label(dollars(value)))
            row.addWidget(subtle(share, wrap=False))
            box.addLayout(row)
        box.addStretch(1)
        return frame

    def _assumptions_card(self, inputs: dict, sample: bool) -> QFrame:
        social_security = f"{dollars(inputs['ss'])} a month at {inputs['ss_age']}"
        if inputs['partner']:
            social_security += (f", partner {dollars(inputs['partner_ss'])} "
                                f"at {inputs['partner_ss_age']}")
        premiums = f"{dollars(inputs['health'])} a year"
        health = f"{dollars(inputs['health_65'])} a year"
        if inputs['partner']:
            premiums += f", partner {dollars(inputs['partner_health'])}"
            health += f", partner {dollars(inputs['partner_health_65'])}"
        rows = [
            ('Average return', f"{inputs['return_mean']:g}% a year"),
            ('Ups and downs', f"{inputs['volatility']:g}% (standard deviation)"),
            ('Inflation', f"{inputs['inflation']:g}% a year"),
            ('Health premiums before 65', premiums),
            ('Health costs from 65', health),
            ('Social Security', social_security),
            ('Cost basis of taxable', f"{inputs['basis_pct']:g}%"),
        ]
        pensions = pension_text(inputs)
        if pensions:
            rows.insert(6, ('Pensions', pensions))
        if inputs['partner'] and partner_retires_at(inputs) > inputs['retire_age'] \
                and inputs['partner_pay']:
            rows.insert(-1, ("Partner's pay", f"{dollars(inputs['partner_pay'])} a year until "
                                              f"retiring at {inputs['partner_retire_age']}"))
        if inputs['partner'] and partner_retires_at(inputs) < inputs['retire_age'] \
                and inputs['pay']:
            rows.insert(-1, ('Your pay', f"{dollars(inputs['pay'])} a year until "
                                         f"retiring at {inputs['retire_age']}"))
        header = QHBoxLayout()
        header.addWidget(card_title('Assumptions'), 1)
        if not sample:
            edit = QPushButton('Edit')
            edit.clicked.connect(self.details_requested.emit)
            header.addWidget(edit)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        for index, (label, value) in enumerate(rows):
            cell = QVBoxLayout()
            cell.setSpacing(2)
            cell.addWidget(subtle(label))
            text = plain_label(value)
            text.setWordWrap(True)
            cell.addWidget(text)
            grid.addLayout(cell, index // 2, index % 2)

        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)
        box.addLayout(header)
        box.addLayout(grid)
        box.addStretch(1)
        return frame

    @staticmethod
    def _range_card(inputs: dict, simulation) -> QFrame:
        """Every fifth age, both ends, and the year of retiring."""
        rows, bold = [], set()
        last = len(simulation.ages) - 1
        for index, age in enumerate(simulation.ages):
            retiring = age == inputs['retire_age']
            if not (retiring or age % 5 == 0 or index in (0, last)):
                continue
            if retiring:
                bold.add(len(rows))
            rows.append([f"{age} · retire" if retiring else str(age),
                         dollars(simulation.p10[index]), dollars(simulation.p50[index]),
                         dollars(simulation.p90[index])])
        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)
        box.addWidget(card_title('Range by age'))
        box.addWidget(figure_table(['Age', 'Poor markets', 'Typical', 'Strong markets'],
                                   rows, bold))
        return frame


# -- Withdrawals & taxes -------------------------------------------------------

class WithdrawalsPage(_Page):
    """Which account pays for each retired year, and the federal tax, on the
    steady path; and the Roth conversion setting."""

    # (convert, rate) when the user changes the Roth conversion setting.
    conversions_changed = Signal(bool, float)

    def _build(self, result: PlanResult):
        inputs, path = result.inputs, result.path
        self._heading('Withdrawals and taxes',
                      'Which account pays for each year, and what it costs in federal tax, '
                      'if markets return their average every year.')
        self._body.addWidget(self._summary_card(result))
        self._body.addWidget(self._conversions_card(inputs, result.sample))

        # Up to the year the money runs out: after it there is nothing to draw,
        # and a row of zeros would read as spending that stopped.
        retired = [row for row in path.rows if row['retired']
                   and (path.failed_at is None or row['age'] <= path.failed_at)]
        if not retired:
            self._body.addWidget(subtle('This plan has no retirement years yet.'))
            return
        note = ''
        if path.failed_at is not None:
            left = inputs['plan_to'] - path.failed_at
            note = (f"On this path the money runs out at {path.failed_at}, {left} "
                    f"year{'s' if left != 1 else ''} before the plan ends.")
        self._body.addWidget(self._chart_card(retired, note))
        self._body.addWidget(self._table_card(retired))

    @staticmethod
    def _summary_card(result: PlanResult) -> QFrame:
        inputs = result.inputs
        converting = inputs['convert']
        other = result.without if converting else result.with_conversions
        gain = (retirement.after_tax_end(result.with_conversions)
                - retirement.after_tax_end(result.without))

        def block(label: str, value: QLabel, note: str = '') -> QVBoxLayout:
            box = QVBoxLayout()
            box.setSpacing(4)
            box.addWidget(field_label(label))
            box.addWidget(value)
            if note:
                box.addWidget(subtle(note))
            box.addStretch(1)
            return box

        tax = QLabel(dollars(result.path.lifetime_tax))
        tax.setObjectName('StatValue')
        alternative = QLabel(dollars(other.lifetime_tax))
        alternative.setObjectName('StatValue')
        # A $0 lifetime tax looks like a bug until it is explained.
        no_tax = ("Each year's taxable income stays within the standard deduction and "
                  "the 0% rate on gains, so no federal tax is due.")

        # Good news and bad in their colours, the words saying it as well.
        with_age = result.with_conversions.failed_at
        without_age = result.without.failed_at
        if with_age is None and without_age is None:
            effect = QLabel(f"{'+' if gain >= 0 else '-'}{dollars(abs(gain))} after tax")
            effect.setObjectName('VerdictGood' if gain >= 0 else 'VerdictBad')
            effect_note = 'Counts leftover 401(k) / IRA money as if withdrawn at 22%'
        else:
            # Money left is nothing on a path that runs out, so a difference
            # in it would say nothing; when it runs out says more.
            if with_age is not None and without_age is not None:
                effect = QLabel('Runs out either way')
                effect.setObjectName('VerdictBad')
            elif with_age is None:
                effect = QLabel('Lasts only with conversions')
                effect.setObjectName('VerdictGood')
            else:
                effect = QLabel('Lasts only without conversions')
                effect.setObjectName('VerdictBad')

            def fate(age):
                return 'lasts' if age is None else f"runs out at {age}"
            effect_note = (f"With conversions the money {fate(with_age)}; without, it "
                           f"{fate(without_age)}.")

        frame = card()
        row = QHBoxLayout(frame)
        row.setContentsMargins(16, 14, 16, 14)
        row.setSpacing(24)
        row.addLayout(block(f"Lifetime federal tax, "
                            f"{'with' if converting else 'without'} conversions", tax,
                            no_tax if result.path.lifetime_tax < 0.5 else ''), 1)
        row.addLayout(block(f"{'Without' if converting else 'With'} conversions",
                            alternative, no_tax if other.lifetime_tax < 0.5 else ''), 1)
        row.addLayout(block(f"Conversions' effect on money left at {inputs['plan_to']}",
                            effect, effect_note), 1)
        return frame

    def _conversions_card(self, inputs: dict, sample: bool) -> QFrame:
        check = QCheckBox('Convert pre-tax money to Roth in low-income years')
        check.setChecked(bool(inputs['convert']))
        bracket = QComboBox()
        for rate, label in BRACKETS:
            bracket.addItem(label, rate)
        bracket.setCurrentIndex(max(0, bracket.findData(inputs['convert_rate'])))
        bracket.setEnabled(bool(inputs['convert']) and not sample)
        check.setEnabled(not sample)

        def changed(*_args):
            self.conversions_changed.emit(check.isChecked(), bracket.currentData())
        check.toggled.connect(changed)
        bracket.activated.connect(changed)

        controls = QHBoxLayout()
        controls.setSpacing(12)
        controls.addWidget(check)
        controls.addSpacing(12)
        controls.addWidget(subtle('Fill up to the', wrap=False))
        controls.addWidget(bracket)
        controls.addStretch(1)

        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)
        box.addWidget(card_title('Roth conversions'))
        box.addLayout(controls)
        box.addWidget(subtle('Conversions run from retirement until required distributions '
                             'start at 75. The tax on each one is paid from your accounts '
                             'in the same year.'))
        if sample:
            box.addWidget(subtle("Showing the sample household's setting. Your own is on "
                                 "your plan."))
        return frame

    @staticmethod
    def _chart_card(rows: list, note: str = '') -> QFrame:
        income = [row['social_security'] + row['pension'] + row['work'] + row['pay']
                  + row['partner_pay'] for row in rows]
        taxfree = [row['from_roth'] + row['from_hsa'] for row in rows]
        series = [
            (RETIREMENT_SERIES['income'], income),
            (RETIREMENT_SERIES['taxable'], [row['from_taxable'] for row in rows]),
            (RETIREMENT_SERIES['pretax'], [row['from_pretax'] for row in rows]),
            (RETIREMENT_SERIES['taxfree'], taxfree),
        ]
        tooltips = []
        for row in rows:
            lines = [f"Age {row['age']}"]
            for label, value in (('Social Security', row['social_security']),
                                 ('Pension', row['pension']),
                                 ('Part-time work', row['work']),
                                 ('Your pay', row['pay']),
                                 ("Partner's pay", row['partner_pay']),
                                 ('Taxable', row['from_taxable']),
                                 ('401(k) / IRA', row['from_pretax']),
                                 ('Roth and HSA', row['from_roth'] + row['from_hsa'])):
                if value > 0.5:
                    lines.append(f"{label}: {dollars(value)}")
            if row['rmd'] > 1:
                lines.append(f"Required distribution: {dollars(row['rmd'])}")
            lines.append(f"Federal tax: {dollars(row['tax'])}")
            tooltips.append('\n'.join(lines))

        chart = StackedBars([row['age'] for row in rows], series, tooltips=tooltips)
        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(8)
        box.addWidget(card_title("Where each year's money comes from"))
        if note:
            box.addWidget(subtle(note))
        need = ("Each bar is the year's spending, extra spending included, health costs "
                "and federal tax.")
        if any(row['rmd'] > 1 for row in rows):
            need += (f" From {retirement.RMD_AGE}, a required distribution can take "
                     "more out of the 401(k) / IRA than the year needs. That makes the "
                     "bar taller, and the extra goes back into the taxable account.")
        box.addWidget(subtle(need))
        box.addWidget(chart)
        sources = ['Social Security']
        if any(row['pension'] > 0.5 for row in rows):
            sources.append('pensions')
        if any(row['pay'] > 0.5 for row in rows):
            sources.append('your pay')
        if any(row['partner_pay'] > 0.5 for row in rows):
            sources.append("partner's pay")
        sources.append('part-time work')
        box.addLayout(legend([
            (Swatch(RETIREMENT_SERIES['income'], block=True),
             ', '.join(sources[:-1]) + ' and ' + sources[-1]),
            (Swatch(RETIREMENT_SERIES['taxable'], block=True), 'Taxable'),
            (Swatch(RETIREMENT_SERIES['pretax'], block=True), '401(k) / IRA'),
            (Swatch(RETIREMENT_SERIES['taxfree'], block=True), 'Roth and HSA'),
        ]))
        return frame

    @staticmethod
    def _table_card(rows: list) -> QFrame:
        # A pension or pay column only for a plan with one: the table is
        # wide enough.
        pensions = any(row['pension'] > 0.5 for row in rows)
        pay = any(row['pay'] > 0.5 for row in rows)
        partner_pay = any(row['partner_pay'] > 0.5 for row in rows)
        cells, flagged = [], set()
        for index, row in enumerate(rows):
            age = str(row['age'])
            if row['shortfall'] > 1:
                age += ' · runs out'
                flagged.add(index)
            pretax = _or_dash(row['from_pretax'])
            if row['rmd'] > 1:
                pretax += ' (RMD)'
            income = [_or_dash(row['social_security'])]
            if pensions:
                income.append(_or_dash(row['pension']))
            work = [_or_dash(row['work'])]
            if pay:
                work.append(_or_dash(row['pay']))
            if partner_pay:
                work.append(_or_dash(row['partner_pay']))
            cells.append([age, dollars(row['need']), *income,
                          *work, _or_dash(row['from_taxable']), pretax,
                          _or_dash(row['from_roth'] + row['from_hsa']),
                          _or_dash(row['conversion']), dollars(row['tax']), dollars(row['end'])])
        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)
        box.addWidget(card_title('Year by year'))
        box.addWidget(subtle('Spending includes any extra spending. RMD marks a year with '
                             'a required distribution from the 401(k) / IRA.'))
        headers = ['Age', 'Spending + health', 'Social Security', 'Part-time', 'Taxable',
                   '401(k) / IRA', 'Roth and HSA', 'Roth conversion', 'Federal tax',
                   'End balance']
        if pensions:
            headers.insert(3, 'Pension')
        if pay:
            headers.insert(headers.index('Part-time') + 1, 'Your pay')
        if partner_pay:
            headers.insert(headers.index('Part-time') + 1 + pay, "Partner's pay")
        box.addWidget(figure_table(headers, cells, flagged))
        return frame
