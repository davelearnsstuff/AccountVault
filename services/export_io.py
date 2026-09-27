# -----------------------------------------------------------------------------
# services/export_io.py
# The mechanics every export shares: what a cell may contain, which keys never
# get a column, and writing the finished document to a path the user chose.
#
# Every export in the app ends here, and it exists because the three that had
# their own `open(path, 'w')` each got a different part of this wrong.
#
# What a plaintext export is depends on the file: a .vault is ciphertext and a
# CSV is not, but both are written the same way, because the difference that
# matters is not the format — it is that the file leaves the vault's directory
# and stops being covered by anything the vault does.
#
# The other direction lands here too. read_text is what opens a file the user
# picked from outside the vault — a bank's statement, an export from another
# password manager — and the two importers that call it share a problem rather
# than a format: a file that is the wrong size or the wrong encoding has to be
# refused or coerced before anything can be read out of it at all.
# -----------------------------------------------------------------------------

import os
import tempfile
from pathlib import Path

from database import restrict_to_owner


class PlaintextReadError(Exception):
    """Raised when a file chosen for import cannot be read at all.

    Distinct from a file that reads fine and holds nothing usable: that is a
    result with every row in `skipped`, which the importers show, and not an
    error."""

# Characters a spreadsheet reads as the start of a formula rather than as text.
_FORMULA_LEAD = ('=', '+', '-', '@', '\t', '\r')


def defuse(value) -> str:
    """Renders a value as CSV text a spreadsheet will show rather than execute.

    Lives here rather than in services/csv_export.py, where it used to, because it
    is not that module's rule: every CSV the app writes goes through it —
    backup's whole-vault export, history_io's history file and
    account_export's per-account file — and a second copy of the lead-character
    list is a second thing to forget to update.

    The leading apostrophe is the documented "this cell is text" marker; Excel
    and LibreOffice consume it and display the real value. A tool reading the
    file programmatically sees it as a literal character, which is the trade:
    a visible apostrophe on a handful of cells, against passwords silently
    evaluated into numbers and notes that can execute on open."""
    text = '' if value is None else str(value)
    return "'" + text if text[:1] in _FORMULA_LEAD else text


# Keys no export ever gives a column to, whatever it is exporting.
#
# 'id' is this app's own row number and means nothing in a spreadsheet.
# 'date_added' is recorded but never shown to the user anywhere in the app, and
# an export is not the place to introduce a field the UI has decided not to
# have. '_category' and '_match' are view bookkeeping the UI flattens on.
#
# One frozenset rather than the two overlapping ones csv_export.py and
# account_export.py each kept: they legitimately differ — a per-account file
# writes its category and closed state by name elsewhere, so those are excluded
# there and not here — but the four keys above are a floor both share, and a
# new piece of bookkeeping added to it was previously remembered in one file
# and leaked from the other.
INTERNAL_KEYS = frozenset({'id', 'date_added', '_category', '_match'})


def write_export(path, text: str) -> bool:
    """Writes text to path, owner-only, leaving nothing behind on failure.

    Returns whether the file could be restricted to the owning account. False
    is not a failure to write — the encryption a .vault carries is the real
    protection and a CSV never had any — but a caller exporting plaintext is
    worth telling, because on a share or a removable drive the answer means the
    file is readable by everyone who can reach the directory.

    Three things this does that a bare open() did not:

      * creates the file owner-only *before* anything is written to it, rather
        than at whatever the target directory grants. A CSV export holds every
        password in the vault in the clear;
      * writes to a temporary file beside the target and renames it into place,
        so a full disk or a pulled drive leaves either the whole document or
        the previous file, never a truncated one. open(path, 'w') truncates
        first, which destroyed the old file before the new one was written;
      * removes the temporary file if the write fails, rather than leaving
        decrypted content in a half-written file nobody knows about.
    """
    path = Path(path)
    # mkstemp, in the target's own directory: same filesystem, so the rename
    # below is atomic (os.replace only guarantees that within one), and a unique
    # name, so a leftover from an earlier crashed export cannot collide with
    # this one and turn the first attempt into a confusing failure.
    #
    # mkstemp also creates the file 0o600 and O_EXCL by construction, which is
    # the point: the content is written into a file that was never readable by
    # anyone else, rather than created at whatever the directory grants and
    # tightened afterwards.
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f'.{path.name}.', suffix='.part')

    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        # Applied while the file still has its temporary name but already holds
        # the content: on Windows the ACL does the work and the 0o600 above does
        # not, so the restriction has to land before the file takes its real one.
        restricted = bool(restrict_to_owner(Path(temporary)))
        os.replace(temporary, path)
        return restricted
    except BaseException:
        # Nothing half-written is left behind, whether the failure was the disk
        # filling up or the rename itself. BaseException, not OSError: a
        # KeyboardInterrupt mid-write must not leave decrypted content in a
        # file nobody knows about either.
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_text(path, max_bytes: int, kind: str) -> str:
    """The text of a chosen file, whatever a spreadsheet encoded it as.

    utf-8-sig first because Excel writes a BOM, then the Windows default, then
    latin-1 — which cannot fail, since every byte sequence maps to some
    character, so a file always opens far enough for its rows to be judged on
    their contents rather than rejected on their encoding. latin-1 is therefore
    outside the loop: leaving it in meant the code after the loop could never
    run while reading as though it were the handler for a file that would not
    decode.

    `kind` names what the caller was expecting, so an oversized file is refused
    in the words of the import that refused it.

    The size cap comes first and is the reason this is not just open().read():
    both importers parse the text several times over while they work out its
    layout, so the wrong file picked by accident has to be turned away before
    any of that starts.
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise PlaintextReadError(str(exc)) from exc
    if size > max_bytes:
        raise PlaintextReadError(
            f'That file is {size / 1024 / 1024:.0f} MB — too large to be {kind}.')

    for encoding in ('utf-8-sig', 'cp1252'):
        try:
            with open(path, 'r', encoding=encoding, newline='') as handle:
                return handle.read()
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            raise PlaintextReadError(str(exc)) from exc

    try:
        with open(path, 'r', encoding='latin-1', newline='') as handle:
            return handle.read()
    except OSError as exc:
        raise PlaintextReadError(str(exc)) from exc
