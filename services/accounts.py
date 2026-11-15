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
    FAVORITE_KEY, SPLIT_FIELDS, active_splits, add_amounts, field_shown, get_config,
    is_favorite, normalize_category, parse_iso_date, parse_money, schema_field,
    trim_number,
)
from core.database import ENCRYPTED_FIELDS, connect
from core.encryption import (
    VaultLockedError, decrypt_field, encrypt_field, is_unreadable,
    looks_encrypted, on_key_change, row_aad, safe_decrypt_field,
)


# Every encrypted field is bound to its column and its row, and padded; see
# encryption.row_aad and encryption.pad. So a field is never encrypted before
# its row has an id: new rows go in with empty fields first.
def account_aad(column: str, account_id) -> bytes:
    return row_aad('accounts', column, account_id)


def balance_aad(account_id, entry_id) -> bytes:
    return row_aad('balance_history', 'balance', account_id, entry_id)


# The encrypted text columns a caller supplies by name. 'category' is encrypted
# too but has rules of its own (see _category), and extra_data is a blob rather
# than a field.
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


# The parts of current_balance on a Both account. A backdated figure files its
# parts with it, and they wait on the same terms the figure does.
BALANCE_PARTS = SPLIT_FIELDS['current_balance']


def _part_amount(value, label: str) -> Decimal | None:
    """One part of a split total as an exact amount, None when it is blank.
    ValueError for an amount that is not one, or is negative: a part is money
    in an account, and the total it makes becomes the balance."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    number = None if isinstance(value, bool) else parse_money(value)
    if number is None:
        raise ValueError(f"{label} must be an amount, like 2,501.99.")
    if number < 0:
        raise ValueError(f"{label} can't be negative.")
    return Decimal(str(number))


def _stored_amount(value) -> Decimal | None:
    """A part as it was stored, or None when it was blank or unreadable."""
    number = None if isinstance(value, bool) or value is None else parse_money(value)
    return None if number is None else Decimal(str(number))


def _settle_fields(category: str, extra: dict, stored: dict | None):
    """Applies the rules between a category's fields to extra, in place,
    before it is saved. stored is what the account held, None for a new one.

    A field with shown_when or hidden_when is cleared once it stops applying:
    switching a 401(k) from Both to Traditional clears its two amounts, and
    changing its type to Brokerage clears its tax treatment as well. An Income
    record made Social Security loses its annual pay, though not the history
    that pay left. Only the category's own fields are touched; keys from
    another schema are kept, as they are everywhere else.

    On a Both account each total is the sum of its parts, worked out here so
    the form, Quick Update and an import all agree. It is worked out only when
    the parts change, or the account has just become split: parts left as they
    were keep the total the caller sent. Balance History can move a balance
    without its parts, and an unrelated edit must not then file the old parts'
    sum as a new reading."""
    category = normalize_category(category or '')
    for field in get_config(category)['fields']:
        if (field.get('shown_when') or field.get('hidden_when')) \
                and not field_shown(category, field, extra):
            extra.pop(field['key'], None)

    was_split = active_splits(category, stored) if stored is not None else {}
    for total, parts in active_splits(category, extra).items():
        amounts = [_part_amount(extra.get(key), schema_field(category, key)['label'])
                   for key in parts]
        if None in amounts:
            # Nothing to add up. The form insists on both; an import may not.
            continue
        for key, amount in zip(parts, amounts):
            extra[key] = trim_number(float(amount))
        unchanged = (total in was_split
                     and [_stored_amount(stored.get(key)) for key in parts] == amounts)
        if unchanged and extra.get(total) not in (None, ''):
            continue
        extra[total] = add_amounts(*amounts)


def _decode_extra(blob, account_id) -> dict:
    """Decrypts an extra_data blob into a dict; {} when it is absent, unreadable
    or not an object. Pair with _extra_is_readable before writing: {} means
    "nothing to show", not "nothing was there"."""
    if not blob:
        return {}
    try:
        parsed = json.loads(decrypt_field(blob, account_aad('extra_data', account_id)))
    except VaultLockedError:
        raise
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extra_is_readable(blob, account_id) -> bool:
    """False only when extra_data holds ciphertext this key cannot open — the
    one case where overwriting it would destroy fields never shown to the user."""
    if not blob or not looks_encrypted(blob):
        return True
    try:
        decrypt_field(blob, account_aad('extra_data', account_id))
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
            record[field] = safe_decrypt_field(record[field],
                                               account_aad(field, record['id']))

    extra = _decode_extra(record.pop('extra_data', None), record['id'])
    record['closed'] = bool(record.get('closed', 0))
    # Columns win over the blob, so an extra_data key named after a column (an
    # 'id', say) can never redirect later writes to a different row.
    record.update({key: value for key, value in extra.items() if key not in record})
    return record


def _entry_balance(entry, account_id) -> str:
    """A history row's figure, decrypted, or the UNREADABLE placeholder. entry
    needs its id and balance."""
    return safe_decrypt_field(entry['balance'], balance_aad(account_id, entry['id']))


def _insert_entry(db, account_id: int, balance: str, recorded_at: str) -> int:
    """Files one history row and returns its id. The row goes in empty and the
    figure follows: it is bound to the row's id, which only the insert makes."""
    entry_id = db.execute(
        "INSERT INTO balance_history (account_id, balance, recorded_at) VALUES (?, '', ?)",
        (account_id, recorded_at),
    ).lastrowid
    db.execute("UPDATE balance_history SET balance = ? WHERE id = ?",
               (encrypt_field(balance, balance_aad(account_id, entry_id)), entry_id))
    return entry_id


def _same_figure(entry, account_id, balance: str) -> bool:
    """Whether a stored history row's figure is the same amount as balance."""
    previous = _entry_balance(entry, account_id)
    try:
        # Decimal: '1.10' == '1.1' without float rounding collisions
        return Decimal(previous.strip()) == Decimal(balance)
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        return previous == balance


def _record_balance(db, account_id: int, extra: dict, now: str, force: bool = False,
                    confirm: bool = False):
    """Inserts an encrypted balance_history row if current_balance is present.

    confirm=True inserts unless the day being filed already ends on the same
    figure, and takes precedence over force. force=True always inserts.
    Otherwise skips when unchanged from the newest entry."""
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

    if confirm:
        # Against the last entry filed that day, not any of them: after 500 then
        # 600, a 500 is a new reading, and skipping it would leave the account
        # on 500 with history ending on 600.
        last = db.execute(
            "SELECT id, balance FROM balance_history "
            "WHERE account_id = ? AND substr(recorded_at, 1, 10) = ? "
            "ORDER BY id DESC LIMIT 1",
            (account_id, now[:10]),
        ).fetchone()
        if last is not None and _same_figure(last, account_id, balance):
            return
    elif not force:
        # Compared with the newest-dated entry, which is the figure the account
        # reads as current — not the last-inserted one, which after a backdated
        # save is an older figure.
        last = _newest_entry(db, account_id, _today())
        if last is not None and _same_figure(last, account_id, balance):
            return

    _insert_entry(db, account_id, balance, now)


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
            rows = db.execute("SELECT * FROM accounts").fetchall()
        # Sorted here, not in SQL: the category is ciphertext on disk. Python
        # compares by code point, which is the order SQLite's BINARY collation
        # gave on the UTF-8 text.
        _account_cache = sorted((_decrypt_row(row) for row in rows),
                                key=lambda a: (a.get('category') or '', a['id']))

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
    _settle_fields(category, extra, None)
    # Checked before the insert, so a value too long to store writes nothing.
    values = {f: _checked(columns.get(f), f) for f in COMMON_FIELDS}

    # In empty first: every field is bound to the id the insert hands back.
    new_id = db.execute("""
        INSERT INTO accounts (category, name, closed, last_updated, date_added)
        VALUES ('', '', ?, ?, ?)
    """, (int(bool(columns.get('closed'))), today, today)).lastrowid
    db.execute("""
        UPDATE accounts
           SET category = ?, name = ?, username = ?, password = ?, website = ?,
               notes = ?, extra_data = ?
         WHERE id = ?
    """, (
        encrypt_field(category, account_aad('category', new_id)),
        *(_encrypt_or_none(values[f], f, new_id) for f in COMMON_FIELDS),
        _encrypt_extra(extra, new_id),
        new_id,
    ))
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


def update_account(account_id: int, flat: dict, balance_date: str | None = None,
                   confirm_balance: bool = False) -> dict:
    """Saves an account and snapshots its current_balance.

    balance_date is the date the balance figure is from. Today or None records
    it now. An earlier date files the figure under that date, where it counts
    exactly as a history entry does (see _sync_current_balance): if it is the
    newest on file, the account follows it; otherwise it only fills in the past.

    confirm_balance files the figure even when it has not moved, as No Change
    does, unless that day already ends on the same figure. Quick Update passes
    it: saving there is a reading of the balance, where the full form is often
    just a note or a password.

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
        stored_category = (row['category'] if is_unreadable(category)
                           else encrypt_field(category, account_aad('category', account_id)))

        stored_extra = _decode_extra(row['extra_data'], account_id)
        _settle_fields(category, extra, stored_extra)

        # snapshot is what reaches history; extra is what the account keeps.
        # They differ only when backdating, which files the new figure but
        # leaves the account showing the one it had until the sync below. The
        # figure's Traditional and Roth parts wait with it.
        #
        # A backdated figure is always filed — the day is the point of it. An
        # ordinary save files only when the figure actually moved, so editing a
        # note does not add a history entry. A confirmed one (Quick Update) is
        # filed either way, short of repeating the figure its day ends on.
        backdating = bool(balance_date and balance_date < today)
        snapshot, recorded_at, last_updated = extra, now, today
        if backdating:
            snapshot, recorded_at = dict(extra), balance_date
            last_updated = row['last_updated']
            # The parts only while the account is split: on any other account
            # _settle_fields has just cleared them, and they stay cleared.
            split = 'current_balance' in active_splits(category, extra)
            for key in ('current_balance', *(BALANCE_PARTS if split else ())):
                if stored_extra.get(key) is None:
                    extra.pop(key, None)
                else:
                    extra[key] = stored_extra[key]

        # Unreadable extra_data decodes to {}, and writing that back would drop
        # every category-specific field at once.
        extra_blob = (_encrypt_extra(extra, account_id)
                      if _extra_is_readable(row['extra_data'], account_id)
                      else row['extra_data'])

        db.execute("""
            UPDATE accounts
               SET category = ?, name = ?, username = ?, password = ?, website = ?,
                   notes = ?, extra_data = ?, closed = ?, last_updated = ?
             WHERE id = ?
        """, (
            stored_category,
            *(row[f] if f in keep else _encrypt_or_none(_checked(columns.get(f), f), f, account_id)
              for f in COMMON_FIELDS),
            extra_blob,
            int(bool(columns.get('closed'))),
            last_updated, account_id,
        ))
        _record_balance(db, account_id, snapshot, recorded_at, force=backdating,
                        confirm=confirm_balance)
        if backdating:
            _sync_current_balance(db, account_id)
            _adopt_backdated_parts(db, account_id, category, snapshot, balance_date)
        row = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()

    invalidate_cache()
    return _decrypt_row(row)


def _adopt_backdated_parts(db, account_id: int, category: str, snapshot: dict,
                           balance_date: str):
    """Gives the account the Traditional and Roth parts filed with a backdated
    figure, when that figure turned out to be the current one: the newest
    entry on file is from that day and holds that figure. Behind a newer
    entry, the parts describe the past and the account keeps its own."""
    if 'current_balance' not in active_splits(category, snapshot):
        return
    parts = {key: snapshot[key] for key in BALANCE_PARTS if snapshot.get(key) is not None}
    total = snapshot.get('current_balance')
    if not parts or total is None:
        return
    newest = _newest_entry(db, account_id, _today())
    if (newest is None or str(newest['recorded_at'])[:10] != balance_date
            or not _same_figure(newest, account_id, str(total))):
        return
    row = db.execute("SELECT extra_data FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not _extra_is_readable(row['extra_data'], account_id):
        return
    extra = _decode_extra(row['extra_data'], account_id)
    extra.update(parts)
    db.execute("UPDATE accounts SET extra_data = ? WHERE id = ?",
               (_encrypt_extra(extra, account_id), account_id))


def touch_account(account_id: int) -> str:
    """Sets last_updated to today and records a balance snapshot if one exists,
    unless today already ends on the same figure, as Quick Update does.
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
        _record_balance(db, account_id, _decode_extra(row['extra_data'], account_id), now,
                        confirm=True)

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

        if not _extra_is_readable(row['extra_data'], account_id):
            raise ValueError(
                "This account's stored fields cannot be read, so it cannot be pinned")

        extra = _decode_extra(row['extra_data'], account_id)
        if favorite:
            extra[FAVORITE_KEY] = True
        else:
            extra.pop(FAVORITE_KEY, None)

        db.execute("UPDATE accounts SET extra_data = ? WHERE id = ?",
                   (_encrypt_extra(extra, account_id), account_id))
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
_BULK_COLUMNS = frozenset({'closed'})


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

    Encrypted once per row, in one transaction. A single token written to every
    row would show on disk which accounts share a category, which is what
    encrypting the column is meant to hide."""
    require_session()
    if not category or not str(category).strip():
        raise ValueError("Category is required")
    category = _category(category)
    ids = [int(i) for i in account_ids]
    changed = 0
    with connect() as db:
        for account_id in ids:
            changed += db.execute(
                "UPDATE accounts SET category = ? WHERE id = ?",
                (encrypt_field(category, account_aad('category', account_id)), account_id),
            ).rowcount
    invalidate_cache()
    return changed


def _encrypt_or_none(value, column: str, account_id):
    return encrypt_field(value, account_aad(column, account_id)) if value else None


def _encrypt_extra(extra: dict, account_id):
    if not extra:
        return None
    return encrypt_field(json.dumps(extra), account_aad('extra_data', account_id))


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
    if not _extra_is_readable(row['extra_data'], account_id):
        return
    extra = _decode_extra(row['extra_data'], account_id)
    if 'current_balance' not in extra:
        return
    extra.pop('current_balance')
    db.execute("UPDATE accounts SET extra_data = ? WHERE id = ?",
               (_encrypt_extra(extra, account_id), account_id))


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
        number = float(_entry_balance(newest, account_id).strip())
        if not math.isfinite(number):
            return
    except (AttributeError, TypeError, ValueError):
        # Unreadable, or legacy plaintext that was never a number.
        return
    balance = trim_number(number)

    # extra_data this key cannot open must not be overwritten.
    if not _extra_is_readable(row['extra_data'], account_id):
        return
    extra = _decode_extra(row['extra_data'], account_id)

    # last_updated only moves forward: filing an old statement does not make the
    # account staler than it was.
    last_updated = max(str(row['last_updated'] or ''), str(newest['recorded_at'])[:10])

    if extra.get('current_balance') == balance and row['last_updated'] == last_updated:
        return
    extra['current_balance'] = balance
    db.execute(
        "UPDATE accounts SET extra_data = ?, last_updated = ? WHERE id = ?",
        (_encrypt_extra(extra, account_id), last_updated, account_id),
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
        {'id': row['id'], 'balance': _entry_balance(row, account_id),
         'recorded_at': row['recorded_at']}
        for row in rows
    ]


def add_balance_entry(account_id: int, balance: str, recorded_at: str):
    require_session()
    balance, recorded_at = _validate_entry(balance, recorded_at)
    with connect() as db:
        if db.execute("SELECT id FROM accounts WHERE id = ?", (account_id,)).fetchone() is None:
            raise AccountNotFound("Account not found")
        _insert_entry(db, account_id, balance, recorded_at)
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
        for balance, recorded_at in prepared:
            _insert_entry(db, account_id, balance, recorded_at)
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
            (encrypt_field(balance, balance_aad(account_id, entry_id)), recorded_at, entry_id,
             account_id),
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
