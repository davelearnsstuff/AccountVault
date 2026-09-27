# -----------------------------------------------------------------------------
# ui/history_table.py
# Balance history for one account: a trend chart, the entry table with a per-row
# edit (which also handles deletion), the add-entry row, and the bulk import.
# Exporting is not here: it covers the whole account, so it lives with the
# account's own actions in ui/detail_view.py.
# -----------------------------------------------------------------------------

from functools import partial

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from categories import (
    DEFAULT_VALUE_NOUN, format_date, format_money, format_month, parse_money,
)
from encryption import VaultLockedError, is_unreadable
from services import accounts as accounts_service
from services.history_io import canonical_amount
from ui.theme import palette
from ui.widgets import build_chart, clear_layout, date_edit

# Only the most recent entries are charted — older ones flatten the trend.
_CHART_LIMIT = 24


class BalanceHistory(QWidget):
    """Self-contained: loads, edits, and deletes its own entries, and tells the
    parent when something changed so the surrounding views can refresh.

    An entry dated on or after every other one is what the account currently
    holds, so filing one here updates the account itself — the service does
    that as part of the same write. The parent has to re-read the account on
    `changed`, not just redraw the copy it already had."""

    changed = Signal()
    error = Signal(str)
    # A vault found locked underneath an operation is not a failed operation —
    # the window answers it by locking itself and dropping every decrypted
    # value on screen. Reported as an `error` it became a toast, and the pane
    # went on showing the account until the idle tick happened to catch up.
    locked = Signal()

    def __init__(self, account_id: int, theme: str,
                 noun: str = DEFAULT_VALUE_NOUN, parent=None):
        super().__init__(parent)
        self._account_id = account_id
        self._theme = theme
        self._noun = noun
        self._editing_id = None
        # reload() runs after every add, edit, delete and import, and _build_chart
        # runs with it — so the undecryptable-entry notice below would fire again
        # on each one. Latched here instead: it describes stored data the user
        # cannot repair from this pane, so saying it once is saying it.
        self._warned_unreadable = False

        self._body = QVBoxLayout()
        self._body.setSpacing(10)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(self._build_heading())
        layout.addLayout(self._body)
        layout.addLayout(self._build_add_row())

        # No reload() here, deliberately: it reads the vault and can find it
        # locked, and a signal emitted from a constructor arrives before the
        # caller has had a chance to connect to it — so the first load is the
        # caller's to trigger once it has. See DetailView._build_history.

    # -- heading -----------------------------------------------------------

    def _build_heading(self) -> QHBoxLayout:
        heading = QLabel(f'{self._noun} History')
        heading.setObjectName('SectionHeading')

        # Sits by the heading rather than in the add row: that row is for the
        # one figure being typed now, and a button next to it that silently
        # takes hundreds is not the neighbour it wants.
        self._import = QPushButton('Import…')
        self._import.setObjectName('Small')
        self._import.setToolTip(
            f'Load past {self._noun.lower()} figures from a CSV file or a '
            'spreadsheet paste'
        )
        self._import.clicked.connect(self._import_entries)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(heading)
        row.addStretch(1)
        row.addWidget(self._import)
        return row

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

        # The account's own entries go in so the dialog can drop rows it already
        # holds — importing the same statement twice should be a no-op, not a
        # doubled chart.
        dialog = ImportHistoryDialog(self, self._noun, existing)
        if dialog.exec() != ImportHistoryDialog.Accepted:
            return
        entries = dialog.entries()
        if not entries:
            return
        self._run(accounts_service.add_balance_entries, self._account_id, entries)

    # -- add row -----------------------------------------------------------

    def _build_add_row(self) -> QHBoxLayout:
        # Capped at today for the same reason Quick Update's 'As Of' field is: a
        # figure from the future is a typo, and the newest entry is the one the
        # account takes its current value from — a stray future date would pin
        # the account to the typo until the entry was deleted.
        self._new_date = date_edit(maximum=QDate.currentDate())
        self._new_date.setToolTip(
            'The date this figure is from. Backdating logs it to history '
            f'without changing the current {self._noun.lower()}.'
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
            # canonical_amount, not str(): a whole figure is stored as 1200
            # rather than 1200.0, matching an imported entry and the account's
            # own current_balance. See categories.trim_number.
            self._account_id, canonical_amount(parsed),
            self._new_date.date().toString('yyyy-MM-dd'),
        )
        self._new_amount.clear()

    # -- table -------------------------------------------------------------

    def reload(self):
        clear_layout(self._body)
        try:
            rows = accounts_service.list_balance_history(self._account_id)
        except VaultLockedError:
            self.locked.emit()
            return
        except Exception as exc:
            self.error.emit(str(exc))
            return

        if len(rows) >= 2:
            chart = self._build_chart(rows)
            if chart:
                self._body.addWidget(chart)

        if not rows:
            empty = QLabel('No history yet.')
            empty.setObjectName('Subtle')
            self._body.addWidget(empty)
            return

        self._body.addWidget(self._build_table(rows))

    def _build_chart(self, rows):
        # Rows arrive newest first; the chart reads oldest to newest.
        ordered = list(reversed(rows[:_CHART_LIMIT]))
        values, labels, tooltips, positions = [], [], [], []
        unreadable = 0
        for row in ordered:
            try:
                values.append(float(row['balance']))
            except (TypeError, ValueError):
                # A figure that would not decrypt comes back as the placeholder
                # and cannot be plotted. Counted rather than passed over in
                # silence: the table beside this chart still lists the entry, so
                # a chart quietly one point short reads as a gap in the history
                # rather than as a field that needs repairing.
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
        table = QTableWidget(len(rows), 3)
        table.setHorizontalHeaderLabels(['Date', self._noun, ''])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.setFocusPolicy(Qt.NoFocus)

        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        # The action column holds widgets, whose width ResizeToContents ignores.
        header.setSectionResizeMode(2, QHeaderView.Fixed)

        widest = 0
        for index, row in enumerate(rows):
            table.setItem(index, 0, QTableWidgetItem(format_date(row['recorded_at']) or ''))
            table.setItem(index, 1, QTableWidgetItem(format_money(row['balance'])))
            actions = self._row_actions(row)
            widest = max(widest, actions.sizeHint().width(),
                         actions.minimumSizeHint().width())
            table.setCellWidget(index, 2, actions)
        # Slack so the column never squeezes the button below its size hint.
        table.setColumnWidth(2, widest + 10)

        # Size the table to its contents so it sits inline rather than scrolling.
        table.resizeRowsToContents()
        height = sum(table.rowHeight(i) for i in range(table.rowCount()))
        table.setFixedHeight(height + table.horizontalHeader().height() + 4)
        return table

    def _row_actions(self, row) -> QWidget:
        # Edit is the only row action — deleting lives inside the edit dialog so
        # entries can't be removed with a single stray click.
        edit = QPushButton('Edit')
        edit.setObjectName('Small')
        edit.setToolTip('Edit or delete this entry')
        edit.clicked.connect(partial(self._edit_entry, dict(row)))
        # A cell widget can be squeezed below its size hint by the column width,
        # which clipped the label. A hard minimum — the polished hint, or the
        # text plus the QSS padding, whichever is larger — stops that.
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

