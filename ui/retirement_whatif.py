# -----------------------------------------------------------------------------
# ui/retirement_whatif.py
# The Retirement tab's What-if and Scenarios pages.
#
# What-if moves six settings over a copy of the plan and compares the result
# with the plan itself. Nothing is stored until Apply, or until it is saved as a
# scenario. Scenarios sets the plan beside each saved one.
# -----------------------------------------------------------------------------

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QSlider, QVBoxLayout, QWidget,
)

from core import retirement
from ui.retirement_charts import BandChart, OddsRing, Swatch
from ui.retirement_details import dollars
from ui.retirement_pages import (
    FOOTNOTE, VERDICTS, PlanResult, card, card_title, compute, figure_table, legend, subtle,
)
from ui.theme import current_theme, palette
from ui.widgets import clear_layout, dot_pixmap, field_label, plain_label

# The line each column of the comparisons is drawn in: the plan first, then
# each scenario in turn. Slots of the checked palette in theme.py; lines only
# promise that neighbours are told apart, and the legend names every one.
LINE_ROLES = ('series_1', 'series_2', 'series_3', 'series_4', 'series_5')


def _whole(value: float):
    return int(value) if value == int(value) else value


# (key, label, lowest, highest, step, how a value reads). The ranges are
# Runway's, except spending's, which reset() fits around the plan (see
# _spending_range), and retirement age's, which reaches the plan's own limit
# so a suggested later retirement (up to LATEST_RETIREMENT) can be moved
# from. A plan value outside one, or a typed one, keeps its own value until
# the slider moves.
SLIDERS = (
    ('retire_age', 'Retirement age', 45, retirement.INPUT_RULES['retire_age'][2], 1, str),
    ('partner_retire_age', "Partner's retirement age", 45,
     retirement.INPUT_RULES['partner_retire_age'][2], 1, str),
    ('spending', 'Yearly spending', 30000, 250000, 1000, dollars),
    ('return_mean', 'Average yearly return', 2, 10, 0.5, lambda v: f"{v:g}%"),
    ('ss_age', 'Claim Social Security at', 62, 70, 1, str),
    ('part_time', 'Income a year', 0, 80000, 2000, dollars),
    ('part_time_years', 'For how many years', 0, 15, 1, str),
)

# Sliders that only mean something together, under one heading: part-time
# income counts for the first so many years after retiring. The two
# retirement ages sit together with no heading; the partner's shows only for
# a plan for two.
SLIDER_GROUPS = {('retire_age', 'partner_retire_age'): '',
                 ('part_time', 'part_time_years'): 'Part-time work after retiring'}

# Each extra spending item's slider moves its yearly amount, in steps of
# EXTRA_STEP, from nothing to twice the plan's amount, and at least to
# EXTRA_LEAST.
EXTRA_STEP = 500
EXTRA_LEAST = 20000


def _extra_key(item_id: str) -> str:
    """An extra spending item's key among the page's levers."""
    return f"extra:{item_id}"


def _extra_range(amount: float) -> tuple[int, int]:
    return 0, max(EXTRA_LEAST, -(-int(amount * 2) // EXTRA_STEP) * EXTRA_STEP)


def _divider() -> QFrame:
    divider = QFrame()
    divider.setObjectName('Divider')
    divider.setFixedHeight(1)
    return divider


def _typed(text: str):
    """The number in a value box, without the $, commas and % it reads with,
    or None when there isn't one."""
    try:
        return float(text.replace('$', '').replace(',', '').replace('%', '').strip())
    except ValueError:
        return None


def _spending_range(spending: float) -> tuple[int, int]:
    """(lowest, highest) for the spending slider: down to half the plan's
    figure and up to half again, and never narrower than Runway's $30,000 to
    $250,000. A plan spending $39,000 sat at the slider's left end, with no
    room to try spending less."""
    step = 1000
    low = min(30000, int(spending * 0.5) // step * step)
    high = max(250000, -(-int(spending * 1.5) // step) * step)
    return max(0, low), high


def auto_name(plan: dict, inputs: dict) -> str:
    """A scenario name saying what changed from the plan, as Runway named
    them: 'Retire at 52, Spend $90,000'."""
    parts = []
    if inputs['retire_age'] != plan['retire_age']:
        parts.append(f"Retire at {inputs['retire_age']}")
    if inputs['partner_retire_age'] != plan['partner_retire_age']:
        parts.append(f"Partner retires at {inputs['partner_retire_age']}")
    if inputs['spending'] != plan['spending']:
        parts.append(f"Spend {dollars(inputs['spending'])}")
    if inputs['part_time'] and inputs['part_time_years']:
        parts.append(f"Part-time {inputs['part_time_years']} yrs")
    if inputs['ss_age'] != plan['ss_age']:
        parts.append(f"Social Security at {inputs['ss_age']}")
    if inputs['return_mean'] != plan['return_mean']:
        parts.append(f"{inputs['return_mean']:g}% return")
    planned = {item[0]: item[2] for item in plan.get('extras', ())}
    for item_id, name, amount, *_rest in inputs.get('extras', ()):
        if item_id in planned and amount != planned[item_id]:
            parts.append(f"{name} {dollars(amount)}")
    return ', '.join(parts)[:retirement.SCENARIO_NAME_MAX] or 'Copy of my plan'


def _scroll(content: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(content)
    return scroll


def _odds(success: float) -> tuple[str, str]:
    """(verdict words, object name) for a chance."""
    return VERDICTS[retirement.verdict(success)]


# -- What-if -------------------------------------------------------------------

class WhatIfPage(QWidget):
    """A slider for each setting over a copy of the plan (the partner's
    retirement age only for two), one more for each extra spending item, and
    how the result compares."""

    # The changed settings, to write into the plan.
    applied = Signal(dict)
    # (name, changed settings), to keep as a scenario.
    save_requested = Signal(str, dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._base: PlanResult | None = None
        self._result: PlanResult | None = None
        self._levers: dict = {}
        self._name_edited = False
        self._sliders = {}
        self._values = {}
        self._plan_values = {}
        # Every slider's spec, as SLIDERS has them, the extra spending items'
        # among them while a plan is in hand.
        self._specs = {spec[0]: spec for spec in SLIDERS}
        # Each slider's (lowest, highest) now in force; the spending slider's
        # follows the plan.
        self._ranges = {key: (low, high) for key, _, low, high, _, _ in SLIDERS}
        # Each SLIDERS row's widgets, by key: the label, slider, box and plan value.
        self._rows = {}
        # The extra spending rows' keys and widgets, replaced at each reset().
        self._extra_keys: list[str] = []
        self._extra_widgets: list[QWidget] = []

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._run)

        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(18, 16, 18, 16)
        body.setSpacing(16)

        heading = QLabel('What if…')
        heading.setObjectName('SectionHeading')
        self._reset_button = QPushButton('Reset')
        self._reset_button.setMinimumWidth(90)
        self._reset_button.clicked.connect(self._reset_levers)
        self._apply_button = QPushButton('Apply to my plan')
        self._apply_button.clicked.connect(self._apply)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addWidget(heading)
        texts.addWidget(subtle('Change a few settings. Your saved plan stays as it is until '
                               'you apply them.'))
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addLayout(texts, 1)
        header.addWidget(self._reset_button, 0, Qt.AlignTop)
        header.addWidget(self._apply_button, 0, Qt.AlignTop)
        body.addLayout(header)

        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(self._build_sliders(), 1)
        side = QVBoxLayout()
        side.setSpacing(16)
        self._hero = card()
        self._hero_box = QHBoxLayout(self._hero)
        self._hero_box.setContentsMargins(16, 14, 16, 14)
        self._hero_box.setSpacing(18)
        side.addWidget(self._hero)
        self._compare = card()
        self._compare_box = QVBoxLayout(self._compare)
        self._compare_box.setContentsMargins(16, 14, 16, 14)
        self._compare_box.setSpacing(10)
        side.addWidget(self._compare)
        side.addWidget(self._build_save_card())
        side.addStretch(1)
        row.addLayout(side, 1)
        body.addLayout(row)

        self._chart = card()
        self._chart_box = QVBoxLayout(self._chart)
        self._chart_box.setContentsMargins(16, 14, 16, 14)
        self._chart_box.setSpacing(8)
        body.addWidget(self._chart)
        body.addWidget(subtle(FOOTNOTE))
        body.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_scroll(content))

    def _build_sliders(self) -> QFrame:
        """One row a setting: its name, the slider, a box to type the value
        into, and the plan's own value beside it. The extra spending rows
        follow, one an item, built by reset() for the plan in hand."""
        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 14, 16, 14)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(12)
        grid.setColumnStretch(1, 1)
        grid.setColumnMinimumWidth(1, 90)
        grid.addWidget(field_label('What-if'), 0, 2)
        grid.addWidget(field_label('Your plan'), 0, 3)
        groups = {key: (keys, heading) for keys, heading in SLIDER_GROUPS.items()
                  for key in keys}
        row = 1
        for index, (key, label, *_rest) in enumerate(SLIDERS):
            keys, heading = groups.get(key, ((key,), ''))
            # A line between sliders, but not between two of the same group.
            if index and key == keys[0]:
                grid.addWidget(_divider(), row, 0, 1, 4)
                row += 1
            if heading and key == keys[0]:
                grid.addWidget(card_title(heading), row, 0, 1, 4)
                row += 1
            self._rows[key] = self._add_row(grid, row, key, label, heading)
            row += 1
        self._grid, self._extras_row = grid, row
        box.addLayout(grid)
        box.addStretch(1)
        return frame

    def _add_row(self, grid, row, key, label, heading) -> tuple:
        """A setting's row of the slider card, and its widgets."""
        low, high, step = self._ranges[key] + (self._spec(key)[4],)
        name = f"{heading}: {label}" if heading else label

        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, round((high - low) / step))
        slider.setPageStep(max(1, round((high - low) / step / 10)))
        # Dragging updates the figure as it goes and simulates on release;
        # a simulation takes a few tenths of a second.
        slider.setTracking(False)
        slider.setAccessibleName(name)
        slider.sliderMoved.connect(lambda position, k=key: self._show_value(
            k, self._from_position(k, position)))
        slider.valueChanged.connect(lambda position, k=key: self._moved(k, position))

        value = QLineEdit()
        value.setFixedWidth(110)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        value.setAccessibleName(f"{name}, typed")
        value.editingFinished.connect(lambda k=key: self._typed(k))

        # A grouped setting is indented under its heading.
        title = plain_label(label)
        if heading:
            title.setContentsMargins(12, 0, 0, 0)
        plan_value = plain_label()
        plan_value.setObjectName('Subtle')
        grid.addWidget(title, row, 0)
        grid.addWidget(slider, row, 1)
        grid.addWidget(value, row, 2)
        grid.addWidget(plan_value, row, 3)
        self._sliders[key] = slider
        self._values[key] = value
        self._plan_values[key] = plan_value
        return title, slider, value, plan_value

    def _build_extra_rows(self, extras):
        """One slider an extra spending item, under its own heading, in place
        of the last plan's. Each moves the item's yearly amount."""
        for widget in self._extra_widgets:
            self._grid.removeWidget(widget)
            widget.hide()
            widget.deleteLater()
        for key in self._extra_keys:
            for table in (self._sliders, self._values, self._plan_values, self._specs,
                          self._ranges):
                table.pop(key, None)
        self._extra_widgets, self._extra_keys = [], []
        if not extras:
            return
        row = self._extras_row
        divider, heading = _divider(), card_title('Extra spending')
        self._grid.addWidget(divider, row, 0, 1, 4)
        self._grid.addWidget(heading, row + 1, 0, 1, 4)
        self._extra_widgets += [divider, heading]
        row += 2
        for item_id, name, amount, *_rest in extras:
            key = _extra_key(item_id)
            low, high = _extra_range(amount)
            self._specs[key] = (key, name, low, high, EXTRA_STEP, dollars)
            self._ranges[key] = (low, high)
            self._extra_widgets += self._add_row(self._grid, row, key, name, 'Extra spending')
            self._extra_keys.append(key)
            row += 1

    def _build_save_card(self) -> QFrame:
        self._name = QLineEdit()
        self._name.setMaxLength(retirement.SCENARIO_NAME_MAX)
        self._name.setAccessibleName('Scenario name')
        self._name.textEdited.connect(self._name_typed)
        self._name.returnPressed.connect(self._save)
        self._save_button = QPushButton('Save scenario')
        self._save_button.clicked.connect(self._save)
        self._status = plain_label()
        self._status.setWordWrap(True)
        self._status.hide()

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._name, 1)
        row.addWidget(self._save_button)
        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(8)
        box.addWidget(field_label('Save this as a scenario'))
        box.addLayout(row)
        box.addWidget(self._status)
        return frame

    # -- values -----------------------------------------------------------

    def _spec(self, key):
        return self._specs[key]

    def _plan_value(self, key):
        if key in self._extra_keys:
            return next(item[2] for item in self._base.inputs.get('extras', ())
                        if _extra_key(item[0]) == key)
        return self._base.inputs[key]

    def _from_position(self, key, position: int):
        step = self._spec(key)[4]
        low, _ = self._ranges[key]
        return _whole(low + position * step)

    def _position(self, key, value) -> int:
        step = self._spec(key)[4]
        low, high = self._ranges[key]
        return round((min(max(value, low), high) - low) / step)

    def _set_range(self, key, low, high):
        """Moves a slider's ends."""
        if self._ranges[key] == (low, high):
            return
        self._ranges[key] = (low, high)
        step = self._spec(key)[4]
        slider = self._sliders[key]
        slider.blockSignals(True)
        slider.setRange(0, round((high - low) / step))
        slider.setPageStep(max(1, round((high - low) / step / 10)))
        slider.blockSignals(False)

    def _show_value(self, key, value):
        self._values[key].setText(self._spec(key)[5](value))

    def _sync_sliders(self):
        """Puts every slider where the levers say, without that counting as a
        move. A value past a slider's end shows as it is; the slider sits at
        the end."""
        for key, slider in self._sliders.items():
            slider.blockSignals(True)
            slider.setValue(self._position(key, self._levers[key]))
            slider.blockSignals(False)
            self._show_value(key, self._levers[key])
            self._plan_values[key].setText(self._spec(key)[5](self._plan_value(key)))

    def _typed(self, key):
        """A value typed into a box: kept as typed when the plan can run on
        it, with the slider moved to it, or as close as its ends allow. The
        box goes back to the value in force otherwise."""
        if self._base is None:
            return
        value = _typed(self._values[key].text())
        rule = (retirement.EXTRA_RULES['amount'] if key in self._extra_keys
                else retirement.INPUT_RULES[key])
        try:
            value = retirement.check_value(key, value, rule)
        except ValueError:
            value = None
        if value is None:
            self._show_value(key, self._levers[key])
            return
        value = _whole(round(value, 2))
        if value == self._levers[key]:
            self._show_value(key, value)
            return
        self._levers[key] = value
        slider = self._sliders[key]
        slider.blockSignals(True)
        slider.setValue(self._position(key, value))
        slider.blockSignals(False)
        self._show_value(key, value)
        self._refresh_name()
        self._timer.start()

    def _moved(self, key, position: int):
        if self._base is None:
            return
        self._levers[key] = self._from_position(key, position)
        self._show_value(key, self._levers[key])
        self._refresh_name()
        self._timer.start()

    def _name_typed(self, _text):
        self._name_edited = True

    def _refresh_name(self):
        if not self._name_edited and self._base is not None:
            self._name.setText(auto_name(self._base.inputs, self._inputs()))

    def _inputs(self) -> dict:
        levers = {key: self._levers[key] for key in retirement.WHAT_IF_LEVERS}
        levers[retirement.EXTRA_LEVER] = {
            item[0]: self._levers[_extra_key(item[0])]
            for item in self._base.inputs.get('extras', ())}
        return retirement.apply_levers(self._base.inputs, levers)

    # -- the page's life ----------------------------------------------------

    def reset(self, base: PlanResult):
        """Starts again from a household's result: the levers at its values,
        and the comparison showing no difference."""
        self._timer.stop()
        self._base = base
        self._levers = {key: base.inputs[key] for key in retirement.WHAT_IF_LEVERS}
        self._levers.update({_extra_key(item[0]): item[2]
                             for item in base.inputs.get('extras', ())})
        self._name_edited = False
        self._build_extra_rows(base.inputs.get('extras', ()))
        two = bool(base.inputs['partner'])
        for widget in self._rows['partner_retire_age']:
            widget.setVisible(two)
        self._rows['retire_age'][0].setText('Your retirement age' if two else 'Retirement age')
        self._set_range('spending', *_spending_range(base.inputs['spending']))
        self._sync_sliders()
        self._refresh_name()
        editable = not base.sample
        for widget in (self._apply_button, self._save_button, self._name):
            widget.setEnabled(editable)
        self._apply_button.setToolTip(
            '' if editable else 'Enter your details to apply or save a what-if')
        self._result = base
        self._render()

    def preset(self, levers: dict):
        """Moves some levers at once, as a suggested move does, and runs it."""
        if self._base is None:
            return
        self._levers.update(levers)
        self._sync_sliders()
        self._refresh_name()
        self._run()

    def clear(self):
        self._timer.stop()
        self._base = self._result = None
        self._levers = {}
        self._name.clear()
        self._build_extra_rows(())
        for box in (*self._values.values(), *self._plan_values.values()):
            box.clear()
        self._status.hide()
        for box in (self._hero_box, self._compare_box, self._chart_box):
            clear_layout(box)

    def show_status(self, text: str, error: bool = False):
        self._status.setObjectName('ErrorLabel' if error else 'Subtle')
        self._status.style().unpolish(self._status)
        self._status.style().polish(self._status)
        self._status.setText(text)
        self._status.setVisible(bool(text))

    def _reset_levers(self):
        if self._base is not None:
            self.show_status('')
            self.reset(self._base)

    def _apply(self):
        if self._base is not None and not self._base.sample:
            self.applied.emit(retirement.changed_levers(self._base.inputs, self._inputs()))

    def _save(self):
        if self._base is None or self._base.sample:
            return
        inputs = self._inputs()
        name = self._name.text().strip() or auto_name(self._base.inputs, inputs)
        self.save_requested.emit(name, retirement.changed_levers(self._base.inputs, inputs))

    def _run(self):
        if self._base is None:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._result = compute(self._inputs(), self._base.sample)
            self._render()
        finally:
            QApplication.restoreOverrideCursor()

    # -- drawing ----------------------------------------------------------

    def _render(self):
        for box in (self._hero_box, self._compare_box, self._chart_box):
            clear_layout(box)
        base, result = self._base, self._result
        if base is None or result is None:
            return
        plan_to = result.inputs['plan_to']
        success = result.simulation.success
        words, object_name = _odds(success)
        points = round((success - base.simulation.success) * 100)

        verdict = QLabel(words)
        verdict.setObjectName(object_name)
        title = card_title(f"Chance your money lasts to age {plan_to}")
        title.setWordWrap(True)
        texts = QVBoxLayout()
        texts.setSpacing(6)
        texts.addStretch(1)
        texts.addWidget(title)
        texts.addWidget(verdict)
        texts.addWidget(subtle('Same as your plan' if points == 0 else
                               f"{'+' if points > 0 else ''}{points} points against your plan"))
        texts.addStretch(1)
        self._hero_box.addWidget(OddsRing(success, retirement.verdict(success), size=112))
        self._hero_box.addLayout(texts, 1)

        plan_sim, sim = base.simulation, result.simulation
        rows = [
            ['Retire at', str(base.inputs['retire_age']), str(result.inputs['retire_age'])],
            ['Spending a year', dollars(base.inputs['spending']),
             dollars(result.inputs['spending'])],
            *([['Partner retires at', str(base.inputs['partner_retire_age']),
                str(result.inputs['partner_retire_age'])]] if base.inputs['partner'] else []),
            [f"Typical at {plan_to}", dollars(plan_sim.p50[-1]), dollars(sim.p50[-1])],
            [f"Poor markets at {plan_to}", dollars(plan_sim.p10[-1]), dollars(sim.p10[-1])],
            ['Lifetime federal tax', dollars(base.path.lifetime_tax),
             dollars(result.path.lifetime_tax)],
        ]
        self._compare_box.addWidget(card_title('Compared with your plan'))
        self._compare_box.addWidget(figure_table(['', 'Your plan', 'What-if'], rows))

        count = min(len(plan_sim.p50), len(sim.p50))
        chart = BandChart(sim.ages[:count],
                          [(plan_sim.p50[:count], LINE_ROLES[0], 2, False),
                           (sim.p50[:count], LINE_ROLES[1], 2, False)],
                          markers=[(result.inputs['retire_age'], 'What-if retires')],
                          tooltips=[f"Age {age}: your plan {dollars(a)}, what-if {dollars(b)}"
                                    for age, a, b in zip(sim.ages, plan_sim.p50, sim.p50)])
        self._chart_box.addWidget(card_title('Typical outcome: your plan and this what-if'))
        self._chart_box.addWidget(chart)
        self._chart_box.addLayout(legend([(Swatch(LINE_ROLES[0]), 'Your plan'),
                                          (Swatch(LINE_ROLES[1]), 'This what-if')]))


# -- Scenarios -----------------------------------------------------------------

class ScenariosPage(QWidget):
    """The plan beside each saved scenario: a card each, the typical outcome of
    all of them on one chart, and their figures side by side."""

    adopt_requested = Signal(str)
    delete_requested = Signal(str)
    whatif_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        content = QWidget()
        self._body = QVBoxLayout(content)
        self._body.setContentsMargins(18, 16, 18, 16)
        self._body.setSpacing(16)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_scroll(content))

    def clear(self):
        clear_layout(self._body)

    def show_message(self, text: str):
        clear_layout(self._body)
        message = subtle(text)
        message.setObjectName('Placeholder')
        message.setAlignment(Qt.AlignCenter)
        self._body.addWidget(message)
        self._body.addStretch(1)

    def show_scenarios(self, entries: list):
        """entries are (scenario, result): the plan first, with None for its
        scenario, then each saved one in its stored order."""
        clear_layout(self._body)
        heading = QLabel('Compare scenarios')
        heading.setObjectName('SectionHeading')
        new = QPushButton('New scenario')
        new.clicked.connect(self.whatif_requested.emit)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addWidget(heading)
        texts.addWidget(subtle("Your plan beside the what-ifs you've saved. Each keeps only "
                               "the settings it changed, so it follows the rest of your plan."))
        header = QHBoxLayout()
        header.addLayout(texts, 1)
        header.addWidget(new, 0, Qt.AlignTop)
        self._body.addLayout(header)

        best = max(range(len(entries)), key=lambda i: entries[i][1].simulation.success)
        grid = QGridLayout()
        grid.setSpacing(12)
        columns = 3
        for index, (scenario, result) in enumerate(entries):
            grid.addWidget(self._card(index, scenario, result,
                                      best=index == best and len(entries) > 1),
                           index // columns, index % columns)
        if len(entries) == 1:
            grid.addWidget(self._empty_card(), 0, 1, 1, 2)
        for column in range(columns):
            grid.setColumnStretch(column, 1)
        self._body.addLayout(grid)

        if len(entries) > 1:
            self._body.addWidget(self._chart_card(entries))
            self._body.addWidget(self._table_card(entries))
        self._body.addWidget(subtle(FOOTNOTE))
        self._body.addStretch(1)

    @staticmethod
    def _name(scenario) -> str:
        return 'My plan' if scenario is None else scenario['name']

    def _card(self, index: int, scenario, result: PlanResult, best: bool) -> QFrame:
        inputs = result.inputs
        description = f"Retire at {inputs['retire_age']}, spend {dollars(inputs['spending'])}"
        if inputs['part_time'] and inputs['part_time_years']:
            description += (f", {dollars(inputs['part_time'])} part-time for "
                            f"{inputs['part_time_years']} years")
        words, object_name = _odds(result.simulation.success)

        title = QHBoxLayout()
        title.setSpacing(8)
        title.addWidget(Swatch(LINE_ROLES[index]))
        name = plain_label(self._name(scenario))
        name.setObjectName('CardTitle')
        title.addWidget(name, 1)
        if scenario is None:
            title.addWidget(subtle('Current', wrap=False))
        elif best:
            title.addWidget(subtle('Highest odds', wrap=False))

        figure = QLabel(f"{round(result.simulation.success * 100)}%")
        figure.setObjectName('StatValue')
        verdict = QLabel(words)
        verdict.setObjectName(object_name)

        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(6)
        box.addLayout(title)
        box.addWidget(subtle(description))
        odds = QHBoxLayout()
        odds.setSpacing(10)
        odds.addWidget(figure)
        odds.addWidget(verdict)
        odds.addStretch(1)
        box.addLayout(odds)
        box.addWidget(subtle(f"Chance the money lasts to {inputs['plan_to']}"))
        if scenario is not None:
            actions = QHBoxLayout()
            actions.setSpacing(8)
            adopt = QPushButton('Make this my plan')
            adopt.setObjectName('Small')
            adopt.clicked.connect(lambda: self.adopt_requested.emit(scenario['id']))
            delete = QPushButton('Delete')
            delete.setObjectName('Small')
            delete.setAccessibleName(f"Delete {scenario['name']}")
            delete.clicked.connect(lambda: self.delete_requested.emit(scenario['id']))
            actions.addWidget(adopt)
            actions.addWidget(delete)
            actions.addStretch(1)
            box.addLayout(actions)
        box.addStretch(1)
        return frame

    def _empty_card(self) -> QFrame:
        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(8)
        box.addWidget(card_title('No saved scenarios yet'))
        box.addWidget(subtle('Try a change on What-if and save it to compare it here.'))
        button = QPushButton('Open What-if')
        button.clicked.connect(self.whatif_requested.emit)
        box.addWidget(button, 0, Qt.AlignLeft)
        box.addStretch(1)
        return frame

    def _chart_card(self, entries: list) -> QFrame:
        count = min(len(result.simulation.p50) for _, result in entries)
        ages = entries[0][1].simulation.ages[:count]
        lines = [(result.simulation.p50[:count], LINE_ROLES[index], 2, False)
                 for index, (_, result) in enumerate(entries)]
        tooltips = []
        for position, age in enumerate(ages):
            parts = [f"{self._name(scenario)} {dollars(result.simulation.p50[position])}"
                     for scenario, result in entries]
            tooltips.append(f"Age {age}: " + ', '.join(parts))
        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(8)
        box.addWidget(card_title('Typical outcome, by scenario'))
        box.addWidget(BandChart(ages, lines, tooltips=tooltips))
        box.addLayout(legend([(Swatch(LINE_ROLES[index]), self._name(scenario))
                              for index, (scenario, _) in enumerate(entries)]))
        return frame

    def _table_card(self, entries: list) -> QFrame:
        plan_to = entries[0][1].inputs['plan_to']

        def extra_total(inputs):
            total = sum(retirement.extra_spending(inputs))
            return dollars(total) if total else '—'

        def part_time(inputs):
            if inputs['part_time'] and inputs['part_time_years']:
                return f"{dollars(inputs['part_time'])} × {inputs['part_time_years']} yrs"
            return '—'

        measures = [
            ('Retire at', lambda r: str(r.inputs['retire_age'])),
            ('Partner retires at', lambda r: str(r.inputs['partner_retire_age'])),
            ('Spending a year', lambda r: dollars(r.inputs['spending'])),
            ('Part-time work', lambda r: part_time(r.inputs)),
            ('Claim Social Security', lambda r: str(r.inputs['ss_age'])),
            ('Average return', lambda r: f"{r.inputs['return_mean']:g}%"),
            ('Extra spending', lambda r: extra_total(r.inputs)),
            (f"Typical at {plan_to}", lambda r: dollars(r.simulation.p50[-1])),
            (f"Poor markets at {plan_to}", lambda r: dollars(r.simulation.p10[-1])),
            ('Lifetime federal tax', lambda r: dollars(r.path.lifetime_tax)),
            ('Chance money lasts', lambda r: f"{round(r.simulation.success * 100)}%"),
        ]
        if not entries[0][1].inputs['partner']:
            measures = [m for m in measures if m[0] != 'Partner retires at']
        rows = [[label] + [measure(result) for _, result in entries]
                for label, measure in measures]
        table = figure_table([''] + [self._name(scenario) for scenario, _ in entries], rows,
                             bold={len(rows) - 1})
        # Each column keyed to its line by a dot beside its name. The name
        # stays in the text colour.
        colors = palette(current_theme())
        for index in range(len(entries)):
            table.horizontalHeaderItem(index + 1).setIcon(
                QIcon(dot_pixmap(colors[LINE_ROLES[index]], 10)))
        frame = card()
        box = QVBoxLayout(frame)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)
        box.addWidget(card_title('The details'))
        box.addWidget(table)
        return frame
