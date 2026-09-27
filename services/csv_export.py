# -----------------------------------------------------------------------------
# services/csv_export.py
# The whole vault as a plaintext CSV.
#
# This was services/backup.py, one character from services/backups.py, which
# copies the vault file. The two do opposite things — one writes every password
# in the clear, the other writes ciphertext — and `from services import backup`
# next to `from services import backups` is a typo that imports the wrong one
# and raises nothing. The name is the fix.
#
# It also used to own a second thing: a .vault file, a JSON envelope holding
# the whole vault encrypted under a backup password of its own. That is gone. A
# backup is now a copy of the vault file itself, so there is no separate format
# to write, no second password to lose, and no import that could reject or
# reshape what it reads.
#
# What is left is the export that was never a backup in the first place. A CSV
# is a readable snapshot for a spreadsheet: it leaves the vault's directory,
# stops being covered by anything the vault does, and holds every password in
# the clear. The UI says so twice before writing one.
#
# services/account_import.py reads this format back.
# -----------------------------------------------------------------------------

import csv
import datetime
import io

from services.accounts import COMMON_FIELDS
from services.export_io import INTERNAL_KEYS, defuse


def default_csv_name() -> str:
    return f"accountvault-export-{datetime.date.today()}.csv"


def export_csv(accounts: list[dict]) -> str:
    """Renders decrypted accounts as CSV text. Balance history is not included.

    A field that would not decrypt appears as its "⚠ unreadable" placeholder
    rather than aborting the export. This file is a readable snapshot for a
    spreadsheet, not the thing the vault is restored from, so one broken field
    is worth showing as broken instead of withholding every other account."""
    # The four encrypted text columns come from COMMON_FIELDS because this is a
    # column order, not a second opinion about which columns exist.
    preferred = ['category', *COMMON_FIELDS, 'closed', 'last_updated']
    headers = list(preferred)
    seen = set(headers) | INTERNAL_KEYS
    for account in accounts:
        for key in account:
            if key not in seen:
                seen.add(key)
                headers.append(key)

    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=headers, extrasaction='ignore', lineterminator='\r\n'
    )
    # The header row is written as a row rather than via writeheader() so the
    # names go through defuse too: past the fixed columns above they are
    # extra_data keys, and a key is whatever the account that carries it says.
    writer.writerow({key: defuse(key) for key in headers})
    for account in accounts:
        writer.writerow({key: defuse(account.get(key)) for key in headers})
    return buffer.getvalue()
