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

from auth import require_session
from categories import (
    FAVORITE_KEY, is_favorite, normalize_category, parse_iso_date, trim_number,
)
from database import ENCRYPTED_FIELDS, connect
from encryption import (
    VaultLockedError, decrypt, encrypt, field_aad, is_unreadable,
    looks_encrypted, on_key_change, safe_decrypt,
)

# Associated data for the two columns that are not addressed by name below.
# Every encrypted field is bound to the column it lives in, so a token moved
# from one column to another stops opening — see encryption.field_aad. The text
# columns get theirs from field_aad('accounts', field) at the point of use,
# where the name is already in hand.
EXTRA_AAD   = field_aad('accounts', 'extra_data')
BALANCE_AAD = field_aad('balance_history', 'balance')

# The encrypted text columns a caller supplies by name. 'category' is a column
# too but is not encrypted, and extra_data is a blob rather than a field, so
# neither belongs in this list — see TOP_LEVEL_KEYS for the full column set.
COMMON_FIELDS = ('name', 'username', 'password', 'website', 'notes')

# Flat keys that are account columns rather than extra_data entries. id and
# date_added are columns too, but split_flat drops them before consulting this
# — they are never written from a caller's dict.
TOP_LEVEL_KEYS = frozenset({'category', 'closed', 'last_updated'} | set(COMMON_FIELDS))

# Encrypted text columns — extra_data is encrypted separately as a JSON blob.
# Derived from ENCRYPTED_FIELDS rather than written out again: two lists that
# must agree are one list that can drift.
ENCRYPTED_TEXT_FIELDS = [f for f in ENCRYPTED_FIELDS['accounts'] if f != 'extra_data']

# Every column of the two record tables, in schema order.
#
# Only restore_account uses these, and it needs them because it writes a row
# back column for column rather than field by field: the names are interpolated
# into its INSERT, so they are checked against these tuples instead of being
# trusted because they arrived in a dict this module produced. Kept in schema
# order so a restored row's INSERT reads like the CREATE TABLE it matches.
ACCOUNT_COLUMNS = ('id', 'category', 'name', 'username', 'password', 'website',
                   'notes', 'extra_data', 'closed', 'date_added', 'last_updated')
HISTORY_COLUMNS = ('id', 'account_id', 'balance', 'recorded_at')

# Maximum stored length per column, mirroring the old pydantic models.
MAX_LENGTHS = {
    'category': 100, 'name': 200, 'username': 500,
    'password': 500, 'website': 500, 'notes': 2000,
}


class AccountNotFound(LookupError):
    """Raised when an account or history entry id does not exist."""


def _today() -> str:
    return datetime.date.today().isoformat()


def _now() -> str:
    return datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


def _clip(value, key):
    """Trims a value to its column's maximum length; passes None through."""
    if value is None:
        return None
    text = str(value)
    limit = MAX_LENGTHS.get(key)
    return text[:limit] if limit else text


def _require_name(columns: dict) -> str:
    """name is NOT NULL in the schema, so reject a blank one before we hit sqlite."""
    name = _clip(columns.get('name'), 'name')
    if not name or not name.strip():
        raise ValueError("Name is required")
    return name


def split_flat(flat: dict) -> tuple[dict, dict]:
    """Splits a flat account dict into (column values, extra_data).

    Underscored keys are dropped wholesale rather than named one at a time.
    They are the views' own bookkeeping — '_category' is the normalized
    category a row was rendered under, '_match' is which field a search hit —
    and nothing that starts with one is ever a field of the account. Named
    individually, '_match' was missed: Quick Update saves a copy of the row on
    screen, so editing a *search result* wrote the match dict into extra_data,
    where it was encrypted, read back onto the record forever after, and
    carried into every .vault backup. ui.account_form.values() already filtered
    on the prefix for the same reason; doing it here means a new view's
    bookkeeping cannot reach the database whichever save path it takes."""
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
    or holds something other than an object. Pair with _extra_is_readable before
    writing: {} means "nothing to show", not "nothing was there"."""
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
    one case where overwriting it would destroy fields we were never able to
    show the user in the first place."""
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

    safe_decrypt rather than decrypt: a single corrupt column used to raise
    straight out of list_accounts and hide every account in the vault behind
    one bad field. It now degrades to the UNREADABLE placeholder, and the
    write paths below refuse to store that placeholder over the original."""
    record = dict(row)
    for field in ENCRYPTED_TEXT_FIELDS:
        if record.get(field):
            record[field] = safe_decrypt(record[field], field_aad('accounts', field))

    extra = _decode_extra(record.pop('extra_data', None))
    record['closed'] = bool(record.get('closed', 0))
    # Columns win over the blob. extra_data holds whatever key space the record
    # was written with, which back when records could arrive from an imported
    # .vault meant a record could carry an 'id' of its own, replace the real
    # one, and send every write the UI made afterwards (an edit, a delete) to
    # whichever row that id named. Nothing outside this module writes accounts
    # now, but the precedence stays: split_flat never routes an account column
    # into extra, so nothing this app writes is dropped by the filter, and a
    # vault written before that was true is still read safely.
    record.update({key: value for key, value in extra.items() if key not in record})
    return record


def _record_balance(db, account_id: int, extra: dict, now: str, force: bool = False):
    """Inserts an encrypted balance_history row if current_balance is present.
    With force=True always inserts; otherwise skips when unchanged from the last entry."""
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
        # The newest-dated entry, not the last-inserted one. A backdated save
        # files a row with the highest id and an older date, so comparing on id
        # asked "does this differ from that older figure?" — and every ordinary
        # save afterwards answered yes and logged a fresh entry for a balance
        # that had not moved. _newest_entry is the same row the account reads
        # its current value from, which is the figure this is really comparing.
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
# Accounts
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Decrypted account cache
#
# list_accounts() decrypts six fields per row plus a JSON blob, and the UI calls
# it constantly: on every debounced search keystroke, every category switch, and
# after every save and delete. At thirty accounts that is invisible; at several
# hundred the search box goes sticky, because each keystroke was paying for the
# whole vault again.
#
# The cache holds the decrypted rows and is dropped on two events:
#
#   * any write here — every mutating function below calls _invalidate(), which
#     is why they all live in this one module;
#   * any change of the session key, via encryption.on_key_change. That covers
#     the lock, and it is the security-relevant half: this is plaintext, so it
#     must not outlive the key that opened it. Registering here rather than
#     letting auth.lock_app remember to call us keeps the two impossible to get
#     out of step.
#
# Callers get shallow copies. Every value in a decrypted row is a str, number,
# bool or None, so a shallow copy is a real copy — and the UI does mutate what
# it is handed (_flatten adds '_category', search adds '_match').
# -----------------------------------------------------------------------------

_account_cache: list[dict] | None = None


def invalidate_cache():
    """Drops the decrypted account cache.

    Every write path in this module already calls it through _invalidate, and
    no other module writes these tables directly any more. It stays public for
    the tests, which stand rows up behind the module's back and need to say so.
    """
    global _account_cache
    _account_cache = None


_invalidate = invalidate_cache

on_key_change(_invalidate)


def cache_is_warm() -> bool:
    """Whether the cache currently holds anything. For tests and diagnostics —
    nothing in the app should branch on this."""
    return _account_cache is not None


def list_accounts(category: str | None = None) -> list[dict]:
    require_session()
    global _account_cache
    if _account_cache is None:
        with connect() as db:
            rows = db.execute("SELECT * FROM accounts ORDER BY category, id").fetchall()
        _account_cache = [_decrypt_row(row) for row in rows]

    if category:
        # Same order the old per-category query gave: filtering a list already
        # sorted by (category, id) leaves one category in id order.
        #
        # Compared normalized, as every other read path does — the category
        # tabs, the dashboard and categories.account_category all normalize
        # first. Matching the raw stored string made a row written before a
        # category rename invisible here and nowhere else in the app.
        wanted = normalize_category(category)
        return [dict(a) for a in _account_cache
                if normalize_category(a.get('category') or '') == wanted]
    return [dict(a) for a in _account_cache]


def count_accounts() -> int:
    """How many accounts the vault holds, without decrypting any of them.

    The CSV export needs this before its file dialog, only to answer "nothing to
    export" without making the user pick a path first. Going through
    list_accounts() for it decrypted six fields and a JSON blob per row to take
    a len() — and then threw the rows away and decrypted them all again."""
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


def _insert_account(db, flat: dict, now: str, today: str) -> int:
    """Writes one new account on an open connection and returns its id.

    Split out of create_account so the bulk import can write a whole file
    inside one transaction without a second copy of the insert. Everything the
    single-record path enforces — the category default, the required name, the
    per-column clipping, the initial balance snapshot — happens here, so a row
    that arrived from a CSV is stored exactly as one typed into the form is."""
    columns, extra = split_flat(flat)
    category = _clip(columns.get('category') or 'Websites', 'category')
    columns['name'] = _require_name(columns)

    cursor = db.execute("""
        INSERT INTO accounts (category, name, username, password, website, notes,
                              extra_data, closed, last_updated, date_added)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        category,
        # name is already clipped by _require_name above.
        *(_encrypt_or_none(_clip(columns.get(f), f), f) for f in COMMON_FIELDS),
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

    _invalidate()
    return _decrypt_row(row)


def create_accounts(flats) -> int:
    """Writes several accounts in one transaction. Returns how many landed.

    All or nothing, which is the whole reason this exists rather than a loop
    over create_account in the caller. An import is a file the user is handing
    over as a unit: a row that fails halfway through leaves them with no way to
    tell which of their accounts arrived without reading the list against the
    vault by hand, and re-running the file would then double everything that
    did. Failing whole means the fix is to correct the file and try again.

    Each row still goes through _insert_account, so nothing about an imported
    account is stored differently from one typed in."""
    require_session()
    flats = list(flats or [])
    if not flats:
        return 0
    now, today = _now(), _today()
    with connect() as db:
        for flat in flats:
            _insert_account(db, flat, now, today)
    _invalidate()
    return len(flats)


def update_account(account_id: int, flat: dict, balance_date: str | None = None) -> dict:
    """Saves an account and snapshots its current_balance.

    balance_date is the date the balance figure is from; anything but a date
    before today (the default) records it now, as usual. An earlier date makes
    the figure history alone — it says what the balance *was*, so it is filed
    under that date while the account keeps its stored balance and its
    last_updated stamp, and neither the headline value nor the staleness
    colouring moves for a statement that isn't current.

    The row and its history entry are written in one transaction, so a save can
    never land without the entry that belongs to it."""
    require_session()
    columns, extra = split_flat(flat)
    category = _clip(columns.get('category') or 'Websites', 'category')
    columns['name'] = _require_name(columns)
    now, today = _now(), _today()

    # Validated rather than compared as a raw string. The UI only ever sends
    # 'YYYY-MM-DD', but this is a public entry point and the value goes straight
    # into balance_history.recorded_at, which the whole app then orders and
    # slices on — one row in another format silently misplaces itself in every
    # history table, chart and month bucket that reads it.
    if balance_date is not None:
        balance_date = _validate_date(balance_date)

    with connect() as db:
        row = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise AccountNotFound("Account not found")

        # A field arriving as the UNREADABLE placeholder is not something the
        # user typed — it is what _decrypt_row shows for ciphertext this key
        # cannot open. The edit form round-trips whatever it was given, so
        # saving would replace the original with the placeholder text. Keep the
        # stored column instead: still unreadable, but not yet destroyed.
        keep = {f for f in COMMON_FIELDS if is_unreadable(columns.get(f))}

        # snapshot is what reaches history; extra is what the account keeps.
        # They differ only when backdating, which files the new figure but
        # leaves the account showing the one it already had.
        #
        # backdating also decides whether the entry is forced. A backdated
        # figure is always filed — it is a statement for a specific day, and the
        # day is the point of it. An ordinary save is not: forcing there filed a
        # fresh history row on *every* save, so editing a website or a note
        # added an entry claiming the balance had been re-read, and an account
        # edited a few times in an afternoon grew a column of identical rows.
        # Unforced, _record_balance files only when the figure actually moved.
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

        # Same reasoning for the blob: unreadable extra_data decodes to {}, and
        # writing that back would drop every category-specific field at once.
        extra_blob = (_encrypt_extra(extra) if _extra_is_readable(row['extra_data'])
                      else row['extra_data'])

        db.execute("""
            UPDATE accounts
               SET category = ?, name = ?, username = ?, password = ?, website = ?,
                   notes = ?, extra_data = ?, closed = ?, last_updated = ?
             WHERE id = ?
        """, (
            category,
            *(row[f] if f in keep else _encrypt_or_none(_clip(columns.get(f), f), f)
              for f in COMMON_FIELDS),
            extra_blob,
            int(bool(columns.get('closed'))),
            last_updated, account_id,
        ))
        _record_balance(db, account_id, snapshot, recorded_at, force=backdating)
        row = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()

    _invalidate()
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
        # An account can hold history while its own figure sits empty — one
        # whose balance was cleared, or that was filed under a category without
        # the field and later moved into one that has it. History is what says
        # what the balance is, so recover the figure from it first: otherwise
        # _record_balance reads the empty value, files nothing, and the account
        # goes on reading blank next to a table full of entries.
        _sync_current_balance(db, account_id)
        row = db.execute(
            "SELECT extra_data FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        _record_balance(db, account_id, _decode_extra(row['extra_data']), now, force=True)

    _invalidate()
    return today


def delete_account(account_id: int) -> dict:
    """Deletes an account and hands back everything needed to put it back.

    The returned token is the row and its history entries exactly as they sat
    on disk — still encrypted, every field still bound to the column it came
    out of. Nothing is decrypted to build it, which is the point: the undo
    window holds ciphertext, so a delete the user is still thinking about costs
    no more exposure than the vault file already does.

    The id travels with it. accounts.id is AUTOINCREMENT, so sqlite will never
    hand the number out again, which makes it free for the restore to reclaim —
    and reclaiming it is what keeps the balance_history rows pointing at the
    account they belong to instead of being renumbered onto a new one.

    Callers that do not want the undo may ignore the return value; the delete
    itself is complete either way, secure_delete included."""
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
    _invalidate()
    return {'account': dict(row), 'history': [dict(entry) for entry in history]}


def restore_account(token: dict) -> int:
    """Puts back an account delete_account removed. Returns its id.

    One transaction covering the row and every history entry, for the same
    reason a rotation is all or nothing: an account restored without its
    history is not the account that was deleted, and the user asked for the
    delete to be undone rather than partly undone.

    The columns are written back verbatim rather than re-encrypted. There is
    nothing to re-encrypt — the ciphertext never left the vault and the key has
    not changed — and a round trip through decrypt/encrypt would be a chance to
    lose a field that does not open, in the one operation whose whole purpose
    is that nothing is lost.

    A token from another vault would write rows no key in this one can open, so
    the caller is responsible for dropping its token when the vault locks or
    changes. The id collision check below is the backstop, not the rule."""
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
            # Forced back onto the restored row rather than trusted from the
            # token: the account it belongs to is the one just written, and a
            # stray account_id would silently graft this history onto another
            # account instead of failing.
            entry['account_id'] = account_id
            history_columns = [c for c in HISTORY_COLUMNS if c in entry]
            history_names = ', '.join(history_columns)
            history_places = ', '.join('?' * len(history_columns))
            db.execute(
                f"INSERT INTO balance_history ({history_names}) "
                f"VALUES ({history_places})",
                [entry[c] for c in history_columns],
            )
    _invalidate()
    return account_id


def set_favorite(account_id: int, favorite: bool) -> bool:
    """Pins or unpins one account. Returns the state it is now in.

    Narrow on purpose, like the bulk edits below and for the same reasons.
    Going through update_account would rewrite every column, re-encrypt each
    one, and force a balance snapshot — so pinning an account would file a
    history entry claiming its balance had been re-read.

    It also leaves last_updated alone. That stamp says when the record was last
    known good and it drives the staleness colouring; pinning an account
    verifies nothing about what is inside it, so moving the stamp would clear a
    warning the user still needs.

    The flag is dropped rather than stored as false when unpinning, so extra_data
    carries the key only while it means something — and an account that was
    never pinned and one that has been unpinned are stored identically."""
    require_session()
    favorite = bool(favorite)
    with connect() as db:
        row = db.execute("SELECT extra_data FROM accounts WHERE id = ?",
                         (account_id,)).fetchone()
        if row is None:
            raise AccountNotFound("Account not found")

        # An extra_data blob that will not open decodes to {}, and writing that
        # back would drop every category-specific field the account holds. A
        # pin is not worth that, so the record keeps its unreadable blob and
        # says the flag could not be set.
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
    _invalidate()
    return favorite


def favorite_count() -> int:
    """How many accounts are pinned. Closed ones included — the Favorites view
    lists whatever is pinned, and an account does not lose its pin by being
    settled."""
    return sum(1 for account in list_accounts() if is_favorite(account))


# -----------------------------------------------------------------------------
# Bulk edits
#
# Deliberately narrow UPDATEs rather than a loop over update_account(). That
# path rewrites every column, re-encrypts extra_data and force-records a balance
# snapshot — so closing ten accounts through it would file ten balance-history
# entries that nothing actually changed, and each one would move the account's
# figure. These two touch one column and nothing else.
#
# Neither moves last_updated. That stamp says when the record was last known
# good, and it drives the staleness colouring; closing an account or filing it
# under a different category verifies nothing about the credentials inside it,
# so claiming otherwise would quietly clear a warning the user still needs.
# -----------------------------------------------------------------------------

# The only columns _bulk_update may write. The column name is interpolated into
# the statement rather than bound — sqlite has no parameter form for an
# identifier — so it is checked against this set instead of resting on the fact
# that today's two callers happen to pass a literal.
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
    _invalidate()
    return changed


def set_closed(account_ids, closed: bool) -> int:
    """Opens or closes several accounts at once. Returns the number changed."""
    require_session()
    return _bulk_update(account_ids, 'closed', int(bool(closed)))


def set_category(account_ids, category: str) -> int:
    """Moves several accounts to another category. Returns the number changed.

    The category column is not encrypted — it is structure rather than content,
    and the same reasoning that keeps it queryable here keeps it out of
    ENCRYPTED_FIELDS."""
    require_session()
    category = _clip(category, 'category')
    if not category or not category.strip():
        raise ValueError("Category is required")
    return _bulk_update(account_ids, 'category', category)


def _encrypt_or_none(value, column: str):
    return encrypt(value, field_aad('accounts', column)) if value else None


def _encrypt_extra(extra: dict):
    return encrypt(json.dumps(extra), EXTRA_AAD) if extra else None


# -----------------------------------------------------------------------------
# Balance history
# -----------------------------------------------------------------------------

def _validate_date(recorded_at) -> str:
    """A YYYY-MM-DD date, or ValueError. The one place a date is admitted to
    balance_history, so both the entry paths and update_account's balance_date
    are held to the same spelling.

    The spelling itself is categories.parse_iso_date's to decide — this used to
    call fromisoformat directly and so admitted forms no other layer accepted,
    including the ISO week date that arrived here as an unrelated day."""
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


# Newest first. Ordering on the date alone rather than the whole stamp: an
# entry filed by hand carries a bare date and one written by a save carries a
# full timestamp, so comparing the raw values ranks same-day entries by their
# format instead of by when they were entered. Same date falls back to the id,
# which is the order they were added in.
_NEWEST_FIRST = "ORDER BY substr(recorded_at, 1, 10) DESC, id DESC"


def _newest_entry(db, account_id: int, today: str):
    """The entry that says what the balance is now, or None if there isn't one.

    Future-dated entries are skipped: a balance from the future is a typo, and
    letting one through would pin the account to it until it was deleted."""
    return db.execute(
        "SELECT id, balance, recorded_at FROM balance_history "
        f"WHERE account_id = ? AND substr(recorded_at, 1, 10) <= ? {_NEWEST_FIRST} LIMIT 1",
        (account_id, today),
    ).fetchone()


def _clear_current_balance(db, account_id: int, row):
    """Drops the account's current_balance, leaving every other field alone.

    last_updated is not touched: the stamp says when the record was last known
    good, and deleting a history entry does not make what the user typed into
    the form any older."""
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
    dated on or after every other one says what the balance is *now*, so the
    account follows it rather than needing Quick Update run separately. An
    entry that lands behind an existing one only fills in the past and leaves
    the account alone.

    Every history mutation runs this, so it is also the way back out of a
    mistyped figure: delete the entry and the account drops to the one before
    it. Callers hold the transaction, so the entry and the account it moves are
    written together or not at all."""
    row = db.execute(
        "SELECT extra_data, last_updated FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None:
        return

    today = _today()
    newest = _newest_entry(db, account_id, today)
    if newest is None:
        # Nothing dated on or before today. Two different situations, and only
        # one of them clears the account's figure.
        remaining = db.execute(
            "SELECT 1 FROM balance_history WHERE account_id = ? LIMIT 1", (account_id,)
        ).fetchone()
        if remaining is not None:
            # Future-dated entries only. _newest_entry rules those out on
            # purpose, and the account keeps the figure it already had rather
            # than being emptied by a typo dated next year.
            return
        # The history is genuinely empty — the last entry has just been deleted.
        # Leaving current_balance pointing at the figure that entry carried is
        # what made "delete the entry and the account drops to the one before
        # it" false when there was no entry before it.
        _clear_current_balance(db, account_id, row)
        return

    try:
        number = float(safe_decrypt(newest['balance'], BALANCE_AAD).strip())
        if not math.isfinite(number):
            return
    except (AttributeError, TypeError, ValueError):
        # Unreadable, or legacy plaintext that was never a number. Nothing here
        # is worth copying onto the account.
        return
    # Whole numbers stay whole, matching what the edit form and Quick Update
    # store. categories.trim_number is the single definition of that spelling —
    # services/history_io.canonical_amount uses it too, so a figure exported and
    # re-imported comes back written the same way it went out.
    balance = trim_number(number)

    # Same guard as update_account: extra_data this key cannot open must not be
    # overwritten, or every category-specific field goes with it.
    if not _extra_is_readable(row['extra_data']):
        return
    extra = _decode_extra(row['extra_data'])

    # last_updated only ever moves forward. It says when the record was last
    # known good, and filing an old statement doesn't make the account staler
    # than it already was. _newest_entry has already ruled out future dates.
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
    # safe_decrypt keeps legacy plaintext entries readable and re-raises when locked
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
    # _sync_current_balance can move the account's current_balance and
    # last_updated, so a history write is an account write too.
    _invalidate()


def add_balance_entries(account_id: int, entries) -> int:
    """Files a batch of (balance, recorded_at) entries in one transaction.

    The import path. Every row is validated before the first one is written, so
    a file with one bad line leaves the account exactly as it was rather than
    half-imported — there is no undo here short of deleting the entries by hand,
    and a partial import is the hardest state to work back out of.

    Insertion follows the order given. The caller sorts oldest first, which
    matters for rows sharing a date: _NEWEST_FIRST breaks that tie on the id, so
    the last row of the file wins, as it would if they had been typed in."""
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
        # Run once for the whole batch rather than per row: the newest entry is
        # the only one that can move the account, and it is the same answer
        # either way.
        _sync_current_balance(db, account_id)
    _invalidate()
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
    _invalidate()


def delete_balance_entry(account_id: int, entry_id: int):
    """Removes one history entry and re-points the account at what is left.

    Raises AccountNotFound for an entry that was not there, matching
    update_balance_entry. Deleting nothing used to report success, so a stale
    id from a list the user had open since before another delete looked like it
    had worked."""
    require_session()
    with connect() as db:
        cursor = db.execute(
            "DELETE FROM balance_history WHERE id = ? AND account_id = ?",
            (entry_id, account_id),
        )
        if not cursor.rowcount:
            raise AccountNotFound("Entry not found")
        _sync_current_balance(db, account_id)
    _invalidate()
