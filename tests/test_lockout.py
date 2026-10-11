# -----------------------------------------------------------------------------
# tests/test_lockout.py
# Login throttling: the control that stands between a stolen vault file and an
# offline guessing run at the keyboard.
#
# Every assertion here is about the *limit*, never about the wait actually
# elapsing — nothing sleeps. Where time has to move, it moves by rewriting the
# stored start time or by replacing time.time, because the whole design of this
# code is that it does not trust the clock.
# -----------------------------------------------------------------------------

import time

import pytest

from core import auth
from core import database
from conftest import MASTER

WRONG = 'not the master password at all'


def attempts() -> int:
    return int(float(database.read_config('failed_login_attempts') or 0))


def fail_once() -> bool:
    return auth.verify_master_password(WRONG)


# -- the limit itself ---------------------------------------------------------

def test_a_wrong_password_is_refused_without_unlocking(vault):
    auth.lock_app()
    assert fail_once() is False
    assert not auth.check_session()


def test_five_wrong_passwords_lock_the_next_attempt_out(vault):
    auth.lock_app()
    for _ in range(auth._MAX_ATTEMPTS):
        assert fail_once() is False

    allowed, wait = auth.check_login_rate_limit()
    assert allowed is False
    assert wait > 0


def test_the_locked_out_attempt_raises_rather_than_answering(vault):
    """A refusal must not double as an oracle: while locked out, the *right*
    password gets the same treatment as a wrong one."""
    auth.lock_app()
    for _ in range(auth._MAX_ATTEMPTS):
        fail_once()

    with pytest.raises(auth.LoginRateLimited):
        auth.verify_master_password(MASTER)


def test_a_correct_password_still_opens_the_vault_below_the_limit(vault):
    auth.lock_app()
    for _ in range(auth._MAX_ATTEMPTS - 1):
        fail_once()

    assert auth.verify_master_password(MASTER) is True


# -- the count survives a restart ---------------------------------------------

def test_the_failure_count_is_persisted_as_it_goes(vault):
    auth.lock_app()
    fail_once()
    fail_once()
    assert attempts() == 2


def test_quitting_does_not_hand_back_the_attempts_already_spent(vault):
    """The in-memory globals are everything a restart clears. Four failures,
    a restart, and the fifth must still be the one that locks out — otherwise
    quitting the app is a free reset."""
    auth.lock_app()
    for _ in range(auth._MAX_ATTEMPTS - 1):
        fail_once()

    auth._lockout_deadline_monotonic = 0.0   # the restart
    auth._lockout_count = 0
    assert auth.check_login_rate_limit()[0] is True

    fail_once()
    assert auth.check_login_rate_limit()[0] is False


def test_a_lockout_written_before_the_restart_still_holds(vault):
    """The persisted half alone, with nothing left in memory."""
    database.write_config('lockout_started', str(time.time()))
    database.write_config('lockout_duration', '30')
    auth._lockout_deadline_monotonic = 0.0

    allowed, wait = auth.check_login_rate_limit()
    assert allowed is False
    assert 0 < wait <= 31


def test_a_successful_login_clears_every_counter(vault):
    auth.lock_app()
    fail_once()
    assert auth.verify_master_password(MASTER) is True

    assert attempts() == 0
    assert float(database.read_config('lockout_started')) == 0.0
    assert float(database.read_config('lockout_duration')) == 0.0
    assert auth._lockout_count == 0
    assert auth.check_login_rate_limit() == (True, 0)


# -- escalation and its ceiling -----------------------------------------------

def test_each_lockout_lasts_longer_than_the_one_before(vault):
    auth.lock_app()
    for _ in range(auth._MAX_ATTEMPTS):
        fail_once()
    first = float(database.read_config('lockout_duration'))

    for _ in range(auth._MAX_ATTEMPTS):
        auth.record_failed_login()
    second = float(database.read_config('lockout_duration'))

    assert first == auth._LOCKOUT_SECONDS
    assert second == auth._LOCKOUT_SECONDS * 2


def test_the_backoff_stops_climbing_at_the_cap(vault):
    for _ in range(12):
        for _ in range(auth._MAX_ATTEMPTS):
            auth.record_failed_login()

    assert float(database.read_config('lockout_duration')) == auth._LOCKOUT_CAP_SECONDS


# -- the clock is not trusted --------------------------------------------------

def test_a_clock_moved_forward_cannot_expire_the_lockout_in_session(vault, monkeypatch):
    """The persisted half is wall-clock and gives way, which no stored wall-clock
    value can prevent. The monotonic deadline is why that does not matter: within
    the session the limit still holds."""
    auth.lock_app()
    for _ in range(auth._MAX_ATTEMPTS):
        fail_once()

    ten_thousand_seconds_on = time.time() + 10_000
    monkeypatch.setattr(time, 'time', lambda: ten_thousand_seconds_on)
    assert auth._persisted_seconds_remaining() == 0.0
    assert auth.check_login_rate_limit()[0] is False


def test_a_clock_moved_backwards_cannot_stretch_the_wait(vault, monkeypatch):
    """A start time in the future means the clock went back. That must read as
    'just begun' — the stored duration — and never as an arbitrarily long wait
    the owner cannot escape."""
    database.write_config('lockout_started', str(time.time() + 50_000))
    database.write_config('lockout_duration', str(auth._LOCKOUT_SECONDS))

    assert auth._persisted_seconds_remaining() == auth._LOCKOUT_SECONDS


def test_a_stored_duration_beyond_the_cap_is_clamped(vault):
    """The clamp is what makes storing a wall-clock pair safe at all: whatever
    is in the row, the wait it can impose is bounded by the cap."""
    database.write_config('lockout_started', str(time.time()))
    database.write_config('lockout_duration', str(60 * 60 * 24 * 365))

    assert auth._persisted_seconds_remaining() == auth._LOCKOUT_CAP_SECONDS


def test_an_elapsed_lockout_lets_the_next_attempt_through(vault):
    database.write_config('lockout_started', str(time.time() - 3600))
    database.write_config('lockout_duration', str(auth._LOCKOUT_SECONDS))

    assert auth._persisted_seconds_remaining() == 0.0
    assert auth.check_login_rate_limit() == (True, 0)


# -- a database that will not answer ------------------------------------------

@pytest.mark.parametrize('value', ['', 'not a number', 'NaN-ish', '\x00'])
def test_an_unreadable_counter_fails_open_rather_than_bricking_the_vault(vault, value):
    """Deliberately fails open. The master hash lives in the same table, so a
    row this cannot read is a vault that cannot be opened anyway — and refusing
    to run would lock the owner out permanently over a corrupt integer."""
    database.write_config('lockout_duration', value)
    database.write_config('lockout_started', value)
    database.write_config('failed_login_attempts', value)

    assert auth._persisted_seconds_remaining() == 0.0
    assert auth._read_failed_attempts() == 0
    assert auth.check_login_rate_limit() == (True, 0)
