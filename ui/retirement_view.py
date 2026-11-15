# -----------------------------------------------------------------------------
# ui/retirement_view.py
# The Retirement tab: its view buttons, the sample-household banner, and the
# pages. The result pages are in retirement_pages.py, What-if and Scenarios in
# retirement_whatif.py, and Your details in retirement_details.py.
#
# Holds the plan and the accounts' figures while the vault is open, and none
# of it after a lock: clear() drops both. The plan is simulated when a page
# that shows it opens and something has changed since, not on every edit. A
# simulation takes a few tenths of a second, and Your details saves on every
# field. The next best moves take a few dozen simulations, so they are worked
# out one per turn of the event loop and the window stays responsive.
# -----------------------------------------------------------------------------

import copy
import secrets

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QHBoxLayout, QPushButton, QStackedWidget, QVBoxLayout,
    QWidget,
)

from core import retirement
from core.encryption import VaultLockedError
from services import retirement as retirement_service
from ui.retirement_details import DetailsPage
from ui.retirement_pages import PlanPage, ProjectionPage, WithdrawalsPage, card, compute
from ui.retirement_whatif import ScenariosPage, WhatIfPage
from ui.widgets import plain_label

PLAN = 'Plan'
PROJECTION = 'Projection'
WHAT_IF = 'What-if'
SCENARIOS = 'Scenarios'
WITHDRAWALS = 'Withdrawals & taxes'
DETAILS = 'Your details'
VIEWS = (PLAN, PROJECTION, WHAT_IF, SCENARIOS, WITHDRAWALS, DETAILS)

# The pages that show whichever household is on show: the sample's while the
# plan cannot run, or while the sample is asked for. Scenarios always compares
# against the user's own plan, and Your details always edits it.
SAMPLE_VIEWS = (PLAN, PROJECTION, WHAT_IF, WITHDRAWALS)

# What each missing detail is called in a request for it.
MISSING_NAMES = {
    'birth_year': 'your birth month and year',
    'partner_birth_year': "your partner's birth month and year",
    'spending': 'your yearly spending',
}

# Which page a next best move opens, and what it sets there.
def _whose_retirement(move: dict) -> str:
    return 'partner_retire_age' if move.get('partner') else 'retire_age'


MOVE_LEVERS = {
    'work_longer': lambda move: {_whose_retirement(move): move['retire_age']},
    'retire_earlier': lambda move: {_whose_retirement(move): move['retire_age']},
    'spend_less': lambda move: {'spending': move['spending']},
    'delay_social_security': lambda move: {'ss_age': 70},
}
MOVE_PAGES = {'start_conversions': WITHDRAWALS, 'conversions': WITHDRAWALS,
              'health_until_medicare': DETAILS}


def missing_text(missing: list) -> str:
    """'your birth month and year and your yearly spending', from the keys
    retirement.missing_details lists. A month is asked for with its year."""
    names = [MISSING_NAMES[key] for key in missing if key in MISSING_NAMES]
    if not names:
        names = [MISSING_NAMES['birth_year']]
    if len(names) == 1:
        return names[0]
    return ', '.join(names[:-1]) + (',' if len(names) > 2 else '') + ' and ' + names[-1]


class RetirementView(QWidget):
    """The tab's content. Its view buttons are shown by whoever frames it, in
    the panel's title bar, as the dashboard's jump links are."""

    account_activated = Signal(int, str)
    # A save found the vault locked; the window locks itself in answer.
    locked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plan = None
        self._savings = None
        # Whether the sample household is on show because it was asked for.
        # It is also shown, without asking, while the plan cannot run yet.
        self._sample = False
        self._stale = True
        self._scenarios_stale = True
        self._result = None
        self._sample_result = None
        # Results by inputs, so reopening Scenarios does not simulate the
        # unchanged ones again.
        self._scenario_results = {}

        # The next best moves search: its generator, and the tick that steps it.
        self._moves = None
        self._moves_timer = QTimer(self)
        self._moves_timer.setInterval(0)
        self._moves_timer.timeout.connect(self._step_moves)

        self._buttons = {}
        self.view_buttons = self._build_view_buttons()

        self._banner_text = plain_label()
        self._banner_text.setWordWrap(True)
        self._banner_button = QPushButton()
        self._banner_button.clicked.connect(self._banner_clicked)
        self._banner = card()
        banner_row = QHBoxLayout(self._banner)
        banner_row.setContentsMargins(14, 10, 14, 10)
        banner_row.setSpacing(12)
        banner_row.addWidget(self._banner_text, 1)
        banner_row.addWidget(self._banner_button)
        self._banner.hide()

        plan_page, whatif, scenarios = PlanPage(), WhatIfPage(), ScenariosPage()
        withdrawals = WithdrawalsPage()
        self._pages = {PLAN: plan_page, PROJECTION: ProjectionPage(), WHAT_IF: whatif,
                       SCENARIOS: scenarios, WITHDRAWALS: withdrawals}
        for page in (plan_page, self._pages[PROJECTION], withdrawals):
            page.details_requested.connect(lambda: self.show_view(DETAILS))
        plan_page.move_chosen.connect(self._try_move)
        withdrawals.conversions_changed.connect(self._set_conversions)
        whatif.applied.connect(self._apply_levers)
        whatif.save_requested.connect(self._save_scenario)
        scenarios.adopt_requested.connect(self._adopt_scenario)
        scenarios.delete_requested.connect(self._delete_scenario)
        scenarios.whatif_requested.connect(lambda: self.show_view(WHAT_IF))
        # Built the first time the tab opens: it is a page of several dozen
        # inputs, and most sessions never look at it.
        self._details_page = None

        self._stack = QStackedWidget()
        for page in self._pages.values():
            self._stack.addWidget(page)

        banner_holder = QVBoxLayout()
        banner_holder.setContentsMargins(18, 4, 18, 0)
        banner_holder.addWidget(self._banner)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(banner_holder)
        layout.addWidget(self._stack, 1)
        self._buttons[PLAN].setChecked(True)

    def _build_view_buttons(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        group = QButtonGroup(row)
        group.setExclusive(True)
        for name in VIEWS:
            # Doubled, or the button reads '&' as a shortcut marker.
            button = QPushButton(name.replace('&', '&&'))
            button.setObjectName('Toggle')
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _=False, n=name: self.show_view(n))
            group.addButton(button)
            layout.addWidget(button)
            self._buttons[name] = button
        return row

    @property
    def _details(self) -> DetailsPage:
        if self._details_page is None:
            page = DetailsPage()
            page.changed.connect(self._on_changed)
            page.sample_requested.connect(self._show_sample)
            page.account_activated.connect(self.account_activated.emit)
            self._stack.addWidget(page)
            self._details_page = page
        return self._details_page

    # -- state ------------------------------------------------------------

    def reload(self):
        """Reads the plan and the accounts' figures afresh. VaultLockedError
        is let out, for the window to answer by locking."""
        self._plan = retirement_service.load_plan()
        self._savings = retirement_service.savings_from_accounts()
        self._details.load(self._plan, self._savings)
        self._stale = self._scenarios_stale = True
        self._refresh_current()

    def clear(self):
        """Drops the plan, the figures and every result: all vault content.
        The tab opens on Plan again next time."""
        self._stop_moves()
        self._plan = None
        self._savings = None
        self._sample = False
        self._stale = self._scenarios_stale = True
        self._result = None
        self._scenario_results = {}
        if self._details_page is not None:
            self._details_page.clear()
        for page in self._pages.values():
            page.clear()
        self._banner.hide()
        self._stack.setCurrentWidget(self._pages[PLAN])
        self._buttons[PLAN].setChecked(True)

    def refresh_theme(self):
        """Redraws what holds a colour of its own: the accounts table's names
        and the Scenarios table's dots. Everything else follows the style sheet
        or paints from the palette."""
        if self._plan is None:
            return
        if self._details_page is not None:
            self._details_page.refresh_colors()
        self._scenarios_stale = True
        if self.current_view() == SCENARIOS:
            self._refresh_scenarios()

    def show_view(self, name: str):
        self._buttons[name].setChecked(True)
        self._stack.setCurrentWidget(self._pages[name] if name in self._pages
                                     else self._details)
        self._refresh_current()

    def current_view(self) -> str:
        current = self._stack.currentWidget()
        return next((name for name, page in self._pages.items() if page is current), DETAILS)

    def _refresh_current(self):
        """Brings the page on show up to date, then the banner."""
        view = self.current_view()
        if view in SAMPLE_VIEWS and self._stale:
            self._recompute()
        elif view == SCENARIOS and self._scenarios_stale:
            self._refresh_scenarios()
        self._update_banner()

    def _my_inputs(self):
        """(inputs, None) for the saved plan, or (None, why it cannot run)."""
        try:
            return retirement.plan_inputs(self._plan, self._savings['figures']), None
        except ValueError as exc:
            missing = retirement.missing_details(self._plan['details'])
            if missing:
                return None, f"Enter {missing_text(missing)} on Your details to see your own plan."
            return None, str(exc)

    def _showing_sample(self) -> bool:
        return self._sample or self._my_inputs()[0] is None

    def _recompute(self):
        if self._plan is None:
            return
        self._stop_moves()
        mine, _problem = self._my_inputs()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if self._sample or mine is None:
                if self._sample_result is None:
                    self._sample_result = compute(retirement.SAMPLE_HOUSEHOLD, sample=True)
                result = self._sample_result
            else:
                result = compute(mine, sample=False)
            # So Plan can say what Invested today is made of. The sample
            # household has no accounts behind it.
            self._pages[PLAN].set_savings(None if result.sample else self._savings)
            for name in (PLAN, PROJECTION, WITHDRAWALS):
                self._pages[name].show_result(result)
            self._pages[WHAT_IF].reset(result)
        finally:
            QApplication.restoreOverrideCursor()
        self._result = result
        self._stale = False
        self._start_moves(result)

    def _update_banner(self):
        """Says whose plan the pages show when it is not the user's own, and
        offers the way back."""
        if self._plan is None or self.current_view() not in SAMPLE_VIEWS:
            self._banner.hide()
            return
        mine, problem = self._my_inputs()
        if mine is None:
            self._banner_text.setText(f"You're looking at a sample household. {problem}")
            self._banner_button.setText('Enter my details')
        elif self._sample:
            self._banner_text.setText("You're looking at a sample household. "
                                      "Your own plan is kept as it is.")
            self._banner_button.setText('Back to my plan')
        else:
            self._banner.hide()
            return
        self._banner.show()

    def _banner_clicked(self):
        if self._sample and self._my_inputs()[0] is not None:
            self._sample = False
            self._stale = True
            self.show_view(self.current_view())
        else:
            self.show_view(DETAILS)

    def _show_sample(self):
        self._sample = True
        self._stale = True
        self.show_view(PLAN)

    # -- next best moves ------------------------------------------------------

    def _start_moves(self, result):
        self._moves = retirement.best_moves_steps(result.inputs, result.simulation.success)
        self._pages[PLAN].show_moves_pending()
        self._moves_timer.start()

    def _stop_moves(self):
        self._moves_timer.stop()
        self._moves = None

    def _step_moves(self):
        """One simulation of the search per tick; the moves once it ends."""
        if self._moves is None:
            self._moves_timer.stop()
            return
        try:
            next(self._moves)
        except StopIteration as done:
            self._stop_moves()
            self._pages[PLAN].show_moves(done.value)

    def moves_running(self) -> bool:
        return self._moves is not None

    def _try_move(self, move: dict):
        """Opens what a next best move is about: What-if with the move already
        made, or the page that holds its setting."""
        if move['kind'] in MOVE_LEVERS:
            self.show_view(WHAT_IF)
            self._pages[WHAT_IF].preset(MOVE_LEVERS[move['kind']](move))
        else:
            self.show_view(MOVE_PAGES.get(move['kind'], PLAN))

    # -- scenarios ------------------------------------------------------------

    def _refresh_scenarios(self):
        page = self._pages[SCENARIOS]
        if self._plan is None:
            return
        mine, _problem = self._my_inputs()
        if mine is None:
            missing = retirement.missing_details(self._plan['details'])
            page.show_message(f"Scenarios compare what-ifs with your own plan. Enter "
                              f"{missing_text(missing)} on Your details first.")
            self._scenarios_stale = False
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            entries = [(None, self._scenario_result(mine))]
            for scenario in self._plan['scenarios']:
                inputs = retirement.apply_levers(mine, scenario['levers'])
                entries.append((scenario, self._scenario_result(inputs)))
            page.show_scenarios(entries)
        finally:
            QApplication.restoreOverrideCursor()
        self._scenarios_stale = False

    def _scenario_result(self, inputs: dict):
        key = tuple(sorted(inputs.items()))
        if key not in self._scenario_results:
            if self._result is not None and self._result.inputs == inputs:
                self._scenario_results[key] = self._result
            else:
                self._scenario_results[key] = compute(inputs, sample=False)
        return self._scenario_results[key]

    def _save_scenario(self, name: str, levers: dict):
        whatif = self._pages[WHAT_IF]
        if self._plan is None or self._showing_sample():
            return
        try:
            plan = retirement.add_scenario(self._plan, name, levers, secrets.token_hex(6))
        except ValueError as exc:
            whatif.show_status(str(exc), error=True)
            return
        if self._store(plan, plan_changed=False):
            whatif.show_status(f"Saved “{plan['scenarios'][0]['name']}”. Compare it on "
                               f"Scenarios.")

    def _apply_levers(self, levers: dict):
        if self._plan is None or self._showing_sample():
            return
        if self._store(retirement.adopt_levers(self._plan, levers)):
            self._refresh_current()
            self._pages[WHAT_IF].show_status('Applied. Your plan now uses these settings.')

    def _adopt_scenario(self, scenario_id: str):
        scenario = next((s for s in (self._plan or {}).get('scenarios', [])
                         if s['id'] == scenario_id), None)
        if scenario is None:
            return
        # Deferred: the redraw replaces the card whose button asked for it.
        if self._store(retirement.adopt_levers(self._plan, scenario['levers'])):
            QTimer.singleShot(0, lambda: self.show_view(PLAN))

    def _delete_scenario(self, scenario_id: str):
        if self._plan is None:
            return
        if self._store(retirement.remove_scenario(self._plan, scenario_id),
                       plan_changed=False):
            QTimer.singleShot(0, self._refresh_current)

    # -- storing ------------------------------------------------------------

    def _store(self, plan: dict, plan_changed: bool = True) -> bool:
        """Saves the plan and keeps Your details' copy in step. False when it
        could not be saved.

        plan_changed is False for a change to the scenarios alone: the plan's
        own results still stand, and What-if keeps the settings it has."""
        try:
            self._plan = retirement_service.save_plan(plan)
        except VaultLockedError:
            self.locked.emit()
            return False
        except Exception as exc:
            self._details.show_error(f"Your change could not be saved: {exc}")
            return False
        self._details.show_error('')
        self._details.adopt(self._plan)
        self._scenarios_stale = True
        if plan_changed:
            self._stale = True
        return True

    def _on_changed(self, plan: dict):
        """Stores a plan Your details has changed."""
        if self._plan is not None:
            self._store(plan)

    def _set_conversions(self, convert: bool, rate: float):
        """Stores the Roth conversion setting from Withdrawals & taxes, then
        redraws the pages. Deferred: the redraw replaces the very checkbox
        whose signal brought it here."""
        if self._plan is None or self._showing_sample():
            return
        plan = copy.deepcopy(self._plan)
        plan['details']['convert'] = convert
        plan['details']['convert_rate'] = rate
        if self._store(plan):
            QTimer.singleShot(0, self._refresh_current)
