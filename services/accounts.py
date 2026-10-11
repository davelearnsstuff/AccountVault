# -----------------------------------------------------------------------------
# services/accounts.py
# Account and balance-history storage.
#
# The database keeps a flat set of common columns (name, username, password,
# website, notes, closed, last_updated) plus an encrypted JSON blob
# (extra_data) for everything category-specific. Callers work with fully flat
# dicts; the split happens here.
# -----------------------------------------------------------------------------

import datetime
import json
import math
from decimal import Decimal, InvalidOperation

from core.auth import require_session
from core.categories import (
    FAVORITE_KEY, is_favorite, normalize_category, parse_iso_date, trim_number,
)
from core.database import ENCRYPTED_FIELDS, connect
from core.encryption import (
    VaultLockedError, decrypt, encrypt, field_aad, is_unreadable,
    looks_encrypted, on_key_change, safe_decrypt,
)

# Associated data for the two columns not addressed by name below. Every
# encrypted field is bound to its column; see encryption.field_aad.
EXTRA_AAD   = field_aad('accounts', 'extra_data')
BALANCE_AAD = field_aad('balance_history', 'balance')

# The encrypted text columns a caller supplies by name. 'category' is a column
# but is not encrypted, and extra_data is a blob rather than a field.
COMMON_FIELDS = ('name', 'username', 'password', 'website', 'notes')

# Flat keys that are account columns rather than extra_data entries. id and
# date_added are columns too, but split_flat drops them: they are never written
# from a caller's dict.
TOP_LEVEL_KEYS = frozenset({'category', 'closed', 'last_updated'} | set(COMMON_FIELDS))

# Encrypted text columns — extra_data is encrypted separately as a JSON blob.
ENCRYPTED_TEXT_FIELDS = [f for f in ENCRYPTED_FIELDS['accounts'] if f != 'extra_data']

# Every column of the two record tables, in schema order. restore_account
# interpolates column names into its INSERT, so it checks them against these.
ACCOUNT_COLUMNS = ('id', 'category', 'name', 'username', 'password', 'website',
                   'notes', 'extra_data', 'closed', 'date_added', 'last_updated')
HISTORY_COLUMNS = ('id', 'account_id', 'balance', 'recorded_at')

# Maximum length per column, in characters. A value past its limit is refused
# with an error naming the field — never cut short — and the form enforces the
# same numbers as you type.
MAX_LENGTHS = {
    'category': 100, 'name': 200, 'username': 500,
    'password': 500, 'website': 500, 'notes': 10_000,
}

_FIELD_NAMES = {
    'category': 'Category', 'name': 'Name', 'username': 'Username',
    'password': 'Password', 'website': 'Website', 'notes': 'Notes',
}


class AccountNotFound(LookupError):
    """Raised when an account or history entry id does not exist."""


def _today() -> str:
    return datetime.date.today().isoformat()


def _now() -> str:
    return datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


def _checked(value, key):
    """value as text, or ValueError if it is longer than its column allows.
    Passes None through."""
    if value is None:
        return None
    text = str(value)
    limit = MAX_LENGTHS.get(key)
    if limit and len(text) > limit:
        raise ValueError(f"{_FIELD_NAMES.get(key, key)} is too long — the limit "
                         f"is {limit:,} characters.")
    return text


def is_reserved_category(name) -> bool:
    """Whether a category name is shaped like the views' own tab keys
    (__all__, __closed__, __dashboard__ …), which a real category must never
    collide with."""
    text = str(name or '').strip()
    return len(text) > 4 and text.startswith('__') and text.endswith('__')


def _category(value) -> str:
    """The category to store, defaulting to Websites. Refuses an over-long or
    reserved name."""
    category = _checked(value or 'Websites', 'category')
    if is_reserved_category(category):
        raise ValueError(f"'{category}' cannot be used as a category name.")
    return category


def _require_name(columns: dict) -> str:
    """name is NOT NULL in the schema, so reject a blank one before sqlite does."""
    name = _checked(columns.get('name'), 'name')
    if not name or not name.strip():
        raise ValueError("Name is required")
    return name


def split_flat(flat: dict) -> tuple[dict, dict]:
    """Splits a flat account dict into (column values, extra_data).

    Underscored keys are dropped: they are the views' own bookkeeping
    ('_category', '_match'), never a field of the account, and would otherwise
    be encrypted into extra_data by whichever save path received them."""
    columns = {}
    extra = {}
    for key, value in flat.items():
        if key in ('id', 'date_added') or key.startswith('_'):
            continue
        if key in TOP_LEVEL_KEYS:
            columns[key] = value
        else:
            extra[key] = value
    return columns, extra


def _decode_extra(blob) -> dict:
    """Decrypts an extra_data blob into a dict; {} when it is absent, unreadable
    or not an object. Pair with _extra_is_readable before writing: {} means
    "nothing to show", not "nothing was there"."""
    if not blob:
        return {}
    try:
        parsed = json.loads(decrypt(blob, EXTRA_AAD))
    except VaultLockedError:
        raise
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extra_is_readable(blob) -> bool:
    """False only when extra_data holds ciphertext this key cannot open — the
    one case where overwriting it would destroy fields never shown to the user."""
    if not blob or not looks_encrypted(blob):
        return True
    try:
        decrypt(blob, EXTRA_AAD)
        return True
    except VaultLockedError:
        raise
    except Exception:
        return False


def _decrypt_row(row) -> dict:
    """Decrypts a DB row into a flat account dict.

    A corrupt column degrades to the UNREADABLE placeholder rather than hiding
    the whole vault behind one bad field; the write paths below refuse to store
    that placeholder over the original."""
    record = dict(row)
    for field in ENCRYPTED_TEXT_FIELDS:
        if record.get(field):
            record[field] = safe_decrypt(record[field], field_aad('accounts', field))

    extra = _decode_extra(record.pop('extra_data', None))
    record['closed'] = bool(record.get('closed', 0))
    # Columns win over the blob, so an extra_data key named after a column (an
    # 'id', say) can never redirect later writes to a different row.
    record.update({key: value for key, value in extra.items() if key not in record})
    return record


def _record_balance(db, account_id: int, extra: dict, now: str, force: bool = False):
    """Inserts an encrypted balance_history row if current_balance is present.
    With force=True always inserts; otherwise skips when unchanged from the
    newest entry."""
    value = (extra if isinstance(extra, dict) else {}).get('current_balance')
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return
    balance = str(value).strip()
    if not balance:
        return
    try:
        if not math.isfinite(float(balance)):
            return
    except (ValueError, TypeError):
        return

    if not force:
        # Compared with the newest-dated entry, which is the figure the account
        # reads as current — not the last-inserted one, which after a backdated
        # save is an older figure.
        last = _newest_entry(db, account_id, _today())
        if last is not None:
            previous = safe_decrypt(last['balance'], BALANCE_AAD)
            try:
                # Decimal: '1.10' == '1.1' without float rounding collisions
                if Decimal(previous.strip()) == Decimal(balance):
                    return
            except (InvalidOperation, ValueError, TypeError, AttributeError):
                if previous == balance:
                    return

    db.execute(
        "INSERT INTO balance_history (account_id, balance, recorded_at) VALUES (?, ?, ?)",
        (account_id, encrypt(balance, BALANCE_AAD), now),
    )


# -----------------------------------------------------------------------------
# Decrypted account cache
#
# list_accounts() decrypts six fields and a JSON blob per row, and the UI calls
# it on every search keystroke, category switch and save. The cache holds the
# decrypted rows and is dropped on two events:
#
#   * any write here — every mutating function below calls invalidate_cache();
#   * any change of the session key, via encryption.on_key_change. That covers
#     the lock: this is plaintext and must not outlive the key that opened it.
#
# Callers get shallow copies. Every value is a str, number, bool or None, so a
# shallow copy is a real copy, and the UI does mutate what it is handed.
# -----------------------------------------------------------------------------

_account_cache: list[dict] | None = None


def invalidate_cache():
    """Drops the decrypted account cache."""
    global _account_cache
    _account_cache = None


on_key_change(invalidate_cache)


def cache_is_warm() -> bool:
    """Whether the cache currently holds anything. For tests — nothing in the
    app should branch on this."""
    return _account_cache is not None


def list_accounts(category: str | None = None) -> list[dict]:
    require_session()
    global _account_cache
    if _account_cache is None:
        with connect() as db:
            rows = db.execute("SELECT * FROM accounts ORDER BY category, id").fetchall()
        _account_cache = [_decrypt_row(row) for row in rows]

    if category:
        # Compared normalized, as every other read path does, so a row written
        # before a category rename is not invisible here alone.
        wanted = normalize_category(category)
        return [dict(a) for a in _account_cache
                if normalize_category(a.get('category') or '') == wanted]
    return [dict(a) for a in _account_cache]


def count_accounts() -> int:
    """How many accounts the vault holds, without decrypting any of them."""
    require_session()
    if _account_cache is not None:
        return len(_account_cache)
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]


def get_account(account_id: int) -> dict:
    require_session()
    with connect() as db:
        row = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise AccountNotFound("Account not found")
    return _decrypt_row(row)


def _without_placeholders(values: dict) -> dict:
    """values minus anything holding the UNREADABLE placeholder. A new record
    can only receive one by copying a field this vault could not decrypt (a CSV
    exported from a damaged vault, say), and storing it would turn a display
    marker into somebody's password."""
    return {key: value for key, value in values.items() if not is_unreadable(value)}


def _insert_account(db, flat: dict, now: str, today: str) -> int:
    """Writes one new account on an open connection and returns its id.

    Shared by create_account and the bulk import, so an imported row is
    validated and stored exactly as one typed into the form is."""
    columns, extra = split_flat(flat)
    columns, extra = _without_placeholders(columns), _without_placeholders(extra)
    category = _category(columns.get('category'))
    columns['name'] = _require_name(columns)

    cursor = db.execute("""
        INSERT INTO accounts (category, name, username, password, website, notes,
                              extra_data, closed, last_updated, date_added)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        category,
        *(_encrypt_or_none(_checked(columns.get(f), f), f) for f in COMMON_FIELDS),
        _encrypt_extra(extra),
        int(bool(columns.get('closed'))),
        today, today,
    ))
    new_id = cursor.lastrowid
    _record_balance(db, new_id, extra, now)
    return new_id


def create_account(flat: dict) -> dict:
    require_session()
    now, today = _now(), _today()
    with connect() as db:
        new_id = _insert_account(db, flat, now, today)
        row = db.execute("SELECT * FROM accounts WHERE id = ?", (new_id,)).fetchone()

    invalidate_cache()
    return _decrypt_row(row)


def create_accounts(flats) -> int:
    """Writes several accounts in one transaction. Returns how many landed.

    All or nothing: a partial import leaves the user no way to tell which
    accounts arrived, and re-running the file would double the ones that did."""
    require_session()
    flats = list(flats or [])
    if not flats:
        return 0
    now, today = _now(), _today()
    with connect() as db:
        for flat in flats:
            _insert_account(db, flat, now, today)
    invalidate_cache()
    return len(flats)


def update_account(account_id: int, flat: dict, balance_date: str | None = None) -> dict:
    """Saves an account and snapshots its current_balance.

    balance_date is the date the balance figure is from. Today or None records
    it now. An earlier date files the figure under that date, where it counts
    exactly as a history entry does (see _sync_current_balance): if it is the
    newest on file, the account follows it; otherwise it only fills in the past.

    The row and its history entry are written in one transaction."""
    require_session()
    columns, extra = split_flat(flat)
    category = _category(columns.get('category'))
    columns['name'] = _require_name(columns)
    now, today = _now(), _today()

    # Validated rather than trusted: it goes straight into recorded_at, which
    # every history table, chart and month bucket orders and slices on.
    if balance_date is not None:
        balance_date = _validate_date(balance_date)

    with connect() as db:
        row = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise AccountNotFound("Account not found")

        # A field arriving as the UNREADABLE placeholder is what _decrypt_row
        # showed for ciphertext this key cannot open, round-tripped by the form.
        # Keep the stored column: still unreadable, but not destroyed.
        keep = {f for f in COMMON_FIELDS if is_unreadable(columns.get(f))}

        # snapshot is what reaches history; extra is what the account keeps.
        # They differ only when backdating, which files the new figure but
        # leaves the account showing the one it had until the sync below.
        #
        # A backdated figure is always filed — the day is the point of it. An
        # ordinary save files only when the figure actually moved, so editing a
        # note does not add a history entry.
        backdating = bool(balance_date and balance_date < today)
        snapshot, recorded_at, last_updated = extra, now, today
        if backdating:
            snapshot, recorded_at = dict(extra), balance_date
            last_updated = row['last_updated']
            stored = _decode_extra(row['extra_data']).get('current_balance')
            if stored is None:
                extra.pop('current_balance', None)
            else:
                extra['current_balance'] = stored

        # Unreadable extra_data decodes to {}, and writing that back would drop
        # every category-specific field at once.
        extra_blob = (_encrypt_extra(extra) if _extra_is_readable(row['extra_data'])
                      else row['extra_data'])

        db.execute("""
            UPDATE accounts
               SET category = ?, name = ?, username = ?, password = ?, website = ?,
                   notes = ?, extra_data = ?, closed = ?, last_updated = ?
             WHERE id = ?
        """, (
            category,
            *(row[f] if f in keep else _encrypt_or_none(_checked(columns.get(f), f), f)
              for f in COMMON_FIELDS),
            extra_blob,
            int(bool(columns.get('closed'))),
            last_updated, account_id,
        ))
        _record_balance(db, account_id, snapshot, recorded_at, force=backdating)
        if backdating:
            _sync_current_balance(db, account_id)
        row = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()

    invalidate_cache()
    return _decrypt_row(row)


def touch_account(account_id: int) -> str:
    """Sets last_updated to today and records a balance snapshot if one exists.
    Returns the new last_updated date."""
    require_session()
    now, today = _now(), _today()

    with connect() as db:
        if db.execute("SELECT id FROM accounts WHERE id = ?", (account_id,)).fetchone() is None:
            raise AccountNotFound("Account not found")

        db.execute("UPDATE accounts SET last_updated = ? WHERE id = ?", (today, account_id))
        # An account can hold history while its own figure is empty (cleared, or
        # moved in from a category without the field). Recover the figure from
        # history first, or the snapshot below would find nothing to file.
        _sync_current_balance(db, account_id)
        row = db.execute(
            "SELECT extra_data FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        _record_balance(db, account_id, _decode_extra(row['extra_data']), now, force=True)

    invalidate_cache()
    return today


def delete_account(account_id: int) -> dict:
    """Deletes an account and returns everything needed to put it back.

    The token is the row and its history exactly as they sat on disk — still
    encrypted, so an undo the user is still thinking about holds no plaintext.
    The id travels with it: AUTOINCREMENT never reissues it, so the restore can
    reclaim it and the history rows keep pointing at the right account."""
    require_session()
    with connect() as db:
        row = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise AccountNotFound("Account not found")
        history = db.execute(
            "SELECT * FROM balance_history WHERE account_id = ? ORDER BY id",
            (account_id,),
        ).fetchall()
        db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    invalidate_cache()
    return {'account': dict(row), 'history': [dict(entry) for entry in history]}


def restore_account(token: dict) -> int:
    """Puts back an account delete_account removed. Returns its id.

    One transaction for the row and its history, written back verbatim: the
    ciphertext never left the vault and the key has not changed, so there is
    nothing to re-encrypt and no chance to lose a field that will not open.

    A token from another vault would write rows no key here can open, so the
    caller drops its token when the vault locks or changes."""
    require_session()
    account = dict((token or {}).get('account') or {})
    account_id = account.get('id')
    if not account_id:
        raise ValueError("Nothing to restore")

    columns = [c for c in ACCOUNT_COLUMNS if c in account]
    names = ', '.join(columns)
    placeholders = ', '.join('?' * len(columns))

    with connect() as db:
        if db.execute("SELECT id FROM accounts WHERE id = ?",
                      (account_id,)).fetchone() is not None:
            raise ValueError("That account is already back")
        db.execute(f"INSERT INTO accounts ({names}) VALUES ({placeholders})",
                   [account[c] for c in columns])
        for entry in (token.get('history') or []):
            entry = dict(entry)
            # Forced onto the restored row rather than trusted from the token.
            entry['account_id'] = account_id
            history_columns = [c for c in HISTORY_COLUMNS if c in entry]
            history_names = ', '.join(history_columns)
            history_places = ', '.join('?' * len(history_columns))
            db.execute(
                f"INSERT INTO balance_history ({history_names}) "
                f"VALUES ({history_places})",
                [entry[c] for c in history_columns],
            )
    invalidate_cache()
    return account_id


def set_favorite(account_id: int, favorite: bool) -> bool:
    """Pins or unpins one account. Returns the state it is now in.

    Narrow on purpose: update_account would re-encrypt every column, file a
    history entry, and move last_updated — and pinning verifies nothing about
    the account. The flag is dropped rather than stored as false when
    unpinning."""
    require_session()
    favorite = bool(favorite)
    with connect() as db:
        row = db.execute("SELECT extra_data FROM accounts WHERE id = ?",
                         (account_id,)).fetchone()
        if row is None:
            raise AccountNotFound("Account not found")

        if not _extra_is_readable(row['extra_data']):
            raise ValueError(
                "This account's stored fields cannot be read, so it cannot be pinned")

        extra = _decode_extra(row['extra_data'])
        if favorite:
            extra[FAVORITE_KEY] = True
        else:
            extra.pop(FAVORITE_KEY, None)

        db.execute("UPDATE accounts SET extra_data = ? WHERE id = ?",
                   (_encrypt_extra(extra), account_id))
    invalidate_cache()
    return favorite


def favorite_count() -> int:
    """How many accounts are pinned, closed ones included."""
    return sum(1 for account in list_accounts() if is_favorite(account))


# -----------------------------------------------------------------------------
# Bulk edits
#
# Narrow UPDATEs rather than a loop over update_account(), which would file a
# balance-history entry per account and move each one's figure. Neither moves
# last_updated: closing or recategorising an account verifies nothing about it.
# -----------------------------------------------------------------------------

# The only columns _bulk_update may write. The name is interpolated into the
# statement (sqlite has no parameter form for an identifier), so it is checked.
_BULK_COLUMNS = frozenset({'closed', 'category'})


def _bulk_update(account_ids, column: str, value) -> int:
    if column not in _BULK_COLUMNS:
        raise ValueError(f"{column} is not a bulk-editable column")
    ids = [int(i) for i in account_ids]
    if not ids:
        return 0
    placeholders = ','.join('?' * len(ids))
    with connect() as db:
        cursor = db.execute(
            f"UPDATE accounts SET {column} = ? WHERE id IN ({placeholders})",
            (value, *ids),
        )
        changed = cursor.rowcount
    invalidate_cache()
    return changed


def set_closed(account_ids, closed: bool) -> int:
    """Opens or closes several accounts at once. Returns the number changed."""
    require_session()
    return _bulk_update(account_ids, 'closed', int(bool(closed)))


def set_category(account_ids, category: str) -> int:
    """Moves several accounts to another category. Returns the number changed.

    The category column is structure rather than content, so it is not
    encrypted."""
    require_session()
    if not category or not str(category).strip():
        raise ValueError("Category is required")
    return _bulk_update(account_ids, 'category', _category(category))


def _encrypt_or_none(value, column: str):
    return encrypt(value, field_aad('accounts', column)) if value else None


def _encrypt_extra(extra: dict):
    return encrypt(json.dumps(extra), EXTRA_AAD) if extra else None


# -----------------------------------------------------------------------------
# Balance history
# -----------------------------------------------------------------------------

def _validate_date(recorded_at) -> str:
    """A YYYY-MM-DD date, or ValueError. The one place a date is admitted to
    balance_history; the spelling is categories.parse_iso_date's to decide."""
    date = parse_iso_date(recorded_at)
    if date is None:
        raise ValueError("Date must be a valid calendar date")
    return date.isoformat()


def _validate_entry(balance: str, recorded_at: str) -> tuple[str, str]:
    balance = str(balance).strip()
    try:
        if not math.isfinite(float(balance)):
            raise ValueError("balance must be a finite number")
    except (ValueError, TypeError):
        raise ValueError("Balance must be a numeric value")

    return balance, _validate_date(recorded_at)


# Newest first. Ordered on the date alone: an entry filed by hand carries a bare
# date and one written by a save a full timestamp, so comparing the raw values
# would rank same-day entries by format. Same date falls back to insertion order.
_NEWEST_FIRST = "ORDER BY substr(recorded_at, 1, 10) DESC, id DESC"


def _newest_entry(db, account_id: int, today: str):
    """The entry that says what the balance is now, or None. Future-dated entries
    are skipped: a balance from the future is a typo, and would pin the account
    to it until deleted."""
    return db.execute(
        "SELECT id, balance, recorded_at FROM balance_history "
        f"WHERE account_id = ? AND substr(recorded_at, 1, 10) <= ? {_NEWEST_FIRST} LIMIT 1",
        (account_id, today),
    ).fetchone()


def _clear_current_balance(db, account_id: int, row):
    """Drops the account's current_balance, leaving everything else — including
    last_updated — alone."""
    if not _extra_is_readable(row['extra_data']):
        return
    extra = _decode_extra(row['extra_data'])
    if 'current_balance' not in extra:
        return
    extra.pop('current_balance')
    db.execute("UPDATE accounts SET extra_data = ? WHERE id = ?",
               (_encrypt_extra(extra), account_id))


def _sync_current_balance(db, account_id: int):
    """Points the account's current_balance at its newest history entry.

    History is a way of updating the balance, not only a log of it: an entry
    dated on or after every other one says what the balance is now, and one
    that lands behind only fills in the past. Every history mutation runs this,
    so deleting a mistyped entry drops the account back to the one before it.
    Callers hold the transaction."""
    row = db.execute(
        "SELECT extra_data, last_updated FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None:
        return

    today = _today()
    newest = _newest_entry(db, account_id, today)
    if newest is None:
        remaining = db.execute(
            "SELECT 1 FROM balance_history WHERE account_id = ? LIMIT 1", (account_id,)
        ).fetchone()
        if remaining is not None:
            # Future-dated entries only: keep the figure the account has.
            return
        # The history is empty — the last entry has just been deleted.
        _clear_current_balance(db, account_id, row)
        return

    try:
        number = float(safe_decrypt(newest['balance'], BALANCE_AAD).strip())
        if not math.isfinite(number):
            return
    except (AttributeError, TypeError, ValueError):
        # Unreadable, or legacy plaintext that was never a number.
        return
    balance = trim_number(number)

    # extra_data this key cannot open must not be overwritten.
    if not _extra_is_readable(row['extra_data']):
        return
    extra = _decode_extra(row['extra_data'])

    # last_updated only moves forward: filing an old statement does not make the
    # account staler than it was.
    last_updated = max(str(row['last_updated'] or ''), str(newest['recorded_at'])[:10])

    if extra.get('current_balance') == balance and row['last_updated'] == last_updated:
        return
    extra['current_balance'] = balance
    db.execute(
        "UPDATE accounts SET extra_data = ?, last_updated = ? WHERE id = ?",
        (_encrypt_extra(extra), last_updated, account_id),
    )


def list_balance_history(account_id: int) -> list[dict]:
    require_session()
    with connect() as db:
        rows = db.execute(
            "SELECT id, balance, recorded_at FROM balance_history "
            f"WHERE account_id = ? {_NEWEST_FIRST}",
            (account_id,),
        ).fetchall()
    return [
        {'id': row['id'], 'balance': safe_decrypt(row['balance'], BALANCE_AAD),
         'recorded_at': row['recorded_at']}
        for row in rows
    ]


def add_balance_entry(account_id: int, balance: str, recorded_at: str):
    require_session()
    balance, recorded_at = _validate_entry(balance, recorded_at)
    with connect() as db:
        if db.execute("SELECT id FROM accounts WHERE id = ?", (account_id,)).fetchone() is None:
            raise AccountNotFound("Account not found")
        db.execute(
            "INSERT INTO balance_history (account_id, balance, recorded_at) VALUES (?, ?, ?)",
            (account_id, encrypt(balance, BALANCE_AAD), recorded_at),
        )
        _sync_current_balance(db, account_id)
    # A history write can move the account's figure, so it is an account write.
    invalidate_cache()


def add_balance_entries(account_id: int, entries) -> int:
    """Files a batch of (balance, recorded_at) entries in one transaction.

    Every row is validated before the first is written, so one bad line leaves
    the account untouched. Insertion follows the order given; the caller sorts
    oldest first, so among rows sharing a date the file's last one wins."""
    require_session()
    prepared = [_validate_entry(balance, recorded_at) for balance, recorded_at in entries]
    if not prepared:
        return 0

    with connect() as db:
        if db.execute("SELECT id FROM accounts WHERE id = ?", (account_id,)).fetchone() is None:
            raise AccountNotFound("Account not found")
        db.executemany(
            "INSERT INTO balance_history (account_id, balance, recorded_at) VALUES (?, ?, ?)",
            [(account_id, encrypt(balance, BALANCE_AAD), recorded_at)
             for balance, recorded_at in prepared],
        )
        _sync_current_balance(db, account_id)
    invalidate_cache()
    return len(prepared)


def update_balance_entry(account_id: int, entry_id: int, balance: str, recorded_at: str):
    require_session()
    balance, recorded_at = _validate_entry(balance, recorded_at)
    with connect() as db:
        row = db.execute(
            "SELECT id FROM balance_history WHERE id = ? AND account_id = ?",
            (entry_id, account_id),
        ).fetchone()
        if row is None:
            raise AccountNotFound("Entry not found")
        db.execute(
            "UPDATE balance_history SET balance = ?, recorded_at = ? WHERE id = ? AND account_id = ?",
            (encrypt(balance, BALANCE_AAD), recorded_at, entry_id, account_id),
        )
        _sync_current_balance(db, account_id)
    invalidate_cache()


def delete_balance_entry(account_id: int, entry_id: int):
    """Removes one history entry and re-points the account at what is left.
    Raises AccountNotFound for an entry that was not there."""
    require_session()
    with connect() as db:
        cursor = db.execute(
            "DELETE FROM balance_history WHERE id = ? AND account_id = ?",
            (entry_id, account_id),
        )
        if not cursor.rowcount:
            raise AccountNotFound("Entry not found")
        _sync_current_balance(db, account_id)
    invalidate_cache()
