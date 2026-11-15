# -----------------------------------------------------------------------------
# core/retirement.py
# The retirement planner's engine: federal tax, one year of withdrawals, a
# lifetime path, the simulated markets the odds come from, and the search
# behind the "next best moves".
#
# Ported from Runway (docs/runway.html) and kept faithful to it: the same
# tables, the same order of arithmetic and the same seeded markets, so one
# household gets the same answer from both. The departures are fixes, each
# marked where it is made:
#   - the markets are as long as the plan. Runway drew 70 years of returns, so
#     a longer plan ran out of them, its balances went to NaN, and a NaN
#     balance never runs out: those markets counted as lasting
#   - HSA contributions, which Runway had no input for
#   - pensions, which Runway had no input for either
#   - two people who retire at different ages, each with their own pay and
#     contributions; Runway retired a household together
#
# Pure: no Qt, no database, no vault. Everything here takes plain numbers and
# hands plain numbers back.
#
# Every figure is in today's dollars. Returns are real (after inflation), and
# the tax brackets are held at their 2026 values for the whole plan: they are
# indexed to inflation, so in today's dollars they stay put.
#
# Inputs are one flat dict; SAMPLE_HOUSEHOLD shows every key, and INPUT_RULES
# what each one may hold. Percentages are stored as percent (6 means 6%),
# except convert_rate, which is the bracket's own rate (0.12). The one key that
# is not a single value is 'extras', the extra spending items (see EXTRA_RULES),
# which may be left out for none.
# -----------------------------------------------------------------------------

import copy
import datetime
import math
from typing import NamedTuple

# -----------------------------------------------------------------------------
# Tables
# -----------------------------------------------------------------------------


class TaxTable(NamedTuple):
    standard: float     # standard deduction
    brackets: tuple     # (top of the band in taxable income, rate), lowest first
    gains_zero: float   # where the 0% capital gains rate ends
    gains_15: float     # where the 15% rate ends; above it, 20%
    ss_lower: float     # provisional income where Social Security starts to be taxed
    ss_upper: float     # where up to 85% of it is


# Approximate 2026 federal figures. They need replacing each year once the IRS
# publishes the next year's.
TAX_TABLES = {
    'joint': TaxTable(
        32200,
        ((24800, .10), (100800, .12), (211400, .22), (403550, .24),
         (512450, .32), (768700, .35), (math.inf, .37)),
        98900, 613700, 32000, 44000,
    ),
    'single': TaxTable(
        16100,
        ((12400, .10), (50400, .12), (105700, .22), (201775, .24),
         (256225, .32), (640600, .35), (math.inf, .37)),
        49450, 545500, 25000, 34000,
    ),
}

# The benefit claimed at each age, as a share of the benefit at 67. A claim
# age missing here is paid the age-67 benefit, as Runway paid it.
SS_FACTORS = {62: .70, 63: .75, 64: .80, 65: .8667, 66: .9333, 67: 1,
              68: 1.08, 69: 1.16, 70: 1.24}


def claim_crossover(age: int):
    """The age at which claiming at `age` and claiming at 67 have paid the
    same in total, or None for 67 itself. Live past it and the later of the
    two claims comes out ahead; die before it and the earlier one does. It
    holds for any benefit, since both grow with the same inflation."""
    factor = SS_FACTORS.get(age, 1)
    if factor == 1:
        return None
    return (67 - factor * age) / (1 - factor)

# Uniform Lifetime Table divisors: the pre-tax balance divided by the divisor
# for one's age is that year's required distribution. Ages past 100 use 100's.
RMD_DIVISORS = {75: 24.6, 76: 23.7, 77: 22.9, 78: 22.0, 79: 21.1, 80: 20.2,
                81: 19.4, 82: 18.5, 83: 17.7, 84: 16.8, 85: 16.0, 86: 15.2,
                87: 14.4, 88: 13.7, 89: 12.9, 90: 12.2, 91: 11.5, 92: 10.8,
                93: 10.1, 94: 9.5, 95: 8.9, 96: 8.4, 97: 7.8, 98: 7.3,
                99: 6.8, 100: 6.4}

# Required distributions start here, and Roth conversions stop. 75 is the
# age for anyone born in 1960 or later.
RMD_AGE = 75

# Health premiums are paid on top of spending until Medicare starts, and
# Medicare costs from then on: each person's by their own age.
MEDICARE_AGE = 65

# What a year of Medicare costs one person if nobody has said: Part B, Part D
# and a supplement, roughly, in today's dollars.
MEDICARE_COST = 6500

# The brackets a Roth conversion may fill up to.
CONVERSION_RATES = (0.12, 0.22, 0.24)

# Simulated markets behind the odds, and the smaller number each next-best-move
# probe uses to stay quick.
SIM_PATHS = 1000
MOVE_PATHS = 400

# Markets are always drawn at least this long, Runway's own length, so short
# and long plans share one set of draws and the cache rarely rebuilds.
_MIN_DRAW_YEARS = 70

# Odds of the money lasting: at or above ON_TRACK is on track, at or above
# NEEDS_TWEAK needs a tweak, and below it is at risk.
ON_TRACK = 0.85
NEEDS_TWEAK = 0.65

# Pre-tax money left at the end, counted as if withdrawn at this rate, so a
# plan that leaves more in a 401(k) is not credited with tax still owed on it.
LEFTOVER_PRETAX_RATE = 0.22

# A shortfall of a dollar or less is rounding, not running out.
_SHORTFALL_TOLERANCE = 1

# The tax estimate behind a year's withdrawals is refined this many times at
# most, and stops once it moves by less than a dollar.
_TAX_PASSES = 5

# -----------------------------------------------------------------------------
# Inputs
# -----------------------------------------------------------------------------

# Runway's sample household. Ages are given directly, not as birth dates, so
# the sample shows the same plan whatever year it is opened in.
SAMPLE_HOUSEHOLD = {
    'age': 48, 'partner': True, 'partner_age': 46, 'retire_age': 55, 'plan_to': 95,
    # Savings today, and what goes into each account every year until retiring.
    'pretax': 720000, 'roth': 150000, 'taxable': 310000, 'basis_pct': 60, 'hsa': 60000,
    'save_pretax': 30000, 'save_roth': 0, 'save_taxable': 18000, 'save_hsa': 0,
    # Runway's contributions were the household's, so here they are all yours.
    'partner_save_pretax': 0, 'partner_save_roth': 0, 'partner_save_taxable': 0,
    'partner_save_hsa': 0,
    # Spending and income in retirement. Social Security is monthly, at 67.
    'spending': 84000, 'health': 14000, 'part_time': 0, 'part_time_years': 0,
    # Runway had no extra spending, so the sample has none.
    'extras': (),
    # Runway's premium was the household's, so here it is all yours. Runway
    # had no health costs from 65, so the sample has none.
    'partner_health': 0, 'health_65': 0, 'partner_health_65': 0,
    'ss': 2900, 'ss_age': 67, 'partner_ss': 1600, 'partner_ss_age': 67,
    # Pensions, monthly. Runway had none, so the sample has none either.
    'pension': 0, 'pension_age': 65, 'pension_cola': False,
    'partner_pension': 0, 'partner_pension_age': 65, 'partner_pension_cola': False,
    # Runway retired the household together, so neither has pay after the
    # other retires.
    'partner_retire_age': 55, 'partner_pay': 0, 'pay': 0,
    # Markets, in percent a year.
    'return_mean': 6, 'volatility': 12, 'inflation': 2.5,
    'convert': True, 'convert_rate': 0.12,
}

# What each input may hold: (kind, lowest, highest). 'whole' is a whole number,
# 'number' any finite number, 'flag' True or False, 'choice' one of the listed
# values. None for highest means no upper limit. The limits are Runway's form's,
# apart from the pensions', which Runway had no form for: their ages take the
# planner's own age limits.
INPUT_RULES = {
    'age':                  ('whole', 18, 90),
    'partner':              ('flag',),
    'partner_age':          ('whole', 18, 90),
    'retire_age':           ('whole', 30, 80),
    'partner_retire_age':   ('whole', 30, 80),
    'plan_to':              ('whole', 70, 105),
    'pretax':               ('number', 0, None),
    'roth':                 ('number', 0, None),
    'taxable':              ('number', 0, None),
    'basis_pct':            ('number', 0, 100),
    'hsa':                  ('number', 0, None),
    'save_pretax':          ('number', 0, None),
    'save_roth':            ('number', 0, None),
    'save_taxable':         ('number', 0, None),
    'save_hsa':             ('number', 0, None),
    'partner_save_pretax':  ('number', 0, None),
    'partner_save_roth':    ('number', 0, None),
    'partner_save_taxable': ('number', 0, None),
    'partner_save_hsa':     ('number', 0, None),
    'spending':             ('number', 0, None),
    'health':               ('number', 0, None),
    'partner_health':       ('number', 0, None),
    'health_65':            ('number', 0, None),
    'partner_health_65':    ('number', 0, None),
    'part_time':            ('number', 0, None),
    'part_time_years':      ('whole', 0, 20),
    'partner_pay':          ('number', 0, None),
    'pay':                  ('number', 0, None),
    'ss':                   ('number', 0, None),
    'ss_age':               ('whole', 62, 70),
    'partner_ss':           ('number', 0, None),
    'partner_ss_age':       ('whole', 62, 70),
    'pension':              ('number', 0, None),
    'pension_age':          ('whole', 18, 90),
    'pension_cola':         ('flag',),
    'partner_pension':      ('number', 0, None),
    'partner_pension_age':  ('whole', 18, 90),
    'partner_pension_cola': ('flag',),
    'return_mean':          ('number', 0, 15),
    'volatility':           ('number', 0, 30),
    'inflation':            ('number', 0, 10),
    'convert':              ('flag',),
    'convert_rate':         ('choice', CONVERSION_RATES),
}

# The six settings the What-if page moves. A saved scenario keeps only these,
# so it follows the plan it was made from in everything else, savings included.
WHAT_IF_LEVERS = ('retire_age', 'partner_retire_age', 'spending', 'return_mean', 'ss_age',
                  'part_time', 'part_time_years')

# Extra spending in retirement: a trip, a car, a wedding. Each item is so much
# a year, starting so many years after you retire (0 is the year you retire),
# for so many years; a purchase is one year. In the inputs an item is a tuple,
# (id, name, amount, start, years), so inputs stay hashable; in a stored plan
# it is a dict with those keys. The id lets a scenario name an item it changed
# however the item is renamed.
EXTRA_RULES = {
    'amount': ('number', 0, None),
    'start':  ('whole', 0, 75),
    'years':  ('whole', 1, 75),
}
EXTRA_FIELDS = ('id', 'name', 'amount', 'start', 'years')
MAX_EXTRAS = 8
EXTRA_NAME_MAX = 40

# The What-if lever holding the items' yearly amounts, {id: amount}. What-if
# moves only the amounts; when and for how long stay as entered.
EXTRA_LEVER = 'extra_amounts'


def check_value(name: str, value, rule: tuple):
    """value as the rule wants it, or ValueError saying why it is not. name is
    what the message calls the value.

    Whole numbers must be whole: ages index the tables, and a fractional age
    would miss every key in RMD_DIVISORS and SS_FACTORS."""
    kind = rule[0]
    if kind == 'flag':
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be on or off.")
        return value
    if kind == 'choice':
        if isinstance(value, bool) or value not in rule[1]:
            raise ValueError(f"{name} must be one of {', '.join(map(str, rule[1]))}.")
        return value

    # bool is an int to Python, and True is not an age anyone typed.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number.")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    if kind == 'whole':
        if value != int(value):
            raise ValueError(f"{name} must be a whole number.")
        value = int(value)
    low, high = rule[1], rule[2]
    if value < low or (high is not None and value > high):
        limit = f"between {low} and {high}" if high is not None else f"at least {low}"
        raise ValueError(f"{name} must be {limit}.")
    return value


def validate_inputs(raw) -> dict:
    """A clean, sanitized copy of raw, or ValueError naming the first problem.
    Every key in INPUT_RULES must be present; unknown keys are dropped."""
    if not isinstance(raw, dict):
        raise ValueError("Retirement inputs are not in the expected format.")

    clean = {}
    for key, rule in INPUT_RULES.items():
        if key not in raw:
            raise ValueError(f"{key} is missing.")
        clean[key] = check_value(key, raw[key], rule)
    clean['extras'] = check_extras(raw.get('extras', ()))
    return sanitize(clean)


def check_extra(raw) -> dict:
    """One extra spending item as a stored plan keeps it, or ValueError saying
    what is wrong with it. raw is a dict or an inputs tuple."""
    if isinstance(raw, (tuple, list)) and len(raw) == len(EXTRA_FIELDS):
        raw = dict(zip(EXTRA_FIELDS, raw))
    if not isinstance(raw, dict):
        raise ValueError("An extra spending item is not in the expected format.")
    item_id, name = raw.get('id'), raw.get('name')
    if not isinstance(item_id, str) or not item_id:
        raise ValueError("An extra spending item has no id.")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Give each extra spending item a name.")
    item = {'id': item_id, 'name': name.strip()[:EXTRA_NAME_MAX]}
    for key, rule in EXTRA_RULES.items():
        item[key] = check_value(key, raw.get(key), rule)
    return item


def check_extras(raw) -> tuple:
    """Extra spending items as the inputs hold them, or ValueError naming
    the first problem."""
    if not isinstance(raw, (tuple, list)) or len(raw) > MAX_EXTRAS:
        raise ValueError(f"There's room for {MAX_EXTRAS} extra spending items.")
    items = tuple(tuple(check_extra(entry)[key] for key in EXTRA_FIELDS) for entry in raw)
    if len({item[0] for item in items}) != len(items):
        raise ValueError("Two extra spending items share an id.")
    return items


def sanitize(inputs: dict) -> dict:
    """A copy with the ages put in an order the plan can run in: at least five
    years of plan, retirement inside it, and claim ages Social Security
    allows. Runway applied this after every edit."""
    p = dict(inputs)
    p['plan_to'] = max(p['plan_to'], p['age'] + 5)
    p['retire_age'] = max(p['age'], min(p['retire_age'], p['plan_to'] - 1))
    p['ss_age'] = min(70, max(62, _js_round(p['ss_age'])))
    p['partner_ss_age'] = min(70, max(62, _js_round(p['partner_ss_age'])))
    if not p['partner']:
        p['partner_age'] = p['age']
    return p


def apply_levers(inputs: dict, levers: dict) -> dict:
    """inputs with a scenario's or the What-if page's settings in place.

    A partner claims Social Security at the same age as you here, as Runway's
    What-if slider moved both."""
    p = dict(inputs)
    p.update({key: value for key, value in levers.items() if key != EXTRA_LEVER})
    amounts = levers.get(EXTRA_LEVER) or {}
    if amounts:
        p['extras'] = tuple((item[0], item[1], amounts.get(item[0], item[2])) + item[3:]
                            for item in p.get('extras', ()))
    if 'ss_age' in levers and p['partner']:
        p['partner_ss_age'] = p['ss_age']
    return sanitize(p)


def extra_spending(inputs: dict) -> list:
    """Each year of the plan's extra spending, from this year to the end,
    in today's dollars. Nothing before retiring, nor past the plan's end."""
    years = plan_years(inputs)
    spent = [0.0] * (years + 1)
    first = max(0, inputs['retire_age'] - inputs['age'])
    for _id, _name, amount, start, length in inputs.get('extras', ()):
        for year in range(first + start, min(first + start + length, years + 1)):
            spent[year] += amount
    return spent


def age_on(birth_year: int, birth_month: int, today: datetime.date | None = None) -> int:
    """Age in whole years on `today`.

    Only the month of birth is known, so the birthday counts as reached from
    the first of that month."""
    if isinstance(birth_month, bool) or not isinstance(birth_month, int) \
            or not 1 <= birth_month <= 12:
        raise ValueError("Birth month must be 1 to 12.")
    today = today or datetime.date.today()
    if isinstance(birth_year, bool) or not isinstance(birth_year, int) \
            or (birth_year, birth_month) > (today.year, today.month):
        raise ValueError("Birth year must be a year that has already begun.")
    return today.year - birth_year - (1 if today.month < birth_month else 0)


# -----------------------------------------------------------------------------
# The stored plan
#
# What the vault keeps for a household: its details as entered, and any
# savings figure typed over the one worked out from the accounts. Ages are kept
# as a birth year and month, so a plan saved this year is still right next
# year. Savings are not kept unless typed over: they come from the accounts
# each time, so the plan follows every balance update.
#
#   {'version': 1, 'details': {key: value}, 'overrides': {key: amount},
#    'scenarios': [{'id': str, 'name': str, 'levers': {key: value}}]}
#
# Every detail is optional; one that is missing has not been entered yet. A
# scenario keeps only the What-if settings it changed, newest first. The
# details' 'extras' is a list of extra spending items (see EXTRA_RULES), and a
# scenario's levers may hold EXTRA_LEVER, the amounts it changed, by item id.
# -----------------------------------------------------------------------------

PLAN_VERSION = 1

# The figures the accounts supply, which may be typed over.
SAVINGS_KEYS = ('pretax', 'roth', 'taxable', 'hsa',
                'save_pretax', 'save_roth', 'save_taxable', 'save_hsa',
                'partner_save_pretax', 'partner_save_roth', 'partner_save_taxable',
                'partner_save_hsa')

# Each person's yearly contributions, in the order Balances holds the accounts
# (the taxable one twice: its value and its basis).
_SAVE_YOURS = ('save_pretax', 'save_roth', 'save_taxable', 'save_hsa')
_SAVE_PARTNERS = ('partner_save_pretax', 'partner_save_roth', 'partner_save_taxable',
                  'partner_save_hsa')

# The monthly benefits the Income records supply. Unlike savings, a figure for
# one is kept with the details, where it was entered before the records could
# supply it, and it wins over the records' while it is there.
BENEFIT_KEYS = ('ss', 'partner_ss', 'pension', 'partner_pension')

# The inputs a stored plan holds as entered. The ages are not among them.
_PLAN_INPUTS = ('partner', 'retire_age', 'partner_retire_age', 'partner_pay', 'pay', 'plan_to', 'basis_pct', 'spending', 'health',
                'partner_health', 'health_65', 'partner_health_65', 'part_time', 'part_time_years', 'ss', 'ss_age', 'partner_ss',
                'partner_ss_age', 'pension', 'pension_age', 'pension_cola',
                'partner_pension', 'partner_pension_age', 'partner_pension_cola',
                'return_mean', 'volatility', 'inflation', 'convert', 'convert_rate')

# What each detail may hold, in INPUT_RULES's terms. A birth year is checked
# against today by age_on, which this cannot do.
DETAIL_RULES = {
    'birth_year':          ('whole', 1900, 2100),
    'birth_month':         ('whole', 1, 12),
    'partner_birth_year':  ('whole', 1900, 2100),
    'partner_birth_month': ('whole', 1, 12),
    **{key: INPUT_RULES[key] for key in _PLAN_INPUTS},
}

# What a detail nobody has entered counts as. Birth dates and spending have no
# default: the plan waits for them, since any guess would decide the answer.
# The partner's retirement age has none here either: until it is entered, the
# partner retires the same year you do (see plan_inputs).
# The rest are Runway's sample figures, apart from conversions, which are off
# until chosen, and health costs from 65, which Runway had none of.
DETAIL_DEFAULTS = {
    'partner': False, 'retire_age': 65, 'partner_pay': 0, 'pay': 0,
    'plan_to': 95, 'basis_pct': 60,
    'health': 0, 'partner_health': 0,
    'health_65': MEDICARE_COST, 'partner_health_65': MEDICARE_COST,
    'part_time': 0, 'part_time_years': 0,
    'ss': 0, 'ss_age': 67, 'partner_ss': 0, 'partner_ss_age': 67,
    'pension': 0, 'pension_age': 65, 'pension_cola': False,
    'partner_pension': 0, 'partner_pension_age': 65, 'partner_pension_cola': False,
    'return_mean': 6, 'volatility': 12, 'inflation': 2.5,
    'convert': False, 'convert_rate': 0.12,
}

# The youngest and oldest the planner runs for: Runway's own limits.
YOUNGEST, OLDEST = INPUT_RULES['age'][1], INPUT_RULES['age'][2]

# Saved scenarios, as many as the Scenarios page has room to compare beside
# the plan. Runway kept the same number, but made room for a new one by
# quietly deleting the oldest; here a fifth is refused instead.
MAX_SCENARIOS = 4
SCENARIO_NAME_MAX = 40


def empty_plan() -> dict:
    return {'version': PLAN_VERSION, 'details': {}, 'overrides': {}, 'scenarios': []}


def validate_plan(raw) -> dict:
    """A clean copy of a stored plan, or ValueError when raw is not one at all.

    A detail or override that does not validate is dropped and reads as not
    entered; the rest of the plan is kept. A newer version of the app might
    allow a value this one does not, and that should cost one field rather
    than the whole plan. Unknown keys are dropped."""
    if not isinstance(raw, dict):
        raise ValueError("The retirement plan is not in the expected format.")
    # Missing or None reads as empty; anything else must be an object.
    details = raw.get('details')
    details = {} if details is None else details
    overrides = raw.get('overrides')
    overrides = {} if overrides is None else overrides
    if not isinstance(details, dict) or not isinstance(overrides, dict):
        raise ValueError("The retirement plan is not in the expected format.")

    clean = empty_plan()
    for key, rule in DETAIL_RULES.items():
        if details.get(key) is not None:
            try:
                clean['details'][key] = check_value(key, details[key], rule)
            except ValueError:
                pass
    extras = _clean_extras(details.get('extras'))
    if extras:
        clean['details']['extras'] = extras
    for key in SAVINGS_KEYS:
        if overrides.get(key) is not None:
            try:
                clean['overrides'][key] = check_value(key, overrides[key], INPUT_RULES[key])
            except ValueError:
                pass
    clean['scenarios'] = _clean_scenarios(raw.get('scenarios'),
                                          {item['id'] for item in extras})
    return clean


def _clean_extras(raw) -> list:
    """The extra spending items worth keeping from a stored list. One that
    does not validate is dropped on its own, as a bad detail is."""
    if not isinstance(raw, list):
        return []
    clean, seen = [], set()
    for entry in raw:
        try:
            item = check_extra(entry)
        except ValueError:
            continue
        if item['id'] in seen:
            continue
        seen.add(item['id'])
        clean.append(item)
        if len(clean) == MAX_EXTRAS:
            break
    return clean


def _clean_scenarios(raw, extra_ids=frozenset()) -> list:
    """The scenarios worth keeping from a stored list: each needs an id and a
    name, and keeps the levers that validate. Anything else is dropped on its
    own, as a bad detail is. An amount for an extra spending item the plan no
    longer has is dropped, so deleting an item drops it from every scenario."""
    if not isinstance(raw, list):
        return []
    clean, seen = [], set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        scenario_id, name = entry.get('id'), entry.get('name')
        if not isinstance(scenario_id, str) or not scenario_id or scenario_id in seen:
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        levers = entry.get('levers') if isinstance(entry.get('levers'), dict) else {}
        kept = {}
        for key in WHAT_IF_LEVERS:
            if levers.get(key) is not None:
                try:
                    kept[key] = check_value(key, levers[key], INPUT_RULES[key])
                except ValueError:
                    pass
        amounts = levers.get(EXTRA_LEVER) if isinstance(levers.get(EXTRA_LEVER), dict) else {}
        kept_amounts = {}
        for item_id, amount in amounts.items():
            if item_id in extra_ids:
                try:
                    kept_amounts[item_id] = check_value('amount', amount,
                                                        EXTRA_RULES['amount'])
                except ValueError:
                    pass
        if kept_amounts:
            kept[EXTRA_LEVER] = kept_amounts
        seen.add(scenario_id)
        clean.append({'id': scenario_id, 'name': name.strip()[:SCENARIO_NAME_MAX],
                      'levers': kept})
        if len(clean) == MAX_SCENARIOS:
            break
    return clean


def changed_levers(base: dict, inputs: dict) -> dict:
    """The What-if settings in which inputs differ from base: what a saved
    scenario keeps, so it follows its plan in everything it did not change."""
    changed = {key: inputs[key] for key in WHAT_IF_LEVERS if inputs[key] != base[key]}
    planned = {item[0]: item[2] for item in base.get('extras', ())}
    amounts = {item[0]: item[2] for item in inputs.get('extras', ())
               if item[0] in planned and item[2] != planned[item[0]]}
    if amounts:
        changed[EXTRA_LEVER] = amounts
    return changed


def add_scenario(plan: dict, name: str, levers: dict, scenario_id: str) -> dict:
    """plan with a new scenario first in its list. ValueError when there is no
    room or no name, saying which."""
    if len(plan.get('scenarios', [])) >= MAX_SCENARIOS:
        raise ValueError(f"There's room to compare {MAX_SCENARIOS} scenarios. Delete one "
                         f"on Scenarios to save this one.")
    name = (name or '').strip()[:SCENARIO_NAME_MAX]
    if not name:
        raise ValueError("Give the scenario a name.")
    new = copy.deepcopy(plan)
    new.setdefault('scenarios', []).insert(
        0, {'id': scenario_id, 'name': name, 'levers': dict(levers)})
    return validate_plan(new)


def remove_scenario(plan: dict, scenario_id: str) -> dict:
    new = copy.deepcopy(plan)
    new['scenarios'] = [s for s in new.get('scenarios', []) if s['id'] != scenario_id]
    return validate_plan(new)


def adopt_levers(plan: dict, levers: dict) -> dict:
    """plan with What-if settings written into its details, as Apply and
    "Make this my plan" do. A partner claims Social Security at the same age,
    as apply_levers has it."""
    new = copy.deepcopy(plan)
    new['details'].update({key: value for key, value in levers.items() if key != EXTRA_LEVER})
    amounts = levers.get(EXTRA_LEVER) or {}
    for item in new['details'].get('extras', []):
        if item['id'] in amounts:
            item['amount'] = amounts[item['id']]
    if 'ss_age' in levers and new['details'].get('partner'):
        new['details']['partner_ss_age'] = levers['ss_age']
    return validate_plan(new)


def missing_details(details: dict) -> list:
    """The details a plan cannot run without that have not been entered, in
    the order a form would ask for them."""
    needed = ['birth_year', 'birth_month']
    if details.get('partner'):
        needed += ['partner_birth_year', 'partner_birth_month']
    needed.append('spending')
    return [key for key in needed if details.get(key) is None]


def plan_inputs(plan: dict, savings: dict, today: datetime.date | None = None) -> dict:
    """The engine's inputs for a stored plan: its details, defaults for any
    not entered, ages worked out from the birth dates, and each savings figure
    and benefit from `savings` unless the plan types over it.

    ValueError when a needed detail is missing (see missing_details) or an
    age falls outside what the planner runs for."""
    details = plan['details']
    if missing_details(details):
        raise ValueError("Some details the plan needs have not been entered.")

    inputs = dict(DETAIL_DEFAULTS)
    inputs.update({key: details[key] for key in _PLAN_INPUTS if key in details})
    inputs['extras'] = tuple(tuple(item[key] for key in EXTRA_FIELDS)
                             for item in details.get('extras', []))
    inputs['age'] = age_on(details['birth_year'], details['birth_month'], today)
    inputs['partner_age'] = (age_on(details['partner_birth_year'],
                                    details['partner_birth_month'], today)
                             if inputs['partner'] else inputs['age'])
    if 'partner_retire_age' not in inputs:
        inputs['partner_retire_age'] = same_year_retirement(inputs)
    for key, who in (('age', 'You'), ('partner_age', 'Your partner')):
        if not YOUNGEST <= inputs[key] <= OLDEST:
            raise ValueError(f"{who} must be between {YOUNGEST} and {OLDEST} for the "
                             f"planner to work, and the birth date entered makes "
                             f"{'you' if who == 'You' else 'them'} {inputs[key]}.")
    for key in SAVINGS_KEYS:
        inputs[key] = plan['overrides'].get(key, savings.get(key, 0))
    for key in BENEFIT_KEYS:
        inputs[key] = benefit(details, savings, key)
    return validate_inputs(inputs)


def same_year_retirement(inputs: dict) -> int:
    """The partner's age the year you retire: their retirement age until one
    is entered, within the ages the planner allows."""
    low, high = INPUT_RULES['partner_retire_age'][1:]
    age = inputs['partner_age'] + inputs['retire_age'] - inputs['age']
    return min(high, max(low, age))


def benefit(details: dict, savings: dict, key: str) -> float:
    """A monthly benefit (one of BENEFIT_KEYS) as the plan uses it: the figure
    in the details, or else the Income records'.

    Social Security already being received is paid as it is. The plan scales
    a benefit at 67 by the claim age, so one received counts as whatever at 67
    the claim age scales back to it."""
    if details.get(key) is not None:
        return details[key]
    value = savings.get(key, 0)
    received = savings.get(f'{key}_received', 0)
    if received:
        age = details.get(f'{key}_age', DETAIL_DEFAULTS[f'{key}_age'])
        value += received / SS_FACTORS.get(age, 1)
    return value


def plan_years(inputs: dict) -> int:
    """How many years the plan runs past this one. A plan always has at least
    one, so there is something to chart."""
    return max(1, inputs['plan_to'] - inputs['age'])


def _js_round(value: float) -> int:
    """JavaScript's Math.round: halves go up. Python's round() sends them to
    the even neighbour, which would move Runway's figures that land on .5."""
    return math.floor(value + 0.5)

# -----------------------------------------------------------------------------
# Tax
# -----------------------------------------------------------------------------


class Tax(NamedTuple):
    tax: float
    ordinary_taxable: float  # taxable income other than capital gains
    unused_deduction: float  # standard deduction left over after all income


def ordinary_tax(taxable: float, table: TaxTable) -> float:
    """Tax on ordinary taxable income, filled through the brackets in order."""
    tax = 0.0
    previous = 0
    for top, rate in table.brackets:
        if taxable <= previous:
            break
        tax += (min(taxable, top) - previous) * rate
        previous = top
    return tax


def bracket_top(table: TaxTable, rate: float) -> float:
    """Where the bracket at `rate` ends, in taxable income. 0 for a rate the
    table has no bracket at."""
    for top, bracket_rate in table.brackets:
        if bracket_rate == rate:
            return top
    return 0


def federal_tax(ordinary: float, gains: float, social_security: float,
                table: TaxTable) -> Tax:
    """Federal income tax on a year's ordinary income, long-term gains and
    Social Security benefits.

    Up to 85% of the benefits are taxable, depending on provisional income.
    Gains stack on top of ordinary income and fill the 0%, 15% and 20% bands
    in turn."""
    provisional = ordinary + gains + 0.5 * social_security
    taxable_ss = 0.0
    if provisional > table.ss_lower:
        if provisional <= table.ss_upper:
            taxable_ss = min(0.5 * social_security, 0.5 * (provisional - table.ss_lower))
        else:
            taxable_ss = min(0.85 * social_security,
                             0.85 * (provisional - table.ss_upper)
                             + min(0.5 * social_security,
                                   0.5 * (table.ss_upper - table.ss_lower)))

    agi = ordinary + taxable_ss + gains
    taxable = max(0, agi - table.standard)
    ordinary_taxable = max(0, taxable - gains)
    gains_taxable = taxable - ordinary_taxable

    tax = ordinary_tax(ordinary_taxable, table)
    in_zero = max(0, min(gains_taxable, table.gains_zero - ordinary_taxable))
    rest = gains_taxable - in_zero
    in_15 = max(0, min(rest, table.gains_15 - max(ordinary_taxable, table.gains_zero)))
    tax += in_15 * 0.15 + (rest - in_15) * 0.20
    return Tax(tax, ordinary_taxable, max(0, table.standard - agi))

# -----------------------------------------------------------------------------
# One year, and a lifetime
# -----------------------------------------------------------------------------


class Balances(NamedTuple):
    pretax: float
    roth: float
    taxable: float
    basis: float  # what was paid into taxable; the rest of taxable is gains
    hsa: float

    def total(self) -> float:
        # basis is a part of taxable, not money of its own.
        return self.pretax + self.roth + self.taxable + self.hsa


_EMPTY = Balances(0.0, 0.0, 0.0, 0.0, 0.0)


class Year(NamedTuple):
    from_taxable: float
    from_pretax: float  # the required distribution included
    from_roth: float
    from_hsa: float
    gains: float        # the part of from_taxable that was gains
    conversion: float   # moved from pre-tax to Roth
    reinvested: float   # income beyond the need, put into taxable
    shortfall: float    # the need nothing was left to pay
    tax: float
    balances: Balances


def plan_year(balances: Balances, need: float, social_security: float,
              other_income: float, rmd: float, inputs: dict, age: int, table: TaxTable,
              tax_estimate: float) -> Year:
    """One retired year: take the required distribution, meet the need plus
    the estimated tax from taxable, then pre-tax, then Roth, then the HSA, and
    convert to Roth up to the chosen bracket if conversions are on.

    other_income is part-time work, a partner's pay and pensions: all pay
    toward the need and are taxed as ordinary income.

    The need includes tax_estimate, which this year's own withdrawals decide,
    so run_path calls this repeatedly until the estimate settles."""
    pretax, roth, taxable, basis, hsa = balances
    from_taxable = from_roth = from_hsa = gains = conversion = reinvested = 0.0

    required = min(rmd, pretax)
    pretax -= required
    from_pretax = required

    gap = need + tax_estimate - social_security - other_income - required
    if gap < 0:
        taxable += -gap
        basis += -gap
        reinvested = -gap
        gap = 0
    if gap > 0 and taxable > 0:
        take = min(gap, taxable)
        # Each dollar out carries the account's share of gains with it.
        basis_share = basis / taxable
        gains = take * max(0, 1 - basis_share)
        basis -= take * basis_share
        taxable -= take
        from_taxable = take
        gap -= take
    if gap > 0 and pretax > 0:
        take = min(gap, pretax)
        pretax -= take
        from_pretax += take
        gap -= take
    if gap > 0 and roth > 0:
        take = min(gap, roth)
        roth -= take
        from_roth = take
        gap -= take
    if gap > 0 and hsa > 0:
        take = min(gap, hsa)
        hsa -= take
        from_hsa = take
        gap -= take

    if inputs['convert'] and inputs['retire_age'] <= age < RMD_AGE and pretax > 0:
        base = federal_tax(from_pretax + other_income, gains, social_security, table)
        room = bracket_top(table, inputs['convert_rate']) - base.ordinary_taxable \
            + base.unused_deduction
        if room > 0:
            conversion = min(room, pretax)
            pretax -= conversion
            roth += conversion

    tax = federal_tax(from_pretax + conversion + other_income, gains, social_security,
                      table).tax
    return Year(from_taxable, from_pretax, from_roth, from_hsa, gains, conversion,
                reinvested, gap, tax, Balances(pretax, roth, taxable, basis, hsa))


class Path(NamedTuple):
    totals: list           # the portfolio at the end of each year, from today
    failed_at: int | None  # the age the money ran out, None if it lasted
    lifetime_tax: float
    rows: list | None      # each year in detail, when run_path was asked to record


def pension_income(monthly: float, rises: bool, years_from_now: int,
                   inflation: float) -> float:
    """A year of a pension, in today's dollars. One that rises with
    inflation keeps its value. A fixed one pays the same dollars every year,
    so it buys a little less each year from today: inflation is in percent."""
    yearly = monthly * 12
    return yearly if rises else yearly / (1 + inflation / 100) ** years_from_now


def _saved(balances: Balances, amounts, share: float = 1.0) -> Balances:
    """balances with a year's contributions (pre-tax, Roth, taxable, HSA) paid
    in, or that share of them. Taxable money goes in as basis too."""
    pretax, roth, taxable, hsa = (amount * share for amount in amounts)
    return Balances(balances.pretax + pretax, balances.roth + roth,
                    balances.taxable + taxable, balances.basis + taxable,
                    balances.hsa + hsa)


def _working(balances: Balances, pay: float, amounts) -> tuple[float, Balances]:
    """(pay left to spend, balances) for someone still working while the
    household draws on its savings. Their contributions come out of their pay:
    as much of them as the pay covers, so with no pay entered none go in."""
    total = sum(amounts)
    if not total:
        return pay, balances
    put_in = min(total, pay)
    return pay - put_in, (_saved(balances, amounts, put_in / total) if put_in else balances)


def run_path(inputs: dict, returns, record: bool = False) -> Path:
    """The household from today to the end of the plan, one real return per
    year: saving while both work, then plan_year each year from the first
    year either of you is retired.

    Each person's contributions go in until their own retirement. Once the
    household is drawing on its savings, the one still working is paid their
    pay, less the contributions it covers (see _working). Social Security and
    pensions are paid from their start ages, in the years the household draws:
    before then, what comes in is what the household saves. A pension pays for
    life, each person's from their own age. Each person's health costs are
    added to spending from their own retirement: premiums until their 65, and
    Medicare costs from then on. Part-time work and extra spending count from
    your retirement (see extra_spending).

    Once the money runs out, every later balance is zero."""
    table = TAX_TABLES['joint' if inputs['partner'] else 'single']
    age = inputs['age']
    partner_age = inputs['partner_age']
    retire_age = inputs['retire_age']
    partner = inputs['partner']
    years = plan_years(inputs)
    extra = extra_spending(inputs)
    # A plan for one counts any contributions marked as a partner's as yours.
    yours = [inputs[key] for key in _SAVE_YOURS]
    partners = [inputs[key] for key in _SAVE_PARTNERS]
    if not partner:
        yours = [mine + theirs for mine, theirs in zip(yours, partners)]
        partners = [0.0] * len(partners)

    balances = Balances(inputs['pretax'], inputs['roth'], inputs['taxable'],
                        inputs['taxable'] * inputs['basis_pct'] / 100, inputs['hsa'])
    totals = [0.0] * (years + 1)
    rows = [] if record else None
    failed_at = None
    lifetime_tax = 0.0

    for year in range(years + 1):
        current = age + year
        you_work = current < retire_age
        partner_works = partner and partner_age + year < inputs['partner_retire_age']
        drawing = not you_work or (partner and not partner_works)
        row = None
        if record:
            row = {'age': current, 'start': balances.total(), 'need': 0.0,
                   'social_security': 0.0, 'pension': 0.0, 'work': 0.0,
                   'pay': 0.0, 'partner_pay': 0.0,
                   'from_taxable': 0.0,
                   'from_pretax': 0.0, 'from_roth': 0.0, 'from_hsa': 0.0,
                   'conversion': 0.0, 'rmd': 0.0, 'tax': 0.0, 'shortfall': 0.0,
                   'end': 0.0, 'pretax_end': 0.0, 'retired': drawing}

        if failed_at is None:
            if not drawing:
                # The HSA line is new: Runway had no HSA contribution input.
                balances = _saved(_saved(balances, yours), partners)
            else:
                pay = partner_pay = 0.0
                if you_work:
                    pay, balances = _working(balances, inputs['pay'], yours)
                if partner_works:
                    partner_pay, balances = _working(balances, inputs['partner_pay'], partners)
                need = inputs['spending'] + extra[year]
                if not you_work:
                    need += inputs['health'] if current < MEDICARE_AGE else inputs['health_65']
                if partner and not partner_works:
                    need += (inputs['partner_health'] if partner_age + year < MEDICARE_AGE
                             else inputs['partner_health_65'])
                social_security = 0.0
                if current >= inputs['ss_age']:
                    social_security += inputs['ss'] * 12 * SS_FACTORS.get(inputs['ss_age'], 1)
                if partner and partner_age + year >= inputs['partner_ss_age']:
                    social_security += (inputs['partner_ss'] * 12
                                        * SS_FACTORS.get(inputs['partner_ss_age'], 1))
                # The amount is checked first: most plans have no pension, and
                # this runs for every year of every simulated market.
                pension = 0.0
                if inputs['pension'] and current >= inputs['pension_age']:
                    pension += pension_income(inputs['pension'], inputs['pension_cola'],
                                              year, inputs['inflation'])
                if (partner and inputs['partner_pension']
                        and partner_age + year >= inputs['partner_pension_age']):
                    pension += pension_income(inputs['partner_pension'],
                                              inputs['partner_pension_cola'],
                                              year, inputs['inflation'])
                work = (inputs['part_time']
                        if not you_work and current - retire_age < inputs['part_time_years']
                        else 0)
                rmd = (balances.pretax / RMD_DIVISORS.get(min(current, 100), 6.4)
                       if current >= RMD_AGE else 0)

                estimate = 0.0
                for _ in range(_TAX_PASSES):
                    result = plan_year(balances, need, social_security,
                                       work + pay + partner_pay + pension,
                                       rmd, inputs, current, table, estimate)
                    if abs(result.tax - estimate) < 1:
                        break
                    estimate = result.tax

                balances = result.balances
                lifetime_tax += result.tax
                if result.shortfall > _SHORTFALL_TOLERANCE:
                    failed_at = current
                if row is not None:
                    row.update(need=need, social_security=social_security,
                               pension=pension, work=work, pay=pay, partner_pay=partner_pay,
                               from_taxable=result.from_taxable,
                               from_pretax=result.from_pretax,
                               from_roth=result.from_roth, from_hsa=result.from_hsa,
                               conversion=result.conversion,
                               rmd=min(rmd, result.from_pretax),
                               tax=result.tax, shortfall=result.shortfall)

        if failed_at is not None:
            balances = _EMPTY
        else:
            growth = 1 + returns[year]
            balances = Balances(balances.pretax * growth, balances.roth * growth,
                                balances.taxable * growth, balances.basis,
                                balances.hsa * growth)

        total = balances.total()
        totals[year] = total
        if row is not None:
            row['end'] = total
            row['pretax_end'] = balances.pretax
            rows.append(row)

    return Path(totals, failed_at, lifetime_tax, rows)

# -----------------------------------------------------------------------------
# Markets
# -----------------------------------------------------------------------------

_MASK = 0xFFFFFFFF


def _mulberry32(seed: int):
    """Runway's random number generator, bit for bit: JavaScript's 32-bit
    integer arithmetic, done here with masks. Returns a function giving
    floats in [0, 1)."""
    state = seed & _MASK

    def next_float() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & _MASK
        t = ((state ^ (state >> 15)) * (1 | state)) & _MASK
        t = ((t + (((t ^ (t >> 7)) * (61 | t)) & _MASK)) & _MASK) ^ t
        return ((t ^ (t >> 14)) & _MASK) / 4294967296

    return next_float


def _normal_row(seed: int, years: int) -> list:
    """Standard normal draws for one simulated market, one per year from 0 to
    `years`, by Box-Muller. Each year takes the next two numbers from the
    seed, so a longer row starts with exactly the draws of a shorter one."""
    draw = _mulberry32(seed)
    row = []
    for _ in range(years + 1):
        u1 = max(1e-12, draw())
        u2 = draw()
        row.append(math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2))
    return row


# Built once and reused: every plan, scenario and probe runs on these same
# markets, so a comparison between two of them is never decided by luck.
_draws: list = []
_draw_years = -1


def market_draws(paths: int, years: int) -> list:
    """Draws for at least `paths` markets of at least `years` years. Market i
    is always seeded with 1000 + i, as Runway seeded it.

    The fix for Runway's 70-year limit: the draws grow to the length asked
    for. Their first 70 years are unchanged, so every plan Runway could run
    gets the same markets here."""
    global _draws, _draw_years
    if len(_draws) < paths or _draw_years < years:
        paths = max(paths, len(_draws))
        years = max(years, _draw_years, _MIN_DRAW_YEARS)
        _draws = [_normal_row(1000 + index, years) for index in range(paths)]
        _draw_years = years
    return _draws

# -----------------------------------------------------------------------------
# Results
# -----------------------------------------------------------------------------


class Simulation(NamedTuple):
    success: float  # share of markets in which the money lasted to plan_to
    p10: list       # the 10th percentile portfolio at each age
    p50: list
    p90: list
    ages: list


def simulate(inputs: dict, paths: int = SIM_PATHS) -> Simulation:
    """The plan run through `paths` simulated markets: the share in which the
    money lasts, and the spread of portfolios at each age."""
    years = plan_years(inputs)
    draws = market_draws(paths, years)
    # Each year's real return is (1 + mean + draw x volatility) / (1 + inflation)
    # - 1, floored at a 95% loss, worked in Runway's order of operations.
    mean = inputs['return_mean'] / 100
    volatility = inputs['volatility']
    inflation = 1 + inputs['inflation'] / 100

    lasted = 0
    paths_totals = []
    for index in range(paths):
        row = draws[index]
        returns = [max(-0.95, (1 + (mean + row[year] * volatility / 100)) / inflation - 1)
                   for year in range(years + 1)]
        path = run_path(inputs, returns)
        if path.failed_at is None:
            lasted += 1
        paths_totals.append(path.totals)

    p10, p50, p90 = [], [], []
    low, middle, high = (math.floor(paths * 0.1), math.floor(paths * 0.5),
                         math.floor(paths * 0.9))
    for column in zip(*paths_totals):
        ordered = sorted(column)
        p10.append(ordered[low])
        p50.append(ordered[middle])
        p90.append(ordered[high])
    ages = [inputs['age'] + year for year in range(years + 1)]
    return Simulation(lasted / paths, p10, p50, p90, ages)


def steady(inputs: dict) -> Path:
    """The plan with the average return every year, recorded year by year:
    the path behind the withdrawals table and the tax figures."""
    years = plan_years(inputs)
    real = (1 + inputs['return_mean'] / 100) / (1 + inputs['inflation'] / 100) - 1
    return run_path(inputs, [real] * (years + 1), record=True)


def after_tax_end(path: Path) -> float:
    """What a recorded path leaves at the end, with the pre-tax part counted
    as if withdrawn at LEFTOVER_PRETAX_RATE."""
    last = path.rows[-1]
    return last['end'] - last['pretax_end'] * LEFTOVER_PRETAX_RATE


def verdict(success: float) -> str:
    """'good' on track, 'warn' needs a tweak, 'bad' at risk."""
    if success >= ON_TRACK:
        return 'good'
    if success >= NEEDS_TWEAK:
        return 'warn'
    return 'bad'

# -----------------------------------------------------------------------------
# Next best moves
# -----------------------------------------------------------------------------

# A plan short of ON_TRACK looks for the fewest extra working years that reach
# it, up to this many and no later than LATEST_RETIREMENT.
_MAX_EXTRA_YEARS = 10
LATEST_RETIREMENT = 75

# Spending cuts are tried in steps of this much, up to this share of spending.
_SPENDING_STEP = 2000
_MAX_SPENDING_CUT = 0.4

# A plan already on track looks for an earlier retirement up to this far back.
_MAX_EARLIER_YEARS = 8

# Roth conversions are suggested when they would leave more than this after
# tax at the end of the plan.
_CONVERSION_WORTH = 5000

# Delaying Social Security is suggested when it adds at least this many points
# to the odds.
_DELAY_WORTH = 2


def best_moves_steps(inputs: dict, success: float):
    """The search behind the next best moves, as a generator.

    Each move needs the plan simulated again, about 30 times at worst, so the
    search yields after every simulation and a caller can spread them out
    instead of freezing on all of them at once. It returns (in StopIteration)
    a list of moves in Runway's order, each a dict with a 'kind' and the
    figures to describe it:

      work_longer            years, retire_age, partner
      spend_less             cut, spending
      retire_earlier         years, retire_age, partner
      start_conversions      gain
      conversions            first, years, rate, tax_saved, gain
      delay_social_security  points
      health_until_medicare  years, premium

    `success` is the plan's odds at SIM_PATHS. The probes use MOVE_PATHS, as
    Runway's did; delay_social_security compares two probes, not the plan's
    own odds, so both sides are measured the same way.

    For a couple, working longer and retiring earlier are searched for each
    person on their own, and partner is True on the partner's; their
    retire_age is the partner's own age. Runway moved the household together.

    The changes are tried without sanitize, as Runway tried them."""
    moves = []
    retire_age = inputs['retire_age']
    spending = inputs['spending']
    people = [('retire_age', inputs['age'], False)]
    if inputs['partner']:
        people.append(('partner_retire_age', inputs['partner_age'], True))

    def odds(**changes) -> float:
        return simulate({**inputs, **changes}, MOVE_PATHS).success

    if success < ON_TRACK:
        for key, _now, partner in people:
            start = inputs[key]
            for extra in range(1, _MAX_EXTRA_YEARS + 1):
                if start + extra > LATEST_RETIREMENT:
                    break
                reached = odds(**{key: start + extra}) >= ON_TRACK
                yield
                if reached:
                    moves.append({'kind': 'work_longer', 'years': extra,
                                  'retire_age': start + extra, 'partner': partner})
                    break

        cut = _SPENDING_STEP
        while cut <= spending * _MAX_SPENDING_CUT:
            reached = odds(spending=spending - cut) >= ON_TRACK
            yield
            if reached:
                moves.append({'kind': 'spend_less', 'cut': cut, 'spending': spending - cut})
                break
            cut += _SPENDING_STEP

    else:
        for key, now, partner in people:
            start = inputs[key]
            if start <= now:
                continue
            earliest = None
            last = max(now, start - _MAX_EARLIER_YEARS)
            for earlier in range(start - 1, last - 1, -1):
                reached = odds(**{key: earlier}) >= ON_TRACK
                yield
                if not reached:
                    break
                earliest = earlier
            if earliest is not None:
                moves.append({'kind': 'retire_earlier', 'years': start - earliest,
                              'retire_age': earliest, 'partner': partner})

    without = steady({**inputs, 'convert': False})
    with_conversions = steady({**inputs, 'convert': True})
    tax_saved = without.lifetime_tax - with_conversions.lifetime_tax
    gain = after_tax_end(with_conversions) - after_tax_end(without)
    if not inputs['convert'] and gain > _CONVERSION_WORTH:
        moves.append({'kind': 'start_conversions', 'gain': gain})
    elif inputs['convert']:
        converting = [row for row in with_conversions.rows if row['conversion'] > 0]
        if converting:
            moves.append({'kind': 'conversions', 'first': converting[0]['conversion'],
                          'years': len(converting), 'rate': inputs['convert_rate'],
                          'tax_saved': tax_saved, 'gain': gain})

    if inputs['ss_age'] < 70:
        partner_age = 70 if inputs['partner'] else inputs['partner_ss_age']
        delayed = odds(ss_age=70, partner_ss_age=partner_age)
        yield
        now = odds()
        yield
        points = _js_round((delayed - now) * 100)
        if points >= _DELAY_WORTH:
            moves.append({'kind': 'delay_social_security', 'points': points})

    if retire_age < MEDICARE_AGE and inputs['health'] > 0:
        moves.append({'kind': 'health_until_medicare', 'years': MEDICARE_AGE - retire_age,
                      'premium': inputs['health']})

    return moves


def best_moves(inputs: dict, success: float) -> list:
    """best_moves_steps run to the end in one go."""
    steps = best_moves_steps(inputs, success)
    while True:
        try:
            next(steps)
        except StopIteration as done:
            return done.value
