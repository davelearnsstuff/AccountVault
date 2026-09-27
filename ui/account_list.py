# -----------------------------------------------------------------------------
# ui/account_list.py
# The left-hand browser: what is in the list, what each row says, and what the
# tab bar above it offers.
#
# Loading a category, counting what is in the vault, building the tab bar,
# rendering a row, sorting, searching, selecting, and the bulk actions a
# multiple selection offers. Nothing here writes an account field; the two
# things that do write — the bulk close and the bulk category move — go through
# the narrow service calls that touch one column, for the reasons stated there.
#
# See ui/actions.py for why these are mixins.
#
# What this one owns: self.accounts, self.current_category, self.selected_id,
# self.selected_category, the counts, self._category_tabs, self._tab_order,
# self.search_query, self.sort_key, self.sort_descending, and the sidebar-fit
# bookkeeping.
# -----------------------------------------------------------------------------

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidgetItem, QSizePolicy, QVBoxLayout, QWidget,
)

import preferences
from categories import (
    CATEGORY_CONFIG, DUE_CYCLE_KEY, attention_counts, due_date_level, format_date,
    format_money, get_config, has_field, is_favorite, needs_attention,
    next_due_date, normalize_category, searchable_fields, updated_age_level,
    value_noun,
)
from encryption import VaultLockedError
from services import accounts as accounts_service
from ui.actions import _FAILED, _TAB_SHORTCUT_LIMIT
from ui.theme import category_color, level_object_name
from ui.widgets import CategoryDot, CategoryTab, ThemedIcon, clear_layout

ALL = 'all'
CLOSED = 'closed'
# Sentinel tab keys: views rather than category filters, so they can never
# collide with a real (including custom) category name.
DASHBOARD = '__dashboard__'
ATTENTION = '__attention__'
FAVORITES = '__favorites__'

# Tabs that stand for a view across every category rather than one category.
# They get no colour dot, and Add is offered without a category preselected.
AGGREGATE_TABS = (DASHBOARD, ATTENTION, FAVORITES, ALL, CLOSED)

# The aggregate views that are read-only selections out of accounts that
# already exist, so none of them offers an Add button — there is no category
# for a new record to be filed under, and a record created here would not meet
# whatever condition put the view on screen.
NO_ADD_TABS = (CLOSED, ATTENTION, FAVORITES)

SORT_OPTIONS = [('name', 'A–Z'), ('due_date', 'Date Due'), ('last_updated', 'Last Updated')]

# The slack the fitted width adds for the list frame, its scrollbar, and a
# little breathing room after the longest row. The drag range itself comes from
# preferences, which is also what bounds the width read back off disk.
SIDEBAR_MIN = preferences.SIDEBAR_MIN
SIDEBAR_MAX = preferences.SIDEBAR_MAX
_SIDEBAR_SLACK = 22

# Gap between a row's category dot and its name. Also the indent the captions
# below are given, so they line up with the name rather than the dot.
_DOT_GAP = 8

# Gap between the captions folded onto a compact row's single meta line. Wider
# than _DOT_GAP because it is the only thing dividing them now that the '·' is
# gone — at a word's width apart they read as separate captions unaided.
_META_GAP = 14

# The pinned star on a list row. Smaller than the 18px one in the detail pane:
# there it is a control to be clicked, here it is a mark to be noticed, and at
# the row's own size it competed with the name beside it.
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
        so the detail pane is never left empty; callers that are about to select
        a specific record themselves turn it off rather than render twice.

        select is the (id, category) of a row the caller is about to open. It is
        applied before the list is built so the single render highlights it —
        setting it afterwards meant a second full rebuild of every row just to
        move the highlight."""
        self.current_category = category
        self.selected_id = None
        self.selected_category = None
        self.search_query = ''
        self.search_input.blockSignals(True)
        self.search_input.clear()
        self.search_input.blockSignals(False)

        # The sort order is deliberately left alone. It used to be forced back
        # to A–Z here, which meant every category switch — and every save,
        # delete and import, since they all land here — silently undid the sort
        # the user had chosen.

        self._show_placeholder()
        # Closed is a record of things already settled, Needs Attention is a
        # worklist drawn from accounts that already exist, and Favorites is a
        # selection made out of them — none is somewhere a new account can be
        # filed, so none offers Add.
        self.add_button.setVisible(category not in NO_ADD_TABS)

        try:
            # Always pull the full list so category_counts stays accurate for
            # every tab, not just ALL/CLOSED — loading a specific category tab
            # must not leave other tabs' counts (and thus which tabs show at
            # all) stale.
            raw = accounts_service.list_accounts()
            self._recount(raw)
            if category == ATTENTION:
                # Spans every category, like a search does, so each row carries
                # its own category rather than borrowing the current tab's.
                self.accounts = [self._flatten(a) for a in raw if needs_attention(a)]
            elif category == FAVORITES:
                # Closed accounts are listed here too, unlike everywhere else.
                # A pin is the user saying they want this one to hand, and
                # settling an account does not withdraw that — the Closed tab
                # is where a closed account is hidden *from its category*, not
                # from a list somebody built by hand.
                self.accounts = [self._flatten(a) for a in raw if is_favorite(a)]
            elif category in (ALL, CLOSED):
                wanted_closed = category == CLOSED
                self.accounts = [
                    self._flatten(a) for a in raw if bool(a.get('closed')) == wanted_closed
                ]
            else:
                # Closed accounts leave their category for the Closed tab.
                # Listing them in both puts a settled record back among the live
                # ones, which is the one place it should not be.
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
            # The _show_placeholder() above ran before the counts were reloaded,
            # so the state it chose was decided from the previous category's
            # numbers. Now that they are current, choose again.
            self._update_placeholder()

    def _recount(self, raw: list[dict]):
        """The numbers behind the tab bar.

        A closed account is counted by the Closed tab and nowhere else, matching
        what the tabs actually list — a category count that included closed
        records would promise rows the category no longer shows, and a category
        with nothing left open would keep an empty tab on the bar.

        total_count still spans everything, because the only thing that reads it
        is the empty-vault placeholder, and a vault holding closed accounts is
        not empty. open_count is what All lists."""
        self.total_count = len(raw)
        self.open_count = 0
        self.closed_count = 0
        self.favorite_count = sum(1 for account in raw if is_favorite(account))
        self.category_counts = {}
        # Every category in use, closed records included, so a bulk move can
        # still target one that has no open accounts left in it.
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
        """Reloads whatever list is on screen.

        A save or a delete used to call load_category unconditionally, which
        threw away an active search and dropped the user back into a category
        list — so editing a search result meant losing the search."""
        if self.search_query:
            self._run_search(keep_selection=True)
            return
        self.load_category(category or self.current_category)

    # -----------------------------------------------------------------
    # The category bar
    # -----------------------------------------------------------------
    def _category_keys(self) -> list[tuple[str, str]]:
        """(key, label) for every tab to show, in display order. Dashboard leads
        and is always offered; the category tabs follow it."""
        tabs = [(DASHBOARD, 'Dashboard')]
        # Only when there is something in it. A permanent "Needs Attention · 0"
        # is a tab that spends most of its life telling you it has nothing to
        # say, and its whole value is being noticeable when it does appear.
        if self.attention['total']:
            tabs.append((ATTENTION, f"Needs Attention · {self.attention['total']}"))
        # Same rule, for the same reason: a tab that spends most of its life
        # saying it holds nothing is a tab nobody reads. It appears the moment
        # something is pinned and goes again when the last pin is removed.
        if self.favorite_count:
            tabs.append((FAVORITES, f'Favorites · {self.favorite_count}'))
        if self.open_count > 0:
            tabs.append((ALL, 'All'))

        known = [k for k in CATEGORY_CONFIG if self.category_counts.get(k, 0) > 0]
        custom = sorted(
            k for k, count in self.category_counts.items()
            if k not in CATEGORY_CONFIG and k != CLOSED and count > 0
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
        # The dashboard wins whenever it is the visible view: a rebuild can be
        # triggered from under it (a save, a delete) and must not move the
        # highlight onto a list nobody is looking at.
        if self._dashboard_open():
            checked = DASHBOARD
        else:
            # The loaded category may have vanished (e.g. its last account was
            # just deleted or moved out) — the list on screen still shows that
            # category's empty state, so no other tab stands for it either.
            checked = self.current_category
            keys = [key for key, _ in tabs]
            if checked not in keys:
                # Leave the bar unlit rather than highlighting an unrelated
                # tab (like 'All') while the view on screen is still the
                # vanished category.
                checked = None

        self._tab_order = [key for key, _ in tabs]

        for index, (key, label) in enumerate(tabs):
            # Only the real category tabs carry a dot — the aggregate views
            # (Dashboard, All, Closed) stand for no one category, and leaving
            # them bare is what sets them apart from the categories beside them.
            aggregate = key in AGGREGATE_TABS
            button = CategoryTab(label, None if aggregate else category_color(key))
            button.setChecked(key == checked)

            hint = ''
            if key == DASHBOARD:
                hint = 'Net worth, assets and liabilities, and balance trends by category'
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

        # The bar has no stretch of its own, so it must ask for the height the
        # wrapped rows actually need at the current width.
        bar = self._category_row.parentWidget()
        bar.show()
        bar.updateGeometry()

    def _check_current_tab(self):
        """Lights the tab matching the view actually on screen, without
        triggering a load — `clicked` fires on user interaction, not setChecked."""
        if self.search_query and not self._dashboard_open():
            # Results are drawn from every category at once, so no one tab is
            # the view on screen.
            self._clear_tab_highlight()
            return

        key = DASHBOARD if self._dashboard_open() else self.current_category
        button = self._category_tabs.get(key)
        if button is not None:
            button.setChecked(True)
            return

        # No tab stands for the view on screen (an empty vault has no category
        # tabs, not even 'All') — clear the bar instead of leaving the highlight
        # on a view nobody asked for.
        self._clear_tab_highlight()

    def _clear_tab_highlight(self):
        """Leaves the tab bar unlit, for a view no single tab stands for."""
        self.category_buttons.setExclusive(False)
        for button in self.category_buttons.buttons():
            button.setChecked(False)
        self.category_buttons.setExclusive(True)

    def _on_tab_selected(self, key: str):
        """One entry point for the whole tab bar — the dashboard tab and the
        category tabs both land here, so the two views can't disagree about
        which tab should be lit."""
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
            # Same category, just coming back from the dashboard — the list is
            # still loaded, so don't throw away the search box or the selection.
            self._check_current_tab()
            return
        self.load_category(key)

    def _attention_hint(self) -> str:
        """What the Needs Attention tab is offering, in words.

        The figures overlap — an account can be both due and stale — so they are
        listed rather than added up.

        'not updated recently' rather than a number of weeks: how long is too
        long is the category's call now, so there is no one interval left to
        quote here."""
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
                # The row is a stack of labels inside a widget, which a screen
                # reader would otherwise read as an unnamed list entry.
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

    def _empty_text(self) -> str:
        """What an empty list is called. Shared with the detail pane's empty
        state so the two cannot word the same situation differently."""
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
        """One readable line describing a row.

        The row itself is a stack of labels inside a container widget, and a
        QListWidgetItem carrying no text of its own is announced as an unnamed
        entry — the whole list reads as a column of blanks."""
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
        # Named so the stylesheet keeps it transparent; otherwise the global
        # QWidget background paints over the item's selection highlight.
        widget.setObjectName('AccountRow')
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)

        dot = CategoryDot(category_color(category))
        # Tooltip rather than a caption: outside a search the category is
        # already the tab you are standing on, and a row that spells it out
        # every time is a line of text saying what you just clicked.
        dot.setToolTip(config['title'])

        name = QLabel(account.get(config['name_field']) or f"Untitled {config['item_label']}")
        name.setObjectName('RowName')

        title_row = QHBoxLayout()
        title_row.setSpacing(_DOT_GAP)
        title_row.addWidget(dot)
        title_row.addWidget(name, 1)

        # A pinned row carries the same star the detail pane's toggle shows, so
        # the mark that means 'pinned' is one mark. Only the filled one is ever
        # drawn here: an outline star on every unpinned row would put a column
        # of controls down a list that has none.
        if is_favorite(account):
            star = ThemedIcon('star-filled', role='accent', size=_ROW_STAR_PX)
            star.setToolTip('Pinned to Favorites')
            title_row.addWidget(star)

        # The figure the row is consulted for, kept up on the name line and
        # pushed to the far edge by the name's stretch, so a column of rows
        # reads as a column of amounts. Fixed width, unlike the name beside it:
        # the sidebar clips rather than scrolls (see the list's scroll policy),
        # and a name losing its tail is still the account you were looking for
        # where '$1,23' is a different balance from the one on file.
        balance = self._balance_text(account, category)
        if balance:
            amount = QLabel(balance)
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
        """The (text, age level) captions under a row's balance, in display
        order — the dates, and in a search the category the result came from.

        Built as data so the two densities can lay out the same set of captions
        without either one deciding for itself what a row says."""
        meta = []

        if self.search_query:
            # Results span every category, so each row says which one it is
            # from — and which field the query actually hit, unless that was the
            # name, which is already the line above.
            matched = account.get('_match') or {}
            caption = config['title']
            if matched.get('key') not in (None, config['name_field']):
                caption += f" · matched {matched['label']}"
            meta.append((caption, ''))

        if account.get('last_updated'):
            # The category decides how long is too long — a note does not go
            # stale at all, and annual pay is current for a year.
            meta.append((f"Updated: {format_date(account['last_updated'])}",
                         updated_age_level(account['last_updated'], category)))

        cycle = account.get(DUE_CYCLE_KEY)
        due = next_due_date(account.get('due_date'), cycle)
        if due:
            # The occurrence still ahead, not the anchor on file: a monthly card
            # last rolled forward in March should read as due this month, which
            # is what the user would answer if asked.
            level = due_date_level(account.get('due_date'), cycle)
            # A lapsed date is a different sentence, so it gets a different
            # word. 'lapsed' is not in _meta_label's colour map either, which
            # leaves it in the ordinary caption colour by design.
            caption = 'Lapsed' if level == 'lapsed' else 'Due'
            meta.append((f"{caption}: {format_date(due)}", level))
        return meta

    def _meta_block(self, meta: list[tuple[str, str]]):
        """Lays a run of captions out at the current density.

        Compact folds them onto one line; comfortable gives
        each its own, which is what every row did before the setting existed. A
        fully populated row costs three lines that way — a name carrying the
        balance, an updated stamp and a due date — so a screenful of accounts
        was six rows.

        Either way each caption stays its own QLabel, because the age colouring
        is per-caption: a row can be current on one stamp and overdue on the
        other, and one label cannot be two colours."""
        indent = CategoryDot.SIZE + _DOT_GAP

        if self.settings.get('list_density') == 'comfortable':
            block = QVBoxLayout()
            block.setContentsMargins(indent, 0, 0, 0)
            block.setSpacing(2)
            for text, level in meta:
                block.addWidget(self._meta_label(text, level))
            return block

        block = QHBoxLayout()
        # Divided by the gap alone. A '·' between them read as content on a line
        # already made of short fragments, and each caption is self-labelling
        # enough ('Updated:', 'Due:') that nothing has to mark where one ends.
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

        Bare on screen. The '$' already says what kind of number it is, the row
        carries only the one figure, and it sits at the end of the name line
        — so the noun was a word spent saying what the layout had said already.

        labelled is for the accessible summary, which needs it back: read aloud
        in a run of fields there is nothing around the amount to say what it
        measures, and Income's figure is annual pay rather than a balance at
        all. The noun is the category's own, so it matches the detail pane and
        the history table. Categories without the field skip the line even if a
        stale value survives in extra_data from an earlier category."""
        if not has_field(category, 'current_balance'):
            return None

        value = account.get('current_balance')
        if value is None or str(value).strip() == '':
            return None

        amount = format_money(value)
        return f"{value_noun(category)}: {amount}" if labelled else amount

    @staticmethod
    def _meta_label(text: str, level: str) -> QLabel:
        """A small caption under a row's name, tinted by how urgent it is.

        Styled entirely by object name. An inline colour outranks the app
        stylesheet and is baked in at build time, so a row built under one theme
        would keep that theme's amber after a switch — and it left this label
        stuck at 11px while every other size followed the font setting."""
        label = QLabel(text)
        label.setObjectName(level_object_name('RowMeta', level))
        return label

    def _sorted_accounts(self) -> list[dict]:
        # Which key is in force cannot change during the sort, so it is resolved
        # once here rather than re-tested inside the comparison function.
        if self.sort_key == 'name':
            def sort_key(account):
                config = get_config(account.get('_category') or self.current_category)
                return (account.get(config['name_field']) or '').lower()
        elif self.sort_key == 'due_date':
            # Accounts with no due date sort last ascending, which is where
            # "nothing scheduled" belongs. Reversing puts them first; that is
            # the honest consequence of asking for the far end of the same
            # order, not a separate rule.
            def sort_key(account):
                # next_due_date, not the stored anchor: the row displays the
                # occurrence still ahead (see _row_meta), so a monthly card
                # anchored in February sorted under February while showing a
                # date months later — the visible column read out of order.
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
        """Sets the sidebar width, once per unlock.

        A width the user dragged to in an earlier session wins outright — they
        chose it, and a fit measured from whatever rows happen to be on screen
        has no business overriding that. Failing one, the sidebar is fitted to
        its widest row. Either way this runs only on the first populated render,
        so a later category switch — or a drag within this session — is never
        undone."""
        if self._sidebar_fitted:
            return

        stored = preferences.get_sidebar_width()
        if stored is not None:
            self._set_sidebar_width(stored)
            self._sidebar_fitted = True
            # Still the user's width, so it is written back on close and
            # survives however many sessions pass without them touching it.
            self._sidebar_user_set = True
            return

        # An empty view still holds one row — the placeholder, a plain text item
        # with no widget. Measuring that would latch the fit at a width that has
        # nothing to do with the accounts, so wait for a render with real rows.
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
        # Never below what the header and search toolbar need to lay out.
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
            # Ids rather than the items themselves: _show_dirty_prompt spins the
            # event loop, and a list rebuilt underneath it (an idle lock, or the
            # prompt's own Save button) leaves the QListWidgetItems dangling
            # while the ids stay good.
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
        self.detail_stack.setCurrentIndex(1)

    def _select_first_row(self):
        """Opens the first account in the list.

        Goes through setCurrentItem rather than calling _select_account, so the
        row highlight and the detail pane are set by the one path that already
        keeps them in step — including its unsaved-form guard.

        An empty category has a placeholder row instead, which carries no id and
        is skipped, leaving the 'select an account' pane where it belongs.

        Never runs with a form open. Selecting a row while one is dirty raises
        the unsaved-changes prompt, and that is only ever meant to answer a
        deliberate click — clearing the search box mid-edit should not put a
        modal on screen."""
        if self.selected_id is not None or self.form is not None:
            return
        for index in range(self.account_list.count()):
            item = self.account_list.item(index)
            if item.data(Qt.UserRole) is None:
                continue
            self.account_list.setCurrentItem(item)
            return

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
        self.detail_stack.setCurrentIndex(0)
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
            # A category with nothing in it. Closed and Needs Attention offer no
            # Add, matching the toolbar button, which hides there for the same
            # reason.
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

        selected_id is cleared because there is no longer one account on screen:
        leaving it set would let Ctrl+E, Ctrl+B and the copy shortcuts act on a
        record the user can no longer see."""
        self._discard_form()
        self.selected_id = None
        self.selected_category = None

        wanted = set(ids)
        accounts = [a for a in self.accounts if a['id'] in wanted]
        self.bulk_view.show_selection(accounts, self._known_categories())
        self.detail_stack.setCurrentIndex(2)

    def _known_categories(self) -> list[str]:
        """Every category a bulk move could target: the built-ins plus whatever
        custom ones the vault is already using, so a move can land on one the
        user invented without retyping it."""
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
        it — a bulk close run from an open category moves every one of those
        rows out, so the selection is restored where it survives and quietly
        dropped where it does not."""
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
        or delete, where the list has to be refreshed without throwing the user
        out of the record they were just working on."""
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
        if not keep_selection:
            self.selected_id = None
            self._show_placeholder()

        # The counts behind the tab bar describe the vault, not the result set.
        # Refreshed here because a save or a delete made from inside a search
        # comes back through this method and nowhere else: without it the tab
        # counts, the Needs Attention badge and the total_count that
        # _update_placeholder branches on all stayed at whatever load_category
        # last computed, for as long as the search box held text.
        self._recount(raw)
        self._rebuild_category_bar()
        # No category tab stands for a set of results spanning all of them.
        self._clear_tab_highlight()
        self.render_list()

    @staticmethod
    def _match_field(account: dict, needle: str) -> dict | None:
        """The first non-sensitive field whose value contains the query, or None.

        Restricted to searchable_fields so the box cannot be used to confirm a
        secret — see the note there. The category is matched too, since 'banking'
        is a reasonable thing to type, but it is reported only when nothing on
        the record itself matched."""
        category = normalize_category(account.get('category', ''))
        for field in searchable_fields(category):
            value = account.get(field['key'])
            if value is not None and needle in str(value).lower():
                return field
        if needle in category.lower():
            return {'key': 'category', 'label': 'Category'}
        return None

    def _on_sort_changed(self):
        """A new sort key resets the direction to that key's natural one.

        Carrying the old direction over is the wrong guess more often than not —
        switching from Last Updated (newest first) to A–Z would land on Z–A,
        which nobody asks for. The toggle is right there to flip it back."""
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
        """Points the arrow the way the list is actually sorted, and says in
        words what clicking it would do — an arrow alone leaves the user to
        guess whether it shows the current state or the pending action."""
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
