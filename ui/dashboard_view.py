# -----------------------------------------------------------------------------
# ui/dashboard_view.py
# Net worth over time plus a per-category trend card grid.
# -----------------------------------------------------------------------------

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from categories import format_money, normalize_category
from encryption import VaultLockedError
from services import dashboard as dashboard_service
from services.dashboard import (
    LIABILITY_CATEGORIES, composition, latest_value, month_over_month, side_totals,
)
from ui.theme import category_color, palette
from ui.widgets import (
    CategoryDot, CompositionBar, FlowLayout, build_series_chart, clear_layout,
)

_CARD_COLUMNS = 2

# How much history each range button shows, in months. None means everything
# recorded. Trailing windows rather than calendar years: "the last 6 months" is
# what someone checking a trend actually means.
RANGES = [('6M', 6), ('1Y', 12), ('All', None)]
# The range a fresh dashboard opens on, named out of RANGES rather than
# restated as a literal beside it.
DEFAULT_RANGE = dict(RANGES)['1Y']


# Past this multiple the percentage stops informing and starts distracting.
# A month where an account is first recorded, or one that starts near zero,
# produces perfectly correct figures like "2846.0%" that tell the reader nothing
# they did not already get from the amount beside it. The change itself is
# always shown; only the ratio is dropped.
_ABSURD_FRACTION = 10.0


def _delta_text(change: float, fraction: float | None) -> str:
    arrow = '▲' if change > 0 else '▼' if change < 0 else '■'
    if fraction is None or abs(fraction) > _ABSURD_FRACTION:
        percent = ''
    else:
        percent = f" ({abs(fraction) * 100:.1f}%)"
    return f"{arrow} {format_money(abs(change))}{percent} vs last month"


def _share_text(share: float) -> str:
    """A share as a percentage, never rounded down to a bare '0%'.

    A category can be a genuinely tiny fraction of its side — a credit-card
    balance against a mortgage is routinely well under one percent — and '0%'
    printed beside a real amount reads as a broken figure rather than as
    'very small'."""
    percent = share * 100
    if 0 < percent < 0.5:
        return '<1%'
    return f"{percent:.0f}%"


def _delta_object_name(change: float, higher_is_better: bool) -> str:
    """Which way to colour a change.

    Up is not good news everywhere: a credit-card balance climbing is the same
    arrow as savings climbing and the opposite outcome, so liabilities invert.
    A flat month is neither."""
    if not change:
        return 'DeltaFlat'
    good = (change > 0) if higher_is_better else (change < 0)
    return 'DeltaGood' if good else 'DeltaBad'


class _Card(QFrame):
    """A dashboard card. Clickable ones carry the category they stand for.

    The category cards are the natural way into a category — you notice
    something on the dashboard and the next thing you want is the accounts
    behind it — and before this the only route was back up to the tab bar."""

    def __init__(self, category: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName('WarningBox')  # reuses the bordered-panel style
        self._category = category
        self._activate = None
        if category:
            self.setCursor(Qt.PointingHandCursor)

    def on_activate(self, callback):
        self._activate = callback
        self.setToolTip(f"Show the {self._category} accounts")

    def mouseReleaseEvent(self, event):
        # On release, not press: a press-triggered navigation fires under a
        # click the user started here and dragged out of.
        if (self._activate and event.button() == Qt.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self._activate(self._category)
        super().mouseReleaseEvent(event)


class DashboardView(QWidget):
    """Rebuilt from scratch each time it is shown, so it always reflects the
    current balance history."""

    category_activated = Signal(str)

    def __init__(self, theme: str, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._range_months = DEFAULT_RANGE
        self._data = None

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

    # -- range ------------------------------------------------------------

    def _build_range_row(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        heading = QLabel('Showing')
        heading.setObjectName('SectionHeading')
        layout.addWidget(heading)

        group = QButtonGroup(row)
        group.setExclusive(True)
        for label, months in RANGES:
            button = QPushButton(label)
            button.setObjectName('Toggle')
            button.setCheckable(True)
            button.setChecked(months == self._range_months)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _=False, m=months: self._set_range(m))
            group.addButton(button)
            layout.addWidget(button)
        layout.addStretch(1)
        return row

    def _set_range(self, months: int | None):
        if months == self._range_months:
            return
        self._range_months = months
        # Redraws from the summary already in hand rather than reading the vault
        # again — the range only decides how much of it to show.
        self._render()

    def _sliced(self, series: list) -> list:
        if self._range_months is None:
            return series
        return series[-self._range_months:]

    def set_theme(self, theme: str):
        """Records the theme for the next build. Deliberately does not redraw —
        unlike DetailView.set_theme, this one cannot: reload() reads the vault,
        and the theme is applied once at startup while it is still locked. The
        caller rebuilds when the dashboard is actually on screen."""
        self._theme = theme

    def reload(self):
        try:
            self._data = dashboard_service.monthly_summary()
        except VaultLockedError:
            # Let it out: the caller answers a lock by locking the window and
            # dropping the decrypted data. Catching it as a failed load would
            # paint an error card while the rest of the vault stayed on screen —
            # which is the outcome services/dashboard.py re-raises to avoid, and
            # the bare `except Exception` below used to undo.
            raise
        except Exception as exc:
            self._data = None
            clear_layout(self._body)
            self._body.addWidget(_message(f"Failed to load dashboard: {exc}"))
            return
        self._render()

    def _render(self):
        """Draws the summary currently in hand. Split from reload() so the range
        buttons can redraw without decrypting every balance entry again."""
        clear_layout(self._body)
        data = self._data
        if data is None:
            return

        months = data['months']
        if not months:
            self._body.addWidget(_message(
                'No balance history recorded yet. Add balances to accounts to see trends here.'
            ))
            self._body.addStretch(1)
            return

        # The delta is always month-over-month, so it is computed on the full
        # series and never on the visible slice — otherwise picking "6M" would
        # change what "vs last month" meant.
        # Computed once and handed to both sections: the bars draw it, and the
        # grid below orders its cards by it and captions each with its share.
        parts = composition(data)

        # Built first so the net-worth card knows whether its own one-line
        # assets/liabilities summary would just be repeating what the bars are
        # about to say two rows further down.
        #
        # The test is both sides, not whether the card appears at all. A side
        # with no positive balance behind it — an overdrawn account as the only
        # asset — gets no block in that card, and dropping the summary line on
        # the strength of the other side having one would leave that total
        # stated nowhere on the dashboard.
        both_sides = bool(parts['assets']['parts']) and bool(parts['liabilities']['parts'])
        breakdown = self._composition_section(parts)

        self._body.addWidget(self._net_worth_section(data, both_sides))
        # Above the range row on purpose. This is a reading of where things
        # stand now, so unlike everything below it the range does not apply.
        if breakdown:
            self._body.addWidget(breakdown)
        if len(months) > 1:
            self._body.addWidget(self._build_range_row())
        self._body.addWidget(self._category_grid(data['categories'], parts))
        self._body.addStretch(1)

    # -- sections ----------------------------------------------------------

    def _net_worth_section(self, data, composition_shown: bool = False) -> QWidget:
        assets, liabilities = side_totals(data)
        current = latest_value(data['net_worth'])
        theme = palette(self._theme)

        title = QLabel('Net Worth')
        title.setObjectName('SectionHeading')

        # Object name rather than an inline size and colour: the size now
        # follows the font setting like every other heading in the app, and the
        # negative-figure red comes from the active palette instead of being
        # baked in at build time.
        value = QLabel(format_money(current) if current is not None else '—')
        value.setObjectName(
            'NetWorthNegative' if current is not None and current < 0 else 'NetWorthValue'
        )

        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(value)

        # Dropped only when the composition card below is carrying both of
        # these figures already, which is nearly always. It is kept for the case
        # that card cannot cover: composition() draws on positive balances
        # alone, so a side made up entirely of an overdrawn account has no block
        # there, and this line is then the only place its total is stated.
        breakdown = None
        if not composition_shown:
            breakdown = QLabel(
                f"Assets {format_money(assets)}   ·   Liabilities {format_money(liabilities)}"
            )
            breakdown.setObjectName('Subtle')

        card = _Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        layout.addLayout(header)
        if breakdown is not None:
            layout.addWidget(breakdown)

        # Computed on the whole series, never the visible slice: "vs last month"
        # has to mean the same thing whichever range is selected.
        change = month_over_month(data['net_worth'])
        if change:
            layout.addWidget(_delta_label(*change, higher_is_better=True))

        chart = build_series_chart(
            self._sliced(data['months']), self._sliced(data['net_worth']),
            theme['accent'], self._theme, 170,
        )
        if chart:
            layout.addWidget(chart)

        return card

    def _composition_section(self, parts) -> QWidget | None:
        """What the two totals are actually made of.

        Assets and liabilities get a bar each rather than sharing one. They are
        opposite signs, so a single bar carrying both would be measuring
        nothing — 400k of mortgage beside 400k of investments would read as an
        even split of a net worth that is in fact zero. And the question people
        ask is what the debt is made of, or what the savings are, not how the
        two compare as lengths; the figure above already answers that."""
        sides = [('Assets', parts['assets']), ('Liabilities', parts['liabilities'])]
        drawable = [(title, side) for title, side in sides if side['parts']]
        if not drawable:
            return None

        heading = QLabel('Composition')
        heading.setObjectName('SectionHeading')

        card = _Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(16)
        for title, side in drawable:
            layout.addLayout(self._composition_block(title, side))

        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)
        outer.addWidget(heading)
        outer.addWidget(card)
        return container

    def _composition_block(self, title: str, side: dict) -> QVBoxLayout:
        """One side: its total, its bar, and the legend naming every segment."""
        name = QLabel(title)
        name.setObjectName('CardTitle')
        total = QLabel(format_money(side['total']))
        total.setObjectName('CardValue')

        header = QHBoxLayout()
        header.addWidget(name)
        header.addStretch(1)
        header.addWidget(total)

        noun = title.lower()
        bar = CompositionBar(
            [(category_color(part['category']),
              f"{part['category']}: {format_money(part['value'])}"
              f" — {_share_text(part['share'])} of {noun}",
              part['share'])
             for part in side['parts']],
            self._theme,
        )

        block = QVBoxLayout()
        block.setSpacing(8)
        block.addLayout(header)
        block.addWidget(bar)
        block.addWidget(self._legend(side['parts']))
        return block

    @staticmethod
    def _legend(parts) -> QWidget:
        """Names every segment, with its amount and its share.

        Always present, never optional. The bar is the only place these colours
        appear without a word beside them, and on the Light theme three of the
        category colours measure under 3:1 against the card behind them — the
        labels are what make the bar readable there at all, and what keeps
        identity off colour alone for a reader who cannot separate the hues.

        A FlowLayout because category names are long, a custom one can be
        longer, and a fixed row would clip whatever ran past the edge."""
        container = QWidget()
        # Same arrangement the category tab bar uses: the flow decides its own
        # height from the width it is given, and the policy is what lets it.
        policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        policy.setHeightForWidth(True)
        container.setSizePolicy(policy)

        flow = FlowLayout(container, spacing=14)
        flow.setContentsMargins(0, 0, 0, 0)
        for part in parts:
            entry = QWidget()
            row = QHBoxLayout(entry)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            row.addWidget(CategoryDot(category_color(part['category'])))
            label = QLabel(f"{part['category']}   {format_money(part['value'])}"
                           f"   {_share_text(part['share'])}")
            # Text tokens, never the series colour. The dot beside it carries
            # the identity; colouring the words too would leave a wall of
            # saturated text that is harder to read and worse on Light, where
            # these hues are already the low-contrast ones.
            label.setObjectName('Subtle')
            row.addWidget(label)
            flow.addWidget(entry)
        return container

    def _category_grid(self, categories, parts) -> QWidget:
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        heading = QLabel('By Category')
        heading.setObjectName('SectionHeading')
        outer.addWidget(heading)

        grid = QGridLayout()
        grid.setSpacing(12)

        # Which side each category is on, and how much of it, so a card can
        # say what it is worth without the reader measuring a sparkline.
        shares = {
            part['category']: (part['share'], noun)
            for noun in ('assets', 'liabilities')
            for part in parts[noun]['parts']
        }

        # Largest first, not alphabetical. Every card carries the same 110px
        # chart whatever it is drawing, so the grid gave a current account and a
        # mortgage identical weight and then let the alphabet decide which was
        # seen first. Magnitude is answered by the bars above now; what is left
        # for this order to do is put the categories that move the total on top.
        # Ties break on the name, so the grid does not reshuffle between
        # renders when two categories hold the same amount.
        order = sorted(
            categories,
            key=lambda name: (-abs(latest_value(categories[name]) or 0.0), name),
        )

        for index, category in enumerate(order):
            grid.addWidget(
                self._category_card(category, categories[category],
                                    shares.get(category)),
                index // _CARD_COLUMNS, index % _CARD_COLUMNS,
            )

        for column in range(_CARD_COLUMNS):
            grid.setColumnStretch(column, 1)

        outer.addLayout(grid)
        return container

    def _category_card(self, category: str, series: list, share=None) -> QWidget:
        latest = latest_value(series)
        liability = normalize_category(category) in LIABILITY_CATEGORIES
        color = category_color(category)

        name = QLabel(category)
        name.setObjectName('CardTitle')

        # Replaces the bare 'liability' tag this line used to carry. The share
        # names the side too — '38% of liabilities' — so nothing is lost, and
        # the card now states its weight instead of leaving it to be read off a
        # chart drawn at the same size as every other card's.
        #
        # None when the category has no positive value to take a share of (an
        # overdrawn account, a category down to zero), which composition()
        # leaves out of its bar. The old tag stands in, since which side it is
        # on is still worth saying.
        if share is not None:
            fraction, noun = share
            caption = QLabel(f"{_share_text(fraction)} of {noun}")
        else:
            caption = QLabel('liability' if liability else '')
        caption.setObjectName('Subtle')

        # The only inline style left on this card, and deliberately: this
        # colour says *which category* the card is, so it is data rather
        # than theme — the same reason the chart below is drawn in it, and
        # the same colour the category's dot carries in the list and on its
        # tab. Everything a theme owns (weight, size) is in the sheet.
        value = QLabel(format_money(latest) if latest is not None else '—')
        value.setObjectName('CardValue')
        value.setStyleSheet(f'color: {color};')

        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(name)
        header.addStretch(1)
        header.addWidget(value)

        card = _Card(category)
        card.on_activate(self.category_activated.emit)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        layout.addLayout(header)

        # The share gets a line of its own, and the reason is width rather than
        # taste. Every card here is one cell of a two-column grid, so whatever
        # widens a card widens the grid twice over, and the grid already sets
        # the dashboard's minimum width. Beside the title it added its text to
        # the header's minimum; sharing the delta's line it added to that line's
        # instead — either way two columns of it pushed the grid past the 900px
        # window this app calls its minimum, and put a horizontal scrollbar
        # under a dashboard that never needed one. On its own row it is measured
        # against the widest row rather than added to it, which costs one line
        # of height and nothing at all in width.
        if caption.text():
            layout.addWidget(caption)

        change = month_over_month(series)
        if change:
            # A liability climbing is bad news wearing the same arrow as savings
            # climbing, so the colour follows the outcome rather than the sign.
            layout.addWidget(_delta_label(*change, higher_is_better=not liability))

        chart = build_series_chart(
            self._sliced(self._months()), self._sliced(series), color, self._theme, 110,
        )
        if chart:
            layout.addWidget(chart)

        return card

    def _months(self) -> list:
        return self._data['months'] if self._data else []


def _delta_label(change: float, fraction: float | None, *, higher_is_better: bool) -> QLabel:
    """The month-over-month line under a card's figure."""
    label = QLabel(_delta_text(change, fraction))
    label.setObjectName(_delta_object_name(change, higher_is_better))
    return label


def _message(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName('Placeholder')
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignCenter)
    return label

