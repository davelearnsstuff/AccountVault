# -----------------------------------------------------------------------------
# ui/dashboard_view.py
# The current balance sheet, net worth over time, a trend card per category,
# and investing against income.
# -----------------------------------------------------------------------------

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QGuiApplication, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.categories import format_money, normalize_category
from core.encryption import VaultLockedError
from services import dashboard as dashboard_service
from services.dashboard import (
    LIABILITY_CATEGORIES, balance_sheet, change_across, composition, latest_value,
    trailing_window,
)
from ui.ranges import (
    DEFAULT_RANGE, basis_text, change_label, month_name, range_buttons,
)
from ui.theme import category_color, palette
from ui.widgets import (
    build_series_chart, clear_layout, dot_pixmap, plain_label, plain_text, selectable,
)

_CARD_COLUMNS = 2

_NET_WORTH = 'Net Worth'
_BALANCES = 'Current Assets & Liabilities'
_TRENDS = 'Trends'
_INVESTING = 'Investing vs. Income'

# Figures in the dashboard's tables, as in the balance history table.
_FIGURE_ALIGN = Qt.AlignRight | Qt.AlignVCenter

# How far an account sits in from the category it is listed under. Spaces, as
# a table item has no indent of its own; copying strips them.
_ACCOUNT_INDENT = ' ' * 6

# The disclosure arrow on a category row, by whether its accounts are showing.
_EXPANDED, _COLLAPSED = '\u25be', '\u25b8'
# The arrow column: room for the arrow and no more, so the names stay close to
# the left edge.
_GUTTER_WIDTH = 26

def _basis_text(range_months: int | None, month_keys: list, start: int) -> str:
    """What a change line is measured from, given the window's month keys and
    the index of its first reading. The window covers the whole range when it
    holds every month of it and its first month has a reading."""
    full = (range_months is not None and start == 0
            and len(month_keys) == range_months + 1)
    return basis_text(range_months, full, month_name(month_keys[start]))


def _share_text(share: float) -> str:
    """A share as a percentage, never rounded down to a bare '0%', which beside
    a real amount reads as a broken figure."""
    percent = share * 100
    if 0 < percent < 0.5:
        return '<1%'
    return f"{percent:.0f}%"


def _rate_text(fraction: float) -> str:
    """A share of income, to one decimal place: whole percents are too coarse
    for a savings rate, where 9.3% and 9.7% are different decisions. Never a
    bare '0.0%' beside a real amount, for the reason _share_text gives."""
    percent = fraction * 100
    if 0 < percent < 0.05:
        return '<0.1%'
    return f"{percent:.1f}%"


def _per_period_text(amount: float, period: str | None) -> str:
    """'$100 every 2 weeks'. The amount alone when no period is set."""
    return f"{format_money(amount)} {period.lower()}" if period else format_money(amount)


class _Card(QFrame):
    """A dashboard card. A category card is clickable and opens that category."""

    def __init__(self, category: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName('WarningBox')  # reuses the bordered-panel style
        self._category = category
        self._activate = None
        if category:
            self.setCursor(Qt.PointingHandCursor)

    def on_activate(self, callback):
        """Makes the card open its category when clicked. Call once its
        content is in place."""
        self._activate = callback
        self.setToolTip(plain_text(f"Show the {self._category} accounts"))
        # Its text is selectable, and a label taking the mouse for that keeps
        # the click from ever reaching the card. So the card watches its
        # labels: a release that selected nothing was a click. The hand stays
        # over the text, since a click there still opens the category.
        for label in self.findChildren(QLabel):
            label.installEventFilter(self)
            label.setCursor(Qt.PointingHandCursor)

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.MouseButtonRelease and self._activate
                and event.button() == Qt.LeftButton and not obj.hasSelectedText()
                and obj.rect().contains(event.position().toPoint())):
            self._activate(self._category)
        return super().eventFilter(obj, event)

    def mouseReleaseEvent(self, event):
        # On release, not press: a press-triggered navigation fires under a
        # click the user started here and dragged out of.
        if (self._activate and event.button() == Qt.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self._activate(self._category)
        super().mouseReleaseEvent(event)


def _cell(text: str, *, figure: bool = False, color: str | None = None) -> QTableWidgetItem:
    """A table cell. Figures sit right-aligned, header and all, so the cents
    line up down a column. A table item never reads its text as HTML, so vault
    content is safe in one as it is."""
    item = QTableWidgetItem(text)
    if figure:
        item.setTextAlignment(_FIGURE_ALIGN)
    if color:
        item.setForeground(QBrush(QColor(color)))
    return item


def _fit(table: QTableWidget):
    """Sizes a table to exactly its contents, so it sits inline in the page
    rather than scrolling or stretching."""
    frame = 2 * table.frameWidth()
    header = table.horizontalHeader()
    table.setFixedWidth(header.length() + frame)
    # A hidden header still reports the height it would have.
    table.setFixedHeight(table.verticalHeader().length()
                         + (0 if header.isHidden() else header.height()) + frame)


def _gutter() -> QTableWidgetItem:
    """A cell of the arrow column. Not selectable, so a copy never carries an
    arrow, or an empty cell where one would be, into a spreadsheet."""
    item = QTableWidgetItem()
    item.setTextAlignment(Qt.AlignCenter)
    item.setFlags(Qt.ItemIsEnabled)
    # The arrows are small glyphs at text size; this makes them a target.
    font = item.font()
    font.setPointSizeF(font.pointSizeF() * 1.4)
    item.setFont(font)
    return item


def _note(text: str) -> QLabel:
    label = selectable(plain_label(text))
    label.setObjectName('Subtle')
    return label


def _copy_cells(table: QTableWidget):
    """The selected cells as tab-separated rows, so they paste into a
    spreadsheet as cells. A gap in the selection is copied as an empty cell,
    keeping the rest in their columns. Plain text, like any text selected by
    hand: these are figures, not secrets, so nothing clears it later."""
    indexes = table.selectedIndexes()
    if not indexes:
        return
    rows = sorted({index.row() for index in indexes})
    columns = sorted({index.column() for index in indexes})
    chosen = {(index.row(), index.column()) for index in indexes}
    lines = []
    for row in rows:
        cells = []
        for column in columns:
            item = table.item(row, column)
            cells.append(item.text().strip() if item and (row, column) in chosen else '')
        lines.append('\t'.join(cells))
    QGuiApplication.clipboard().setText('\n'.join(lines))


class DashboardView(QWidget):
    """Rebuilt from scratch each time it is shown, so it always reflects the
    current balance history."""

    category_activated = Signal(str)
    # (account id, category), from a name in the investing section.
    account_activated = Signal(int, str)

    def __init__(self, theme: str, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._range_months = DEFAULT_RANGE
        # Rebuilds each section the range applies to, from the summary in hand.
        self._redraws = []
        self._data = None
        self._investing = None
        # Each section on the page now, by heading, for the jump links.
        self._sections = {}
        # The categories whose accounts the balance tables are showing. Kept
        # across reloads, so saving an account does not fold them all back up.
        self._expanded = set()

        self._content = QWidget()
        self._body = QVBoxLayout(self._content)
        self._body.setContentsMargins(18, 16, 18, 16)
        self._body.setSpacing(16)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._content)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._scroll)

        # Owned here, shown by whoever frames the view: in the panel's title
        # bar, outside the scroll area, they stay in reach from anywhere on
        # the page.
        self._links = {}
        self.section_links = self._build_section_links()

    # -- jump links ---------------------------------------------------------

    def _build_section_links(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        for title in (_BALANCES, _TRENDS, _INVESTING):
            # Doubled, or a button reads the '&' as a shortcut marker and
            # underlines the letter after it instead.
            link = QPushButton(title.replace('&', '&&'))
            link.setObjectName('Ghost')
            link.setCursor(Qt.PointingHandCursor)
            link.setToolTip(f"Jump to {title}")
            link.setAccessibleName(f"Jump to {title}")
            link.clicked.connect(lambda _=False, t=title: self.scroll_to(t))
            link.hide()
            self._links[title] = link
            layout.addWidget(link)
        return row

    def scroll_to(self, title: str):
        """Brings a section's heading to the top of the view."""
        section = self._sections.get(title)
        if section is None:
            return
        top = self._body.contentsMargins().top()
        self._scroll.verticalScrollBar().setValue(max(0, section.y() - top))

    def _add_section(self, title: str, section: QWidget):
        self._sections[title] = section
        self._body.addWidget(section)

    def _show_links(self):
        """A link for each section on the page, and none when there is only
        one: a link to the only thing on screen goes nowhere."""
        several = len(self._sections) > 1
        for title, link in self._links.items():
            link.setVisible(several and title in self._sections)

    # -- range ------------------------------------------------------------

    def _set_range(self, months: int | None):
        if months == self._range_months:
            return
        self._range_months = months
        # Redraws the sections the range applies to from the summary already
        # in hand, rather than reading the vault again: the range only decides
        # how much of it to show.
        for redraw in self._redraws:
            redraw()

    def _window(self, series: list) -> list:
        """The part of a series the range shows."""
        return trailing_window(series, self._range_months)

    def _ranged(self, build) -> QWidget:
        """What `build` makes, made again whenever the range changes."""
        holder = QWidget()
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(build())

        def redraw():
            clear_layout(box)
            box.addWidget(build())
        self._redraws.append(redraw)
        return holder

    def set_theme(self, theme: str):
        """Records the theme for the next build. Deliberately does not redraw —
        unlike DetailView.set_theme, this one cannot: reload() reads the vault,
        and the theme is applied once at startup while it is still locked. The
        caller rebuilds when the dashboard is actually on screen."""
        self._theme = theme

    def reload(self):
        try:
            self._data = dashboard_service.monthly_summary()
            self._investing = dashboard_service.investing_vs_income()
        except VaultLockedError:
            # Let it out: the caller answers a lock by locking the window.
            raise
        except Exception as exc:
            self._data = None
            self._investing = None
            clear_layout(self._body)
            self._sections = {}
            self._show_links()
            self._body.addWidget(_message(f"Failed to load dashboard: {exc}"))
            return
        self._render()

    def _render(self):
        """Draws the summary currently in hand. Split from reload() so the range
        buttons can redraw without decrypting every balance entry again."""
        clear_layout(self._body)
        self._redraws = []
        self._sections = {}
        self._show_links()
        data = self._data
        if data is None:
            return

        # Built from current figures, not balance history, so it is shown even
        # when there is no history to chart.
        investing = self._investing_section()

        months = data['months']
        if not months:
            self._body.addWidget(_message(
                'No balance history recorded yet. Add balances to accounts to see trends here.'
            ))
            if investing:
                self._add_section(_INVESTING, investing)
            self._body.addStretch(1)
            self._show_links()
            return

        # What the grid orders and captions its cards by.
        parts = composition(data)

        # Current figures, then the trends the range buttons apply to, then the
        # other current figures.
        balances = self._balance_section(balance_sheet(data))
        if balances:
            self._add_section(_BALANCES, balances)
        # A single month has no range to choose.
        buttons = range_buttons(self._range_months, self._set_range) if len(months) > 1 else None
        self._add_section(_TRENDS, _section(
            _TRENDS, self._ranged(lambda: self._trend_grid(data, parts)), buttons))
        if investing:
            self._add_section(_INVESTING, investing)
        self._body.addStretch(1)
        self._show_links()

    # -- sections ----------------------------------------------------------

    def _balance_section(self, sheet) -> QWidget | None:
        """Each side as a table of its categories and their total, then the
        net worth the two totals make. A side with nothing on it is left out.
        Under each category, the accounts its figure is made of: clicking a
        category opens it, and clicking an account opens that account.

        A table per side, not a bar: the shares sit in a column, and a negative
        balance, which no bar can draw, is listed and counted, so the totals
        always come to the net worth beneath them."""
        sides = [('Assets', sheet['assets']), ('Liabilities', sheet['liabilities'])]
        shown = [(title, side) for title, side in sides if side['rows']]
        if not shown:
            return None

        columns = ['Amount', 'Share']
        tables = [self._side_table(title, side, columns) for title, side in shown]

        net_worth = sheet['net_worth']
        danger = palette(self._theme)['danger'] if net_worth < 0 else None
        tables.append(self._sheet(
            ['', _NET_WORTH, *columns], [],
            [_gutter(), _cell(_NET_WORTH),
             _cell(format_money(net_worth), figure=True, color=danger), _cell('')],
            show_header=False, leading=2,
        ))

        # One width per column across all three, so every amount sits in the
        # same column down the card.
        for column in range(2 + len(columns)):
            widest = max(table.columnWidth(column) for table in tables)
            for table in tables:
                table.setColumnWidth(column, widest)
        for table in tables:
            table.horizontalHeader().setMinimumSectionSize(_GUTTER_WIDTH)
            table.setColumnWidth(0, _GUTTER_WIDTH)

        card = _Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        for table in tables:
            _fit(table)
            layout.addWidget(table, 0, Qt.AlignLeft)
        return _section(_BALANCES, card)

    def _side_table(self, title: str, side: dict, columns: list[str]) -> QTableWidget:
        """One side's table: a row per category, with its accounts folded
        beneath it behind the arrow in the first column, then the side's total.
        Clicking the arrow shows or hides the accounts; clicking anywhere else
        on the row opens the category."""
        body = []
        groups = {}  # category row -> (category, the rows of its accounts)
        for row in side['rows']:
            category = row['category']
            head = len(body)
            share = '' if row['share'] is None else _share_text(row['share'])
            body.append((
                lambda c=category: self.category_activated.emit(c),
                [_gutter(), self._category_cell(category),
                 _cell(format_money(row['value']), figure=True),
                 _cell(share, figure=True)],
            ))
            for account in row['accounts']:
                entry = {**account, 'category': category}
                body.append((self._opener(entry), self._account_cells(account, category)))
            if row['accounts']:
                groups[head] = (category, range(head + 1, len(body)))

        def show(head: int):
            category, rows = groups[head]
            expanded = category in self._expanded
            for index in rows:
                table.setRowHidden(index, not expanded)
            arrow = table.item(head, 0)
            arrow.setText(_EXPANDED if expanded else _COLLAPSED)
            # Tooltips guess at HTML; a custom category's name is vault content.
            arrow.setToolTip(plain_text(
                f"{'Hide' if expanded else 'Show'} the {category} accounts here"))

        def toggle(head: int):
            self._expanded ^= {groups[head][0]}
            show(head)
            _fit(table)

        total = [_gutter(), _cell('Total'),
                 _cell(format_money(side['total']), figure=True), _cell('')]
        table = self._sheet(['', title, *columns], body, total, leading=2,
                            toggles={head: (lambda h=head: toggle(h)) for head in groups})
        # Folded only now: _sheet sized the columns with every row showing, so
        # unfolding a category never has to widen the table.
        for head in groups:
            show(head)
        return table

    def _category_cell(self, category: str) -> QTableWidgetItem:
        """A category's name and dot, in the link colour: clicking its row
        opens it, as clicking its trend card does."""
        item = _cell(category, color=palette(self._theme)['accent'])
        item.setIcon(QIcon(dot_pixmap(category_color(category))))
        # Tooltips guess at HTML; a custom category's name is vault content.
        item.setToolTip(plain_text(f"Show the {category} accounts"))
        return item

    def _account_cells(self, account: dict, category: str) -> list[QTableWidgetItem]:
        """An account's row under its category: set in from it, and quieter,
        so the category figures still read as the table's own. Its share is
        of the category, not of the side."""
        subtle = palette(self._theme)['text_secondary']
        name = _cell(_ACCOUNT_INDENT + account['name'], color=subtle)
        # Tooltips guess at HTML; the name is vault content.
        name.setToolTip(plain_text(f"Open {account['name']}"))
        share = _cell('' if account['share'] is None else _share_text(account['share']),
                      figure=True, color=subtle)
        if account['share'] is not None:
            share.setToolTip(plain_text(f"{share.text()} of {category}"))
        return [_gutter(), name,
                _cell(format_money(account['value']), figure=True, color=subtle), share]

    def _investing_section(self) -> QWidget | None:
        """Every income and every contribution as a yearly figure, their totals,
        and the share of income going into investments. None when the vault
        has neither."""
        data = self._investing
        if not data or not (data['income'] or data['contributions']):
            return None

        card = _Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(14)
        layout.addLayout(self._investing_header(data))
        layout.addWidget(self._investing_tables(data))
        return _section(_INVESTING, card)

    @staticmethod
    def _investing_header(data) -> QVBoxLayout:
        """The answer as one sentence, rate included, so it reads as a
        sentence rather than a caption with a number off at the far edge. In
        the cards' heading style, as every other figure on the page is."""
        invested = format_money(data['contribution_total'])
        if data['rate'] is None:
            text = f"Currently investing {invested} a year"
        else:
            text = (f"Currently investing {invested} a year of "
                    f"{format_money(data['income_total'])} income: "
                    f"{_rate_text(data['rate'])}")
        summary = selectable(QLabel(text))
        summary.setObjectName('CardTitle')
        summary.setWordWrap(True)

        header = QVBoxLayout()
        header.setSpacing(4)
        header.addWidget(summary)
        if data['rate'] is None:
            header.addWidget(_note('No income entered to compare it with.'))
        return header

    def _investing_tables(self, data) -> QWidget:
        """Income, then contributions, each as a ruled table, the way the same
        figures would sit in a spreadsheet.

        Each table is as wide as its contents and sits at the left, so the
        figures stay beside the names instead of across a full-screen window
        from them. The name columns share one width, so the two tables start
        their figures at the same place."""
        theme = palette(self._theme)
        warn = theme['level_warn']
        tables = []

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        if data['income']:
            body = []
            for entry in data['income']:
                yearly = (_cell('no pay entered', figure=True, color=warn)
                          if entry['yearly'] is None
                          else _cell(format_money(entry['yearly']), figure=True))
                body.append((self._opener(entry), [self._name_cell(entry), yearly]))
            total = [_cell('Total'), _cell(format_money(data['income_total']), figure=True)]
            tables.append(self._sheet(['Income', 'Yearly'], body, total))
            layout.addWidget(tables[-1], 0, Qt.AlignLeft)
        else:
            layout.addWidget(_note('No Income sources entered.'))

        if data['contributions']:
            body = []
            for entry in data['contributions']:
                yearly = (_cell('needs a period', figure=True, color=warn)
                          if entry['yearly'] is None
                          else _cell(format_money(entry['yearly']), figure=True))
                share = ('' if entry['of_income'] is None
                         else _rate_text(entry['of_income']))
                body.append((self._opener(entry), [
                    self._name_cell(entry),
                    _cell(_per_period_text(entry['amount'], entry['period']), figure=True),
                    yearly,
                    _cell(share, figure=True),
                ]))
            total = [
                _cell('Total'), _cell(''),
                _cell(format_money(data['contribution_total']), figure=True),
                _cell('' if data['rate'] is None else _rate_text(data['rate']), figure=True),
            ]
            tables.append(self._sheet(['Contributions', 'Per period', 'Yearly', '% of income'],
                                      body, total))
            layout.addWidget(tables[-1], 0, Qt.AlignLeft)
        else:
            layout.addWidget(_note('No contribution amounts entered on Investments accounts.'))

        # One name-column width for both, then each table fitted around it.
        widest = max((table.columnWidth(0) for table in tables), default=0)
        for table in tables:
            table.setColumnWidth(0, widest)
            _fit(table)
        return container

    def _name_cell(self, entry: dict) -> QTableWidgetItem:
        """An account's name in the link colour: clicking its row opens it."""
        item = _cell(entry['name'], color=palette(self._theme)['accent'])
        # Tooltips guess at HTML; the name is vault content.
        item.setToolTip(plain_text(f"Open {entry['name']}"))
        return item

    def _opener(self, entry: dict):
        """What clicking an account's row does."""
        return lambda: self.account_activated.emit(entry['id'], entry['category'])

    def _sheet(self, headers: list[str], body: list, total: list, *,
               show_header: bool = True, leading: int = 1,
               toggles: dict | None = None) -> QTableWidget:
        """A read-only table: one row per (activate, cells) in `body`, then a
        bold, shaded total row. Clicking a row calls its activate, except in
        the first column of a row in `toggles`, which calls that instead.
        Headers are still given when hidden: they set the column count. The
        first `leading` columns hold text, and the rest figures."""
        toggles = toggles or {}
        table = QTableWidget(len(body) + 1, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setVisible(show_header)
        for column in range(len(headers)):
            table.horizontalHeaderItem(column).setTextAlignment(
                Qt.AlignLeft | Qt.AlignVCenter if column < leading else _FIGURE_ALIGN)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        # Cells select for copying: drag across them, or Shift- or Ctrl-click.
        # Focus on a click only, so Ctrl+C reaches the table without it
        # becoming a stop when tabbing through the page.
        table.setSelectionMode(QTableWidget.ExtendedSelection)
        table.setFocusPolicy(Qt.ClickFocus)
        copy = QAction('Copy', table)
        copy.setShortcut(QKeySequence.Copy)
        copy.setShortcutContext(Qt.WidgetShortcut)
        copy.triggered.connect(lambda: _copy_cells(table))
        table.addAction(copy)
        table.setContextMenuPolicy(Qt.ActionsContextMenu)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        actions = {}
        for index, (activate, cells) in enumerate(body):
            actions[index] = activate
            for column, item in enumerate(cells):
                table.setItem(index, column, item)

        shade = QBrush(QColor(palette(self._theme)['bg_tertiary']))
        for column, item in enumerate(total):
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            item.setBackground(shade)
            table.setItem(len(body), column, item)

        def open_row(row: int, column: int):
            if column == 0 and row in toggles:
                toggles[row]()
                return
            # A Shift- or Ctrl-click is building a selection, not opening.
            if QApplication.keyboardModifiers() & (Qt.ShiftModifier | Qt.ControlModifier):
                return
            activate = actions.get(row)
            if activate is not None:
                activate()

        def point_at(row: int, _column: int):
            # A hand over a body row only: the total row opens nothing.
            table.viewport().setCursor(
                Qt.PointingHandCursor if row in actions else Qt.ArrowCursor)

        table.cellClicked.connect(open_row)
        table.setMouseTracking(True)
        table.cellEntered.connect(point_at)

        table.resizeColumnsToContents()
        table.resizeRowsToContents()
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Fixed)
        for column in range(len(headers)):
            # Breathing room past the widest entry, as a spreadsheet column has.
            table.setColumnWidth(column, table.columnWidth(column) + 16)
        return table

    def _trend_grid(self, data, parts) -> QWidget:
        """A card for net worth, then one per category, all the same size and
        all over the range in force."""
        container = QWidget()
        grid = QGridLayout(container)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)

        # Which side each category is on, and how much of it, so a card can
        # say what it is worth without the reader measuring a sparkline.
        shares = {
            part['category']: (part['share'], noun)
            for noun in ('assets', 'liabilities')
            for part in parts[noun]['parts']
        }

        # Largest first, so the categories that move the total are on top. Ties
        # break on the name, so the grid does not reshuffle between renders.
        categories = data['categories']
        order = sorted(
            categories,
            key=lambda name: (-abs(latest_value(categories[name]) or 0.0), name),
        )

        # Net worth first: it is the total the rest are parts of.
        cards = [self._net_worth_card(data)]
        cards += [self._category_card(category, categories[category], shares.get(category))
                  for category in order]
        for index, card in enumerate(cards):
            grid.addWidget(card, index // _CARD_COLUMNS, index % _CARD_COLUMNS)

        for column in range(_CARD_COLUMNS):
            grid.setColumnStretch(column, 1)
        return container

    def _net_worth_card(self, data) -> QWidget:
        current = latest_value(data['net_worth'])
        theme = palette(self._theme)
        # Red below zero, as the figure has always been; otherwise the colour
        # its chart is drawn in.
        color = theme['danger'] if current is not None and current < 0 else theme['accent']
        # A caption, as every category card has, so its chart starts level
        # with the one beside it.
        return self._trend_card(_NET_WORTH, 'Assets minus liabilities', data['net_worth'],
                                color, theme['accent'], higher_is_better=True)

    def _category_card(self, category: str, series: list, share=None) -> QWidget:
        liability = normalize_category(category) in LIABILITY_CATEGORIES
        color = category_color(category)

        # The card's share of its side ('38% of liabilities'). A category with no
        # positive value has no share, and is just tagged with its side.
        if share is not None:
            fraction, noun = share
            caption = f"{_share_text(fraction)} of {noun}"
        else:
            caption = 'liability' if liability else ''

        card = self._trend_card(category, caption, series, color, color,
                                higher_is_better=not liability, category=category)
        card.on_activate(self.category_activated.emit)
        return card

    def _trend_card(self, title: str, caption: str, series: list, value_color: str,
                    chart_color: str, *, higher_is_better: bool,
                    category: str | None = None) -> QWidget:
        """One card in the trend grid: its name and latest figure, a caption,
        the change across the range, and the chart. Net worth's card and every
        category's are built here, so they cannot drift apart in size. A card
        given a category opens it when clicked."""
        latest = latest_value(series)

        name = selectable(plain_label(title))
        name.setObjectName('CardTitle')

        # Inline colour on purpose: on a category card it says which category
        # this is, so it is data rather than theme. Weight and size come from
        # the sheet.
        value = selectable(QLabel(format_money(latest) if latest is not None else '—'))
        value.setObjectName('CardValue')
        value.setStyleSheet(f'color: {value_color};')

        # Figures sit beside their names, with the slack after them: pushed to
        # the far edge, a full-screen window put the width of the screen between
        # a figure and what it is a figure of.
        header = QHBoxLayout()
        header.setSpacing(12)
        header.addWidget(name)
        header.addWidget(value)
        header.addStretch(1)

        card = _Card(category)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        layout.addLayout(header)

        # On a line of its own: beside the title it widened every card, and two
        # columns of cards then outgrew the 900px minimum window.
        if caption:
            layout.addWidget(_note(caption))

        months = self._window(self._months())
        window = self._window(series)
        change = _change_label(window, months, self._range_months,
                               higher_is_better=higher_is_better)
        if change:
            layout.addWidget(change)

        chart = build_series_chart(months, window, chart_color, self._theme, 110)
        if chart:
            layout.addWidget(chart)

        return card

    def _months(self) -> list:
        return self._data['months'] if self._data else []


def _section(title: str, content: QWidget, controls: QWidget | None = None) -> QWidget:
    """A heading above its content, laid out the same for every section. Any
    controls sit beside the heading, with the slack after them: pushed to the
    far edge, a full-screen window would put its whole width between them."""
    heading = selectable(QLabel(title))
    heading.setObjectName('SectionHeading')

    row = QHBoxLayout()
    row.setSpacing(12)
    row.addWidget(heading)
    if controls is not None:
        row.addWidget(controls)
    row.addStretch(1)

    container = QWidget()
    outer = QVBoxLayout(container)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(10)
    outer.addLayout(row)
    outer.addWidget(content)
    return container


def _change_label(series: list, month_keys: list, range_months: int | None, *,
                  higher_is_better: bool) -> QLabel | None:
    """The change line under a figure, across the window its section shows.
    `series` and `month_keys` are that window. None with nothing to compare."""
    change = change_across(series)
    if not change:
        return None
    amount, fraction, start = change
    return change_label(amount, fraction, _basis_text(range_months, month_keys, start),
                        higher_is_better=higher_is_better)


def _message(text: str) -> QLabel:
    label = selectable(plain_label(text))
    label.setObjectName('Placeholder')
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignCenter)
    return label

