# -----------------------------------------------------------------------------
# ui/account_list.py
# The left-hand browser: what is in the list, what each row says, and what the
# tab bar above it offers — loading, counting, the tab bar, rows, sorting,
# searching, selecting, and the bulk actions a multiple selection offers.
#
# Nothing here writes an account field; the bulk close and category move go
# through the narrow service calls that touch one column.
#
# See ui/actions.py for why these are mixins. What this one owns:
# self.accounts, self.current_category, self.selected_id,
# self.selected_category, the counts, self._category_tabs, self._tab_order,
# self.search_query, self.sort_key, self.sort_descending, and the sidebar-fit
# bookkeeping.
# -----------------------------------------------------------------------------

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidgetItem, QSizePolicy, QVBoxLayout, QWidget,
)

from core import preferences
from core.categories import (
    CATEGORY_CONFIG, DUE_CYCLE_KEY, attention_counts, due_date_level, format_date,
    format_money, get_config, has_field, is_favorite, needs_attention,
    next_due_date, normalize_category, searchable_fields, updated_age_level,
    value_noun,
)
from core.encryption import VaultLockedError
from services import accounts as accounts_service
from ui.actions import _FAILED, _TAB_SHORTCUT_LIMIT
from ui.theme import category_color, level_object_name
from ui.widgets import (
    CategoryDot, CategoryTab, ThemedIcon, clear_layout, plain_label, plain_text,
)

# Tab keys for the views that are not a single category. Shaped so no real
# category can share one: services.accounts refuses names of the form __x__.
ALL = '__all__'
CLOSED = '__closed__'
DASHBOARD = '__dashboard__'
ATTENTION = '__attention__'
FAVORITES = '__favorites__'

# Tabs that stand for a view across every category. They get no colour dot,
# and Add is offered without a category preselected.
AGGREGATE_TABS = (DASHBOARD, ATTENTION, FAVORITES, ALL, CLOSED)

# Views selected out of existing accounts, with no category for a new record to
# be filed under — so no Add button.
NO_ADD_TABS = (CLOSED, ATTENTION, FAVORITES)

SORT_OPTIONS = [('name', 'A–Z'), ('due_date', 'Date Due'), ('last_updated', 'Last Updated')]

# The sidebar's drag range comes from preferences, which also bounds the width
# read back off disk. The slack covers the list frame, its scrollbar, and a
# little room after the longest row.
SIDEBAR_MIN = preferences.SIDEBAR_MIN
SIDEBAR_MAX = preferences.SIDEBAR_MAX
_SIDEBAR_SLACK = 22

# Gap between a row's category dot and its name, and the indent the captions
# below are given so they line up with the name.
_DOT_GAP = 8

# Gap between the captions folded onto a compact row's single meta line.
_META_GAP = 14

# The pinned star on a list row: a mark to be noticed, smaller than the
# clickable one in the detail pane.
_ROW_STAR_PX = 13


class AccountListMixin:
    """The account list, the tab bar over it, and the search and sort that
    decide what it holds."""

    # -----------------------------------------------------------------
    # Loading
    # -----------------------------------------------------------------
    def load_category(self, category: str, *, select_first: bool = True,
                      select: tuple | None = None):
        """Loads a category into the list. select_first opens its first account
        so the detail pane is never left empty; callers about to select a
        specific record pass `select` instead, (id, category), so the single
        render highlights it."""
        self.current_category = category
        self.selected_id = None
        self.selected_category = None
        self.search_query = ''
        self.search_input.blockSignals(True)
        self.search_input.clear()
        self.search_input.blockSignals(False)

        # The sort order is deliberately left as the user chose it.

        self._show_placeholder()
        self.add_button.setVisible(category not in NO_ADD_TABS)
        self.refresh_button.setVisible(category == ATTENTION)

        try:
            # Always the full list, so every tab's count stays accurate.
            raw = accounts_service.list_accounts()
            self._recount(raw)
            if category == ATTENTION:
                self.accounts = [self._flatten(a) for a in raw if needs_attention(a)]
            elif category == FAVORITES:
                # Closed accounts are listed here too: settling an account does
                # not withdraw the user's pin.
                self.accounts = [self._flatten(a) for a in raw if is_favorite(a)]
            elif category in (ALL, CLOSED):
                wanted_closed = category == CLOSED
                self.accounts = [
                    self._flatten(a) for a in raw if bool(a.get('closed')) == wanted_closed
                ]
            else:
                # Closed accounts leave their category for the Closed tab.
                self.accounts = [
                    self._flatten(a) for a in raw
                    if normalize_category(a.get('category', '')) == category
                    and not a.get('closed')
                ]
        except VaultLockedError:
            self._handle_vault_locked()
            return
        except Exception as exc:
            self.accounts = []
            self.show_toast(str(exc))

        self._rebuild_category_bar()
        self._update_list_title()
        if select is not None:
            self.selected_id, self.selected_category = select
        self.render_list()
        if select_first:
            self._select_first_row()
        if self.selected_id is None:
            # The placeholder above was chosen from the previous counts.
            self._update_placeholder()

    def _recount(self, raw: list[dict]):
        """The numbers behind the tab bar.

        A closed account is counted by the Closed tab alone, matching what the
        tabs list. total_count spans everything: a vault holding only closed
        accounts is not empty."""
        self.total_count = len(raw)
        self.open_count = 0
        self.closed_count = 0
        self.favorite_count = sum(1 for account in raw if is_favorite(account))
        self.category_counts = {}
        # Every category in use, closed records included, so a bulk move can
        # still target one with no open accounts left.
        self.all_categories = set()
        for account in raw:
            key = normalize_category(account['category'])
            self.all_categories.add(key)
            if account.get('closed'):
                self.closed_count += 1
                continue
            self.open_count += 1
            self.category_counts[key] = self.category_counts.get(key, 0) + 1
        self.attention = attention_counts(raw)

    @staticmethod
    def _flatten(account: dict) -> dict:
        """Adds the normalized category the views sort and group by."""
        flat = dict(account)
        flat['_category'] = normalize_category(account.get('category', ''))
        return flat

    def _reload_view(self, category: str | None = None):
        """Reloads whatever list is on screen, keeping an active search."""
        if self.search_query:
            self._run_search(keep_selection=True)
            return
        self.load_category(category or self.current_category)

    def _refresh_attention(self):
        """Re-runs the Needs Attention filter on request. A quick update or a
        touch redraws the record in place rather than reloading, so the account
        just dealt with stays listed until the user asks for this. The account
        on show stays selected if it still needs attention; otherwise the first
        one left is opened."""
        if not self._confirm_leaving_form(('category', ATTENTION, None)):
            return
        self._discard_form()

        account_id, category = self.selected_id, self.selected_category
        self.load_category(ATTENTION, select_first=False, select=(account_id, category))
        if account_id is not None and any(a['id'] == account_id for a in self.accounts):
            self._select_account(account_id, category)
            return

        self.selected_id = None
        self.selected_category = None
        self._select_first_row()
        if self.selected_id is None:
            self._update_placeholder()

    # -----------------------------------------------------------------
    # The category bar
    # -----------------------------------------------------------------
    def _category_keys(self) -> list[tuple[str, str]]:
        """(key, label) for every tab to show, in display order. Dashboard leads
        and is always offered. Needs Attention and Favorites appear only while
        they hold something — a permanent "· 0" tab is one nobody reads."""
        tabs = [(DASHBOARD, 'Dashboard')]
        if self.attention['total']:
            tabs.append((ATTENTION, f"Needs Attention · {self.attention['total']}"))
        if self.favorite_count:
            tabs.append((FAVORITES, f'Favorites · {self.favorite_count}'))
        if self.open_count > 0:
            tabs.append((ALL, 'All'))

        known = [k for k in CATEGORY_CONFIG if self.category_counts.get(k, 0) > 0]
        custom = sorted(
            k for k, count in self.category_counts.items()
            if k not in CATEGORY_CONFIG and count > 0
        )
        tabs += [(key, get_config(key)['title']) for key in known + custom]

        if self.closed_count > 0:
            tabs.append((CLOSED, 'Closed'))
        return tabs

    def _rebuild_category_bar(self):
        for button in self.category_buttons.buttons():
            self.category_buttons.removeButton(button)
        clear_layout(self._category_row)
        self._category_tabs = {}

        tabs = self._category_keys()
        # The dashboard wins whenever it is the visible view.
        if self._dashboard_open():
            checked = DASHBOARD
        else:
            # A category whose last account just went has no tab; leave the bar
            # unlit rather than lighting an unrelated one.
            checked = self.current_category
            keys = [key for key, _ in tabs]
            if checked not in keys:
                checked = None

        self._tab_order = [key for key, _ in tabs]

        for index, (key, label) in enumerate(tabs):
            aggregate = key in AGGREGATE_TABS
            button = CategoryTab(label, None if aggregate else category_color(key))
            button.setChecked(key == checked)

            hint = ''
            if key == DASHBOARD:
                hint = ('Net worth, assets and liabilities, investing against '
                        'income, and balance trends by category')
            elif key == ATTENTION:
                hint = self._attention_hint()
            elif key == FAVORITES:
                hint = (f"{self.favorite_count} pinned account"
                        f"{'s' if self.favorite_count != 1 else ''}, "
                        f"closed ones included")
            else:
                count = self.category_counts.get(key)
                if key == ALL:
                    count = self.open_count
                elif key == CLOSED:
                    count = self.closed_count
                if count:
                    hint = f"{count} account{'s' if count != 1 else ''}"
            if index < _TAB_SHORTCUT_LIMIT:
                hint = f"{hint}  (Ctrl+{index + 1})" if hint else f"Ctrl+{index + 1}"
            if hint:
                button.setToolTip(hint)

            button.clicked.connect(lambda _=False, k=key: self._on_tab_selected(k))
            self.category_buttons.addButton(button)
            self._category_row.addWidget(button)
            self._category_tabs[key] = button

        # The bar has no stretch of its own; it must ask for the height its
        # wrapped rows need.
        bar = self._category_row.parentWidget()
        bar.show()
        bar.updateGeometry()

    def _check_current_tab(self):
        """Lights the tab matching the view on screen, without triggering a load
        — `clicked` fires on user interaction, not setChecked."""
        if self.search_query and not self._dashboard_open():
            # Results span every category; no one tab stands for them.
            self._clear_tab_highlight()
            return

        key = DASHBOARD if self._dashboard_open() else self.current_category
        button = self._category_tabs.get(key)
        if button is not None:
            button.setChecked(True)
            return

        self._clear_tab_highlight()

    def _clear_tab_highlight(self):
        """Leaves the tab bar unlit, for a view no single tab stands for."""
        self.category_buttons.setExclusive(False)
        for button in self.category_buttons.buttons():
            button.setChecked(False)
        self.category_buttons.setExclusive(True)

    def _on_tab_selected(self, key: str):
        """One entry point for the whole tab bar, so the dashboard and the
        category tabs cannot disagree about which tab is lit."""
        if not key:
            self._check_current_tab()
            return

        on_dashboard = self._dashboard_open()

        # Already showing exactly what was asked for.
        if key == DASHBOARD and on_dashboard:
            self._check_current_tab()
            return
        if key != DASHBOARD and key == self.current_category and not on_dashboard:
            self._check_current_tab()
            return

        if self.form is not None and self.form.is_dirty():
            # Put the highlight back until the user resolves the unsaved form.
            self._check_current_tab()
        if not self._confirm_leaving_form(
                ('dashboard' if key == DASHBOARD else 'category', key, None)):
            return

        if key == DASHBOARD:
            self._show_dashboard()
            return

        self._hide_dashboard()
        if key == self.current_category:
            # Back from the dashboard to the list still loaded: keep its search
            # and selection.
            self._check_current_tab()
            return
        self.load_category(key)

    def _attention_hint(self) -> str:
        """What the Needs Attention tab is offering, in words. The figures
        overlap, so they are listed rather than added up."""
        parts = []
        if self.attention['due']:
            parts.append(f"{self.attention['due']} due soon or overdue")
        if self.attention['stale']:
            parts.append(f"{self.attention['stale']} not updated recently")
        if self.attention['lapsed']:
            parts.append(f"{self.attention['lapsed']} with a lapsed due date")
        return ' · '.join(parts) or 'Nothing needs attention'

    def _update_list_title(self):
        if self.current_category == ALL:
            self.list_title.setText('All Accounts')
        elif self.current_category == CLOSED:
            self.list_title.setText('Closed Accounts')
        elif self.current_category == ATTENTION:
            self.list_title.setText('Needs Attention')
        elif self.current_category == FAVORITES:
            self.list_title.setText('Favorites')
        else:
            self.list_title.setText(get_config(self.current_category)['title'])

    # -----------------------------------------------------------------
    # Rendering rows
    # -----------------------------------------------------------------
    def render_list(self):
        self.account_list.blockSignals(True)
        self.account_list.clear()

        rows = self._sorted_accounts()
        if not rows:
            self.account_list.addItem(self._empty_item())
        else:
            for account in rows:
                item = QListWidgetItem(self.account_list)
                widget = self._list_item_widget(account)
                item.setSizeHint(widget.sizeHint())
                item.setData(Qt.UserRole, account['id'])
                item.setData(Qt.UserRole + 1, account.get('_category'))
                # The row is a stack of labels, which a screen reader would
                # otherwise announce as an unnamed entry.
                item.setData(Qt.AccessibleTextRole, self._row_summary(account))
                self.account_list.addItem(item)
                self.account_list.setItemWidget(item, widget)

                if account['id'] == self.selected_id and (
                    self.current_category not in AGGREGATE_TABS
                    or account.get('_category') == self.selected_category
                ):
                    item.setSelected(True)

        self.account_list.blockSignals(False)
        self._fit_sidebar()
        self.detail_view.refresh_navigation()

    def _empty_text(self) -> str:
        """What an empty list is called, shared with the detail pane's empty
        state so the two word it the same."""
        if self.search_query:
            return f'No results for "{self.search_query}"'
        if self.current_category == CLOSED:
            return 'No closed accounts'
        if self.current_category == ATTENTION:
            return 'Nothing needs attention'
        if self.current_category == FAVORITES:
            return 'Nothing pinned yet'
        if self.current_category == ALL:
            return 'No accounts yet'
        return f"No {get_config(self.current_category)['title'].lower()} yet"

    def _empty_item(self) -> QListWidgetItem:
        item = QListWidgetItem(self._empty_text())
        item.setFlags(Qt.NoItemFlags)
        return item

    def _row_summary(self, account: dict) -> str:
        """One readable line describing a row, for screen readers."""
        category = account.get('_category') or self.current_category
        config = get_config(category)
        parts = [
            account.get(config['name_field']) or f"Untitled {config['item_label']}",
            config['title'],
        ]
        balance = self._balance_text(account, category, labelled=True)
        if balance:
            parts.append(balance)
        if is_favorite(account):
            parts.append('pinned')
        if account.get('due_date'):
            parts.append(f"due {format_date(account['due_date'])}")
        if account.get('closed'):
            parts.append('closed')
        return ', '.join(parts)

    def _list_item_widget(self, account: dict) -> QWidget:
        category = account.get('_category') or self.current_category
        config = get_config(category)

        widget = QWidget()
        # Named so the stylesheet keeps it transparent over the selection.
        widget.setObjectName('AccountRow')
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)

        dot = CategoryDot(category_color(category))
        dot.setToolTip(plain_text(config['title']))

        name = plain_label(account.get(config['name_field'])
                           or f"Untitled {config['item_label']}")
        name.setObjectName('RowName')

        title_row = QHBoxLayout()
        title_row.setSpacing(_DOT_GAP)
        title_row.addWidget(dot)
        title_row.addWidget(name, 1)

        # Only the filled star is drawn here; an outline on every unpinned row
        # would be a column of controls down a list that has none.
        if is_favorite(account):
            star = ThemedIcon('star-filled', role='accent', size=_ROW_STAR_PX)
            star.setToolTip('Pinned to Favorites')
            title_row.addWidget(star)

        # The balance at the far end of the name line, at a fixed width: the
        # sidebar clips rather than scrolls, and a clipped name is still the
        # account while a clipped '$1,23' is a different balance.
        balance = self._balance_text(account, category)
        if balance:
            amount = plain_label(balance)
            amount.setObjectName('RowBalance')
            amount.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
            title_row.addWidget(amount)
        layout.addLayout(title_row)

        meta = self._row_meta(account, config, category)
        if meta:
            layout.addLayout(self._meta_block(meta))
        return widget

    def _row_meta(self, account: dict, config: dict,
                  category: str) -> list[tuple[str, str]]:
        """The (text, age level) captions under a row's name, in display order:
        the dates, and in a search the category the result came from."""
        meta = []

        if self.search_query:
            # Results span every category, so each says which one it is from —
            # and which field the query hit, unless that was the name.
            matched = account.get('_match') or {}
            caption = config['title']
            if matched.get('key') not in (None, config['name_field']):
                caption += f" · matched {matched['label']}"
            meta.append((caption, ''))

        if account.get('last_updated'):
            meta.append((f"Updated: {format_date(account['last_updated'])}",
                         updated_age_level(account['last_updated'], category)))

        cycle = account.get(DUE_CYCLE_KEY)
        due = next_due_date(account.get('due_date'), cycle)
        if due:
            # The occurrence still ahead, not the anchor on file.
            level = due_date_level(account.get('due_date'), cycle, category)
            # A lapsed date gets its own word, and the ordinary caption colour.
            caption = 'Lapsed' if level == 'lapsed' else 'Due'
            meta.append((f"{caption}: {format_date(due)}", level))
        return meta

    def _meta_block(self, meta: list[tuple[str, str]]):
        """Lays the captions out at the current density: one line when compact,
        one each when comfortable. Each caption is its own label because the age
        colouring is per caption."""
        indent = CategoryDot.SIZE + _DOT_GAP

        if self.settings.get('list_density') == 'comfortable':
            block = QVBoxLayout()
            block.setContentsMargins(indent, 0, 0, 0)
            block.setSpacing(2)
            for text, level in meta:
                block.addWidget(self._meta_label(text, level))
            return block

        block = QHBoxLayout()
        block.setContentsMargins(indent, 0, 0, 0)
        block.setSpacing(_META_GAP)
        for text, level in meta:
            block.addWidget(self._meta_label(text, level))
        block.addStretch(1)
        return block

    @staticmethod
    def _balance_text(account: dict, category: str,
                      labelled: bool = False) -> str | None:
        """The row's balance, or None when the account hasn't got one.

        Bare on screen; labelled with the category's own noun for the accessible
        summary, where nothing around it says what it measures. Categories
        without the field skip it even if a stale value survives in extra_data."""
        if not has_field(category, 'current_balance'):
            return None

        value = account.get('current_balance')
        if value is None or str(value).strip() == '':
            return None

        amount = format_money(value)
        return f"{value_noun(category)}: {amount}" if labelled else amount

    @staticmethod
    def _meta_label(text: str, level: str) -> QLabel:
        """A small caption under a row's name, tinted by how urgent it is —
        by object name, so the colour follows the theme."""
        label = plain_label(text)
        label.setObjectName(level_object_name('RowMeta', level))
        return label

    def _sorted_accounts(self) -> list[dict]:
        if self.sort_key == 'name':
            def sort_key(account):
                config = get_config(account.get('_category') or self.current_category)
                return (account.get(config['name_field']) or '').lower()
        elif self.sort_key == 'due_date':
            # No due date sorts last ascending; reversing puts those first.
            def sort_key(account):
                # The occurrence the row displays, not the stored anchor.
                return next_due_date(account.get('due_date'),
                                     account.get(DUE_CYCLE_KEY)) or '9999-99-99'
        else:
            def sort_key(account):
                return account.get('last_updated') or ''

        return sorted(self.accounts, key=sort_key, reverse=self.sort_descending)

    # -----------------------------------------------------------------
    # Sidebar width
    # -----------------------------------------------------------------
    def _fit_sidebar(self):
        """Sets the sidebar width, once per unlock: the width the user last
        dragged to if there is one, else fitted to the widest row. Later
        category switches never undo it."""
        if self._sidebar_fitted:
            return

        stored = preferences.get_sidebar_width()
        if stored is not None:
            self._set_sidebar_width(stored)
            self._sidebar_fitted = True
            # Still the user's width, so it is written back on close.
            self._sidebar_user_set = True
            return

        # An empty view holds only the placeholder item, which has no widget;
        # wait for a render with real rows.
        widgets = [
            self.account_list.itemWidget(self.account_list.item(index))
            for index in range(self.account_list.count())
        ]
        widgets = [widget for widget in widgets if widget is not None]
        if not widgets:
            return

        widest = max(widget.sizeHint().width() for widget in widgets)

        margins = self.list_panel.layout().contentsMargins()
        target = widest + margins.left() + margins.right() + _SIDEBAR_SLACK
        # Never below what the header and search toolbar need.
        target = max(target, self.list_panel.minimumSizeHint().width())
        self._set_sidebar_width(target)
        self._sidebar_fitted = True

    def _set_sidebar_width(self, width: int):
        width = max(SIDEBAR_MIN, min(SIDEBAR_MAX, width))
        total = self.splitter.width() or self.width()
        self.splitter.setSizes([width, max(1, total - width)])

    # -----------------------------------------------------------------
    # Selection
    # -----------------------------------------------------------------
    def _on_selection_changed(self):
        items = [i for i in self.account_list.selectedItems()
                 if i.data(Qt.UserRole) is not None]
        if not items:
            return

        if len(items) > 1:
            # Ids, not items: the dirty-form prompt spins the event loop, and a
            # rebuilt list would leave the items dangling.
            ids = [i.data(Qt.UserRole) for i in items]
            if self._confirm_leaving_form(('bulk', ids, None)):
                self._show_bulk(ids)
            return

        account_id = items[0].data(Qt.UserRole)
        category = items[0].data(Qt.UserRole + 1)
        if self._confirm_leaving_form(('select', account_id, category)):
            self._select_account(account_id, category)

    def _select_account(self, account_id, category):
        self.selected_id = account_id
        self.selected_category = category
        account = next((a for a in self.accounts if a['id'] == account_id), None)
        if account is None:
            return
        self._discard_form()
        self.detail_view.show_account(account)
        self.detail_stack.setCurrentWidget(self._detail_scroll)

    def _select_first_row(self):
        """Opens the first account in the list, through setCurrentItem so the
        highlight and the detail pane move together.

        Never with a form open: selecting a row then would raise the
        unsaved-changes prompt, which should only ever answer a deliberate
        click."""
        if self.selected_id is not None or self.form is not None:
            return
        for index in range(self.account_list.count()):
            item = self.account_list.item(index)
            if item.data(Qt.UserRole) is None:
                continue
            self.account_list.setCurrentItem(item)
            return

    def _account_rows(self) -> list[int]:
        """Indexes of the list rows that are accounts, skipping the empty-state
        placeholder."""
        return [index for index in range(self.account_list.count())
                if self.account_list.item(index).data(Qt.UserRole) is not None]

    def _selected_row(self, rows: list[int]) -> int | None:
        """Where the account on show sits among rows, or None if it is not in
        the list."""
        for position, index in enumerate(rows):
            item = self.account_list.item(index)
            if item.data(Qt.UserRole) == self.selected_id and (
                self.current_category not in AGGREGATE_TABS
                or item.data(Qt.UserRole + 1) == self.selected_category
            ):
                return position
        return None

    def _navigation_state(self) -> tuple[bool, bool]:
        """(has_previous, has_next) for the detail pane's arrows."""
        rows = self._account_rows()
        position = self._selected_row(rows)
        if position is None:
            return False, False
        return position > 0, position < len(rows) - 1

    def _step_account(self, step: int):
        """Opens the account `step` rows away in the list as shown, through the
        same path a click takes."""
        rows = self._account_rows()
        position = self._selected_row(rows)
        if position is None or not 0 <= position + step < len(rows):
            return
        item = self.account_list.item(rows[position + step])
        self.account_list.setCurrentItem(item)
        self.account_list.scrollToItem(item)

    def _clear_selection(self):
        self.account_list.blockSignals(True)
        self.account_list.clearSelection()
        self.account_list.blockSignals(False)
        self.selected_id = None
        self.selected_category = None
        self._show_placeholder()

    def _restore_selection(self):
        """Puts the list selection back on the account being edited."""
        self.account_list.blockSignals(True)
        for index in range(self.account_list.count()):
            item = self.account_list.item(index)
            item.setSelected(item.data(Qt.UserRole) == self.selected_id)
        self.account_list.blockSignals(False)

    def _show_placeholder(self):
        self.detail_stack.setCurrentWidget(self.placeholder)
        self.detail_view.clear()
        self._update_placeholder()

    def _update_placeholder(self):
        """Picks which empty state the detail pane should be showing."""
        if self.search_query:
            if self.accounts:
                self.placeholder.show_prompt()
            else:
                self.placeholder.show_no_results(self.search_query)
        elif self.total_count == 0:
            self.placeholder.show_empty_vault()
        elif not self.accounts:
            self.placeholder.show_empty_list(
                self._empty_text(),
                can_add=self.current_category not in NO_ADD_TABS,
            )
        else:
            self.placeholder.show_prompt()

    # -----------------------------------------------------------------
    # Bulk actions
    # -----------------------------------------------------------------
    def _show_bulk(self, ids):
        """Swaps the detail pane for the multi-selection actions.

        selected_id is cleared so Ctrl+E and the copy shortcuts cannot act on a
        record that is no longer on screen."""
        self._discard_form()
        self.selected_id = None
        self.selected_category = None

        wanted = set(ids)
        accounts = [a for a in self.accounts if a['id'] in wanted]
        self.bulk_view.show_selection(accounts, self._known_categories())
        self.detail_stack.setCurrentWidget(self._bulk_scroll)

    def _known_categories(self) -> list[str]:
        """Every category a bulk move could target: the built-ins plus the
        custom ones the vault is already using."""
        custom = sorted(
            key for key in self.all_categories
            if key and key not in CATEGORY_CONFIG
        )
        return list(CATEGORY_CONFIG) + custom

    def _selected_ids(self) -> list[int]:
        return [
            item.data(Qt.UserRole)
            for item in self.account_list.selectedItems()
            if item.data(Qt.UserRole) is not None
        ]

    def _bulk_set_closed(self, closed: bool):
        ids = self._selected_ids()
        if not ids:
            return
        changed = self._attempt(accounts_service.set_closed, ids, closed)
        if changed is _FAILED:
            return

        word = 'closed' if closed else 'reopened'
        self.show_toast(f"{changed} account{'s' if changed != 1 else ''} {word}.")
        self._after_bulk(ids)

    def _bulk_set_category(self, category: str):
        ids = self._selected_ids()
        if not ids:
            return
        changed = self._attempt(accounts_service.set_category, ids, category)
        if changed is _FAILED:
            return

        self.show_toast(
            f"{changed} account{'s' if changed != 1 else ''} moved to {category}."
        )
        self._after_bulk(ids)

    def _after_bulk(self, ids: list[int]):
        """Reloads the view and puts the selection back on whatever is still in
        it — a bulk close from a category moves every row out."""
        self._reload_view()

        remaining = [i for i in ids if any(a['id'] == i for a in self.accounts)]
        if not remaining:
            self._clear_selection()
            return

        self.account_list.blockSignals(True)
        for index in range(self.account_list.count()):
            item = self.account_list.item(index)
            item.setSelected(item.data(Qt.UserRole) in remaining)
        self.account_list.blockSignals(False)
        self._on_selection_changed()

    # -----------------------------------------------------------------
    # Search and sort
    # -----------------------------------------------------------------
    def _on_search_typed(self, text: str):
        self.search_query = text.strip()
        self._search_timer.start(250)

    def _run_search(self, keep_selection: bool = False):
        """Searches every category. keep_selection is for re-running after a save
        or delete without throwing the user out of the record they were on."""
        if not self.search_query:
            self.load_category(self.current_category)
            return

        needle = self.search_query.lower()
        raw = self._attempt(accounts_service.list_accounts)
        if raw is _FAILED:
            return

        matches = []
        for account in raw:
            matched = self._match_field(account, needle)
            if matched is None:
                continue
            flat = self._flatten(account)
            # Carried on the row so the list can say why this result is here.
            flat['_match'] = matched
            matches.append(flat)

        self.accounts = matches
        # The results are not the Needs Attention list, whatever tab they were
        # typed from; clearing the search brings it back.
        self.refresh_button.hide()
        if not keep_selection:
            self.selected_id = None
            self._show_placeholder()

        # The tab counts describe the vault, not the results; a save made from
        # inside a search returns through here, so refresh them too.
        self._recount(raw)
        self._rebuild_category_bar()
        self._clear_tab_highlight()
        self.render_list()

    @staticmethod
    def _match_field(account: dict, needle: str) -> dict | None:
        """The first searchable field whose value contains the query, or None.

        Restricted to searchable_fields so the box cannot confirm a secret. The
        category is matched too, but reported only when nothing on the record
        itself matched."""
        category = normalize_category(account.get('category', ''))
        for field in searchable_fields(category):
            value = account.get(field['key'])
            if value is not None and needle in str(value).lower():
                return field
        if needle in category.lower():
            return {'key': 'category', 'label': 'Category'}
        return None

    def _on_sort_changed(self):
        """A new sort key resets the direction to that key's natural one —
        switching from Last Updated to A–Z should not land on Z–A."""
        self.sort_key = self.sort_combo.currentData()
        self.sort_descending = preferences.SORT_DEFAULT_DESCENDING.get(self.sort_key, False)
        preferences.save_sort_key(self.sort_key)
        preferences.save_sort_descending(self.sort_descending)
        self._sync_sort_direction()
        self.render_list()

    def _toggle_sort_direction(self):
        self.sort_descending = not self.sort_descending
        preferences.save_sort_descending(self.sort_descending)
        self._sync_sort_direction()
        self.render_list()

    def _sync_sort_direction(self):
        """Points the arrow the way the list is sorted, and says in words what
        clicking it would do."""
        label = dict(SORT_OPTIONS).get(self.sort_key, '')
        if self.sort_descending:
            self.sort_direction.set_icon_name('arrow-up')
            hint = f"{label}, reversed — click for normal order"
        else:
            self.sort_direction.set_icon_name('arrow-down')
            hint = f"{label}, normal order — click to reverse"
        self.sort_direction.setToolTip(hint)
        self.sort_direction.setAccessibleName(hint)

    def _apply_saved_sort(self):
        """Restores the stored sort order into both the field and the combo."""
        self.sort_key = preferences.get_sort_key()
        self.sort_descending = preferences.get_sort_descending(self.sort_key)
        self.sort_combo.blockSignals(True)
        index = self.sort_combo.findData(self.sort_key)
        self.sort_combo.setCurrentIndex(max(0, index))
        self.sort_combo.blockSignals(False)
        self._sync_sort_direction()
