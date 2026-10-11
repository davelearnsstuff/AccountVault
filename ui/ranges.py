# -----------------------------------------------------------------------------
# ui/ranges.py
# The range buttons and change lines shared by the dashboard's trends and an
# account's balance history, so the two cannot drift apart in what a range
# covers or how a change is worded and coloured.
# -----------------------------------------------------------------------------

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QPushButton, QWidget

from core.categories import format_money
from ui.widgets import selectable

# (button, months, what it covers, what its change line is measured from).
# Months of history shown; None means everything recorded. Trailing windows
# rather than calendar years: "the last 6 months" is what someone checking a
# trend actually means. Each window also holds the point it is measured from,
# so "vs last year" compares with this time last year.
RANGES = [
    ('1M',  1,    'Past month',    'vs last month'),
    ('3M',  3,    'Past 3 months', 'vs 3 months ago'),
    ('6M',  6,    'Past 6 months', 'vs 6 months ago'),
    ('1Y',  12,   'Past year',     'vs last year'),
    ('2Y',  24,   'Past 2 years',  'vs 2 years ago'),
    ('All', None, 'All history',   None),
]
# The range a view opens on, named out of RANGES rather than restated as a
# literal beside it.
DEFAULT_RANGE = next(months for label, months, _, _ in RANGES if label == '1Y')
_BASIS = {months: basis for _, months, _, basis in RANGES}

# Fixed English names, as the rest of the interface is, rather than the
# locale's.
_MONTH_NAMES = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

# Past this multiple a percentage ("2846.0%", from a month starting near zero)
# says nothing the amount beside it does not. The change is always shown; only
# the ratio is dropped.
_ABSURD_FRACTION = 10.0


def range_buttons(current: int | None, on_pick) -> QWidget:
    """A toggle per range, with `current` checked. `on_pick` is called with
    the months of the one clicked."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)

    group = QButtonGroup(row)
    group.setExclusive(True)
    for label, months, span, _ in RANGES:
        button = QPushButton(label)
        button.setObjectName('Toggle')
        button.setCheckable(True)
        button.setChecked(months == current)
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(span)
        # '1M' read aloud says nothing about what it shows.
        button.setAccessibleName(span)
        button.clicked.connect(lambda _=False, m=months: on_pick(m))
        group.addButton(button)
        layout.addWidget(button)
    return row


def delta_text(change: float, fraction: float | None, basis: str) -> str:
    arrow = '▲' if change > 0 else '▼' if change < 0 else '■'
    if fraction is None or abs(fraction) > _ABSURD_FRACTION:
        percent = ''
    else:
        percent = f" ({abs(fraction) * 100:.1f}%)"
    return f"{arrow} {format_money(abs(change))}{percent} {basis}"


def delta_object_name(change: float, higher_is_better: bool) -> str:
    """Which way to colour a change. Debts and bills invert: a card balance
    climbing is the same arrow as savings climbing and the opposite outcome."""
    if not change:
        return 'DeltaFlat'
    good = (change > 0) if higher_is_better else (change < 0)
    return 'DeltaGood' if good else 'DeltaBad'


def month_name(key: str) -> str:
    """'2021-03' as 'Mar 2021'. A chart's '03-21' would read as a date in a
    sentence."""
    year, _, month = str(key).partition('-')
    if month.isdigit() and 1 <= int(month) <= 12:
        return f"{_MONTH_NAMES[int(month) - 1]} {year}"
    return str(key)


def day_name(stamp: str) -> str:
    """'2025-03-04' as 'Mar 4, 2025', for a sentence. Anything else is left
    as it came rather than misnamed."""
    parts = str(stamp)[:10].split('-')
    if (len(parts) == 3 and all(part.isdigit() for part in parts)
            and 1 <= int(parts[1]) <= 12):
        year, month, day = parts
        return f"{_MONTH_NAMES[int(month) - 1]} {int(day)}, {year}"
    return str(stamp)


def basis_text(range_months: int | None, full: bool, since: str) -> str:
    """What a change line is measured from. The range's own words only when
    the history reaches back the whole range (`full`); otherwise `since` the
    point it really starts on, rather than "vs last year" across eight months.
    All always names where it starts."""
    if range_months is not None and full:
        return _BASIS[range_months]
    return f"since {since}"


def change_label(change: float, fraction: float | None, basis: str, *,
                 higher_is_better: bool) -> QLabel:
    """The change line under a figure, coloured by whether the move was good
    news. Selectable, as every figure beside it is."""
    label = selectable(QLabel(delta_text(change, fraction, basis)))
    label.setObjectName(delta_object_name(change, higher_is_better))
    return label
