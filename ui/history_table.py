# -----------------------------------------------------------------------------
# ui/history_table.py
# Balance history for one account: the change and chart across a chosen range,
# the entry table with a per-row edit (which also handles deletion), the
# add-entry row, and the bulk import.
# Exporting is not here: it covers the whole account, so it lives with the
# account's own actions in ui/detail_view.py.
# -----------------------------------------------------------------------------

from functools import partial

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.categories import (
    DEFAULT_VALUE_NOUN, format_date, format_money, format_month, parse_money,
)
from core.encryption import VaultLockedError, is_unreadable
from services import accounts as accounts_service
from services.dashboard import change_across, dated_window
from services.history_io import canonical_amount
from ui.ranges import DEFAULT_RANGE, basis_text, change_label, day_name, range_buttons
from ui.theme import palette
from ui.widgets import build_chart, clear_layout, date_edit

# Figures sit right-aligned, header and all, so the cents line up down a column.
_FIGURE_ALIGN = Qt.AlignRight | Qt.AlignVCenter


def _figure_item(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setTextAlignment(_FIGURE_ALIGN)
    return item


def _amount(row: dict) -> float | None:
    """An entry's figure, or None for one that would not decrypt."""
    try:
        return float(row['balance'])
    except (TypeError, ValueError):
        return None


def _change_text(row: dict, older: dict | None) -> str:
    """The move from the entry below. A dash where there is no step to show:
    the oldest entry, or one filed on the same day as the entry below it. Blank
    where either figure would not decrypt — reaching past it to an older entry
    would pass a longer span off as one step."""
    if older is None or str(row['recorded_at'])[:10] == str(older['recorded_at'])[:10]:
        return '—'
    try:
        change = round(float(row['balance']) - float(older['balance']), 2)
    except (TypeError, ValueError):
        return ''
    # format_money only signs a fall; a rise needs its plus said.
    return f"+{format_money(change)}" if change > 0 else format_money(change)


class BalanceHistory(QWidget):
    """Self-contained: loads, edits, and deletes its own entries, and tells the
    parent when something changed so the surrounding views can refresh.

    An entry dated on or after every other one is what the account currently
    holds, so filing one here updates the account itself — the service does
    that as part of the same write. The parent has to re-read the account on
    `changed`, not just redraw the copy it already had."""

    changed = Signal()
    error = Signal(str)
    # A vault found locked underneath an operation: the window answers it by
    # locking itself, not with an error toast.
    locked = Signal()

    # The range every account's history opens on: the last one picked, for as
    # long as the app runs, so stepping between accounts compares the same
    # window. On the class for that reason, and never saved.
    _range_months = DEFAULT_RANGE

    def __init__(self, account_id: int, theme: str,
                 noun: str = DEFAULT_VALUE_NOUN, parent=None, *,
                 higher_is_better: bool = True):
        super().__init__(parent)
        self._account_id = account_id
        self._theme = theme
        self._noun = noun
        # Which way a change is good news: down for a debt or a bill.
        self._higher_is_better = higher_is_better
        self._editing_id = None
        # The undecryptable-entry notice is said once, not on every reload.
        self._warned_unreadable = False
        # The entries last read, newest first, so a range change redraws from
        # them rather than decrypting every entry again.
        self._rows = []

        # The change line and chart, which the range redraws on their own.
        self._trend = QVBoxLayout()
        self._trend.setSpacing(6)
        self._body = QVBoxLayout()
        self._body.setSpacing(10)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(self._build_heading())
        layout.addLayout(self._trend)
        layout.addLayout(self._body)
        layout.addLayout(self._build_add_row())

        # No reload() here: a lock found during it would be signalled before the
        # caller could connect. See DetailView._build_history.

    # -- heading -----------------------------------------------------------

    def _build_heading(self) -> QHBoxLayout:
        heading = QLabel(f'{self._noun} History')
        heading.setObjectName('SectionHeading')

        # By the heading rather than in the add row, which is for one figure.
        self._import = QPushButton('Import…')
        self._import.setObjectName('Small')
        self._import.setToolTip(
            f'Load past {self._noun.lower()} figures from a CSV file or a '
            'spreadsheet paste'
        )
        self._import.clicked.connect(self._import_entries)

        # Beside the heading, as on the dashboard. Hidden until there is a
        # history to choose a range of.
        self._range_row = range_buttons(self._range_months, self._set_range)
        self._range_row.hide()

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(heading)
        row.addWidget(self._range_row)
        row.addStretch(1)
        row.addWidget(self._import)
        return row

    def _set_range(self, months: int | None):
        if months == self._range_months:
            return
        BalanceHistory._range_months = months
        # Redraws from the entries in hand: the range only decides how many of
        # them the chart shows.
        clear_layout(self._trend)
        self._show_trend()

    def _import_entries(self):
        from ui.dialogs import ImportHistoryDialog

        try:
            existing = accounts_service.list_balance_history(self._account_id)
        except VaultLockedError:
            self.locked.emit()
            return
        except Exception as exc:
            self.error.emit(str(exc))
            return

        # The account's own entries go in, so importing a statement twice adds
        # nothing the second time.
        dialog = ImportHistoryDialog(self, self._noun, existing)
        if dialog.exec() != ImportHistoryDialog.Accepted:
            return
        entries = dialog.entries()
        if not entries:
            return
        self._run(accounts_service.add_balance_entries, self._account_id, entries)

    # -- add row -----------------------------------------------------------

    def _build_add_row(self) -> QHBoxLayout:
        # Capped at today: a figure from the future is a typo, and the newest
        # entry is the one the account takes its current value from.
        self._new_date = date_edit(maximum=QDate.currentDate())
        self._new_date.setToolTip(
            'The date this figure is from. If it is the newest entry on file it '
            f'becomes the current {self._noun.lower()}; an older date only fills '
            'in the history.'
        )

        self._new_amount = QLineEdit()
        self._new_amount.setPlaceholderText('Amount')
        self._new_amount.returnPressed.connect(self._add_entry)

        add = QPushButton('Add Entry')
        add.setObjectName('Small')
        add.setToolTip(
            "Record this figure. An entry dated on or after every other one "
            f"becomes the account's current {self._noun.lower()}."
        )
        add.clicked.connect(self._add_entry)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._new_date)
        row.addWidget(self._new_amount, 1)
        row.addWidget(add)
        return row

    def _add_entry(self):
        amount = self._new_amount.text().strip()
        if not amount:
            return
        parsed = parse_money(amount)
        if parsed is None:
            self.error.emit('Enter a valid amount.')
            return
        self._run(
            accounts_service.add_balance_entry,
            # canonical_amount: a whole figure is stored as 1200, not 1200.0.
            self._account_id, canonical_amount(parsed),
            self._new_date.date().toString('yyyy-MM-dd'),
        )
        self._new_amount.clear()

    # -- table -------------------------------------------------------------

    def reload(self):
        clear_layout(self._trend)
        clear_layout(self._body)
        try:
            rows = accounts_service.list_balance_history(self._account_id)
        except VaultLockedError:
            self.locked.emit()
            return
        except Exception as exc:
            self.error.emit(str(exc))
            return

        self._rows = rows
        # A single entry has no range to choose.
        self._range_row.setVisible(len(rows) >= 2)
        if len(rows) >= 2:
            self._show_trend()

        if not rows:
            empty = QLabel('No history yet.')
            empty.setObjectName('Subtle')
            self._body.addWidget(empty)
            return

        self._body.addWidget(self._build_table(rows))

    def _show_trend(self):
        """The change across the range in force, then its chart. Both start on
        the same entry, so the figure always matches the line."""
        # Rows arrive newest first; a range reads oldest to newest.
        window, full = dated_window(reversed(self._rows), self._range_months)
        values = [_amount(row) for row in window]

        change = change_across(values)
        readings = [index for index, value in enumerate(values) if value is not None]
        if change is None and full and readings:
            # One reading, from before the range began: nothing has been filed
            # since, so the figure has held for the whole range.
            start = readings[0]
            change = (0.0, 0.0 if values[start] else None, start)

        if change:
            amount, fraction, start = change
            basis = basis_text(self._range_months, full and start == 0,
                               day_name(window[start]['recorded_at']))
            self._trend.addWidget(change_label(
                amount, fraction, basis, higher_is_better=self._higher_is_better))

        chart = self._build_chart(window)
        if chart:
            self._trend.addWidget(chart)

    def _build_chart(self, ordered):
        """The entries' chart, `ordered` oldest first. None with fewer than
        two figures to draw."""
        values, labels, tooltips, positions = [], [], [], []
        unreadable = 0
        for row in ordered:
            try:
                values.append(float(row['balance']))
            except (TypeError, ValueError):
                # A figure that would not decrypt cannot be plotted; counted, so
                # the missing point is explained rather than looking like a gap.
                if is_unreadable(row.get('balance')):
                    unreadable += 1
                continue
            stamp = str(row['recorded_at'])[:10]
            labels.append(format_month(stamp))
            tooltips.append(f"{format_date(row['recorded_at'])}: {format_money(row['balance'])}")
            # Day number drives the x axis, so a week's worth of entries occupies a
            # week of chart width rather than the same room as a year of them.
            recorded = QDate.fromString(stamp, 'yyyy-MM-dd')
            positions.append(recorded.toJulianDay() if recorded.isValid() else None)

        if any(p is None for p in positions):
            positions = None
        if unreadable and not self._warned_unreadable:
            self._warned_unreadable = True
            self.error.emit(
                f"{unreadable} {self._noun.lower()} entr"
                f"{'ies' if unreadable != 1 else 'y'} could not be decrypted and "
                f"{'are' if unreadable != 1 else 'is'} missing from the chart."
            )
        return build_chart(values, labels, tooltips,
                           palette(self._theme)['accent'], self._theme, height=140,
                           x_positions=positions)

    def _build_table(self, rows) -> QTableWidget:
        table = QTableWidget(len(rows), 4)
        table.setHorizontalHeaderLabels(['Date', self._noun, 'Change', ''])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.setFocusPolicy(Qt.NoFocus)

        header = table.horizontalHeader()
        for column in (0, 1, 2):
            header.setSectionResizeMode(column, QHeaderView.Stretch)
        # The action column holds widgets, whose width ResizeToContents ignores.
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        for column in (1, 2):
            table.horizontalHeaderItem(column).setTextAlignment(_FIGURE_ALIGN)

        widest = 0
        for index, row in enumerate(rows):
            # Rows arrive newest first, so the entry before this one is below it.
            older = rows[index + 1] if index + 1 < len(rows) else None
            table.setItem(index, 0, QTableWidgetItem(format_date(row['recorded_at']) or ''))
            table.setItem(index, 1, _figure_item(format_money(row['balance'])))
            table.setItem(index, 2, _figure_item(_change_text(row, older)))
            actions = self._row_actions(row)
            widest = max(widest, actions.sizeHint().width(),
                         actions.minimumSizeHint().width())
            table.setCellWidget(index, 3, actions)
        # Slack so the column never squeezes the button below its size hint.
        table.setColumnWidth(3, widest + 10)

        # Size the table to its contents so it sits inline rather than scrolling.
        table.resizeRowsToContents()
        height = sum(table.rowHeight(i) for i in range(table.rowCount()))
        table.setFixedHeight(height + table.horizontalHeader().height() + 4)
        return table

    def _row_actions(self, row) -> QWidget:
        # Edit is the only row action; deleting lives inside the edit dialog so
        # an entry cannot go with one stray click.
        edit = QPushButton('Edit')
        edit.setObjectName('Small')
        edit.setToolTip('Edit or delete this entry')
        edit.clicked.connect(partial(self._edit_entry, dict(row)))
        # A hard minimum, so the column cannot squeeze the label below its text.
        edit.ensurePolished()
        edit.setMinimumWidth(max(
            edit.sizeHint().width(),
            edit.fontMetrics().horizontalAdvance(edit.text()) + 24,
        ) + 2)

        container = QWidget()
        # Named so the stylesheet can pin it transparent, keeping the row's own
        # background and selection showing through the cell widget.
        container.setObjectName('RowActions')
        layout = QHBoxLayout(container)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)
        layout.addWidget(edit)
        return container

    def _edit_entry(self, row: dict):
        from ui.dialogs import BalanceEntryDialog

        dialog = BalanceEntryDialog(self, row['balance'], row['recorded_at'], self._noun)
        if dialog.exec() != BalanceEntryDialog.Accepted:
            return
        if dialog.delete_requested:
            self._run(
                accounts_service.delete_balance_entry, self._account_id, row['id'],
            )
            return
        balance, recorded_at = dialog.values()
        self._run(
            accounts_service.update_balance_entry,
            self._account_id, row['id'], balance, recorded_at,
        )

    def _run(self, operation, *args):
        """Runs a history mutation, then reloads and notifies on success."""
        try:
            operation(*args)
        except VaultLockedError:
            self.locked.emit()
            return
        except Exception as exc:
            self.error.emit(str(exc))
            return
        self.reload()
        self.changed.emit()

