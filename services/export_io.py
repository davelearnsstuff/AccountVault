# -----------------------------------------------------------------------------
# services/export_io.py
# What every export and import shares: what a CSV cell may contain, which keys
# never get a column, writing a finished document to a path the user chose, and
# reading a file the user picked.
# -----------------------------------------------------------------------------

import os
import tempfile
from pathlib import Path

from core.database import restrict_to_owner


class PlaintextReadError(Exception):
    """Raised when a file chosen for import cannot be read at all. A file that
    reads but holds nothing usable is not an error: its rows are reported as
    skipped."""


# Characters a spreadsheet reads as the start of a formula rather than as text.
# The password generator avoids leading with these too.
FORMULA_LEAD = ('=', '+', '-', '@', '\t', '\r')

# The documented "this cell is text" marker. Excel and LibreOffice consume it
# and show the real value.
_TEXT_MARKER = "'"


def defuse(value) -> str:
    """Renders a value as CSV text a spreadsheet will show rather than execute.

    A value that already starts with the marker gets one more, so undefuse can
    take exactly one off and hand back what was stored — otherwise a password
    beginning "'=" would come back from a round trip without its apostrophe."""
    text = '' if value is None else str(value)
    if text[:1] in FORMULA_LEAD or text[:1] == _TEXT_MARKER:
        return _TEXT_MARKER + text
    return text


def undefuse(value) -> str:
    """Undoes defuse: removes exactly one leading apostrophe, and only from a
    cell defuse would have marked. A value that merely starts with an
    apostrophe and some other character is left alone."""
    text = '' if value is None else str(value)
    if text[:1] == _TEXT_MARKER and (text[1:2] in FORMULA_LEAD
                                     or text[1:2] == _TEXT_MARKER):
        return text[1:]
    return text


# Keys no export ever gives a column to. 'id' is the app's own row number,
# 'date_added' is never shown to the user, and '_category' and '_match' are
# view bookkeeping.
INTERNAL_KEYS = frozenset({'id', 'date_added', '_category', '_match'})


def write_export(path, text: str) -> bool:
    """Writes text to path, owner-only, leaving nothing behind on failure.

    Returns whether the file could be restricted to the owning account. False is
    not a write failure, but a caller exporting plaintext should say so: on a
    share or a removable drive it means anyone who can reach the folder can
    read the file.

    The content goes into a temporary file beside the target — created 0o600 and
    restricted before it takes its real name — and is renamed into place, so a
    full disk or a pulled drive leaves the old file or the whole new one, never
    a truncated one."""
    path = Path(path)
    # Same directory, so the rename is atomic; unique, so a leftover from a
    # crashed export cannot collide with this one.
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f'.{path.name}.', suffix='.part')

    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        # Before the rename: on Windows the ACL does the work, not the mode.
        restricted = bool(restrict_to_owner(Path(temporary)))
        os.replace(temporary, path)
        return restricted
    except BaseException:
        # BaseException, so even a KeyboardInterrupt mid-write does not leave
        # decrypted content in a file nobody knows about.
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_text(path, max_bytes: int, kind: str) -> str:
    """The text of a chosen file, whatever a spreadsheet encoded it as.

    The size cap comes first: both importers parse the text several times over
    while working out its layout, so the wrong file has to be turned away before
    that starts. `kind` names what the caller expected, for the refusal.

    utf-8-sig first because Excel writes a BOM, then the Windows default, then
    latin-1, which cannot fail — so a file is always judged on its rows rather
    than rejected on its encoding."""
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
