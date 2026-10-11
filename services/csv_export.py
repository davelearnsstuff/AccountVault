# -----------------------------------------------------------------------------
# services/csv_export.py
# The whole vault as a plaintext CSV.
#
# Not a backup — a backup is a copy of the vault file (services/backups.py).
# This is a readable snapshot for a spreadsheet, holding every password in the
# clear, and the UI says so before writing one. services/account_import.py
# reads this format back.
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
    rather than aborting the export; the importer refuses to store it back."""
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
    # Written as a row rather than via writeheader() so the names go through
    # defuse too: past the fixed columns they are extra_data keys, which are
    # whatever the account says.
    writer.writerow({key: defuse(key) for key in headers})
    for account in accounts:
        writer.writerow({key: defuse(account.get(key)) for key in headers})
    return buffer.getvalue()
