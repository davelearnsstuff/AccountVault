# -----------------------------------------------------------------------------
# tests/test_export_io.py
# The one path every export in the app takes to disk.
#
# A CSV export holds every password in the vault in the clear, so what matters
# here is not that the bytes arrive: it is that they are never readable by
# anyone else on the way, and that a failure cannot leave a half-written file of
# decrypted content behind, or destroy the file that was already there.
# -----------------------------------------------------------------------------

import os

import pytest

from services import export_io

TEXT = 'Date,Balance\r\n2024-01-01,1200\r\n'


@pytest.fixture(autouse=True)
def stub_restriction(monkeypatch):
    """export_io binds restrict_to_owner at import, so conftest's stub on the
    database module does not reach it — it would shell out to icacls. Each test
    that cares about the answer sets its own."""
    monkeypatch.setattr(export_io, 'restrict_to_owner', lambda path: True)


def leftovers(directory) -> list:
    return [p.name for p in directory.iterdir() if p.name.endswith('.part')]


# -- the happy path ------------------------------------------------------------

def test_the_document_arrives_exactly_as_given(tmp_path):
    target = tmp_path / 'export.csv'
    export_io.write_export(target, TEXT)
    assert target.read_bytes() == TEXT.encode('utf-8')


def test_line_endings_are_not_rewritten(tmp_path):
    r"""newline='' in the open, and it has to stay. The csv module already wrote
    \r\n into the string; letting Python translate on top of that gives \r\r\n
    on Windows, which some spreadsheets read as a blank row between every row."""
    target = tmp_path / 'export.csv'
    export_io.write_export(target, TEXT)
    assert b'\r\r\n' not in target.read_bytes()
    assert target.read_bytes().count(b'\r\n') == 2


def test_non_ascii_survives_the_round_trip(tmp_path):
    target = tmp_path / 'export.csv'
    export_io.write_export(target, 'Café,€1.200,00,⚠ unreadable\n')
    assert target.read_text(encoding='utf-8').startswith('Café')


def test_nothing_temporary_is_left_behind(tmp_path):
    export_io.write_export(tmp_path / 'export.csv', TEXT)
    assert leftovers(tmp_path) == []


# -- the permission answer the caller is told about ---------------------------

def test_it_reports_the_file_could_be_restricted(tmp_path, monkeypatch):
    monkeypatch.setattr(export_io, 'restrict_to_owner', lambda path: True)
    assert export_io.write_export(tmp_path / 'a.csv', TEXT) is True


def test_it_reports_when_the_file_could_not_be_restricted(tmp_path, monkeypatch):
    """False is not a write failure — the caller still has its file. It is what
    the CSV export turns into the warning naming the folder as readable."""
    monkeypatch.setattr(export_io, 'restrict_to_owner', lambda path: False)
    target = tmp_path / 'a.csv'
    assert export_io.write_export(target, TEXT) is False
    assert target.read_bytes() == TEXT.encode('utf-8')


def test_the_restriction_is_applied_before_the_file_takes_its_real_name(tmp_path):
    """On Windows the ACL does the work, not the 0o600 mkstemp gives it, so it
    has to land while the content is still under the temporary name."""
    seen = []
    export_io.restrict_to_owner = lambda path: seen.append(str(path)) or True
    try:
        export_io.write_export(tmp_path / 'export.csv', TEXT)
    finally:
        from core.database import restrict_to_owner
        export_io.restrict_to_owner = restrict_to_owner

    assert len(seen) == 1
    assert seen[0].endswith('.part')


# -- failure must not cost the user the file that was already there -----------

def test_a_failed_write_leaves_no_half_written_file(tmp_path, monkeypatch):
    monkeypatch.setattr(os, 'fsync', lambda fd: (_ for _ in ()).throw(OSError('disk full')))
    target = tmp_path / 'export.csv'

    with pytest.raises(OSError):
        export_io.write_export(target, TEXT)

    assert not target.exists()
    assert leftovers(tmp_path) == []


def test_a_failed_write_does_not_destroy_the_previous_export(tmp_path, monkeypatch):
    """open(path, 'w') truncates first, so the old file was gone before the new
    one was written. Writing beside the target and renaming is what fixed it."""
    target = tmp_path / 'export.csv'
    target.write_text('the previous export', encoding='utf-8')

    monkeypatch.setattr(os, 'fsync', lambda fd: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        export_io.write_export(target, TEXT)

    assert target.read_text(encoding='utf-8') == 'the previous export'
    assert leftovers(tmp_path) == []


def test_a_failure_restricting_the_file_still_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(export_io, 'restrict_to_owner',
                        lambda path: (_ for _ in ()).throw(OSError('no ACL here')))
    with pytest.raises(OSError):
        export_io.write_export(tmp_path / 'export.csv', TEXT)
    assert leftovers(tmp_path) == []


def test_an_interrupt_mid_write_leaves_nothing_decrypted_on_disk(tmp_path, monkeypatch):
    """BaseException, not OSError. Ctrl-C during an export must not leave a file
    of decrypted credentials that nobody knows is there."""
    def interrupt(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, 'fsync', interrupt)
    with pytest.raises(KeyboardInterrupt):
        export_io.write_export(tmp_path / 'export.csv', TEXT)

    assert leftovers(tmp_path) == []


# -- replacing an existing export ---------------------------------------------

def test_an_existing_file_is_replaced_whole(tmp_path):
    target = tmp_path / 'export.csv'
    target.write_text('a much, much longer previous export' * 50, encoding='utf-8')

    export_io.write_export(target, TEXT)

    # read_bytes, not read_text: text mode would translate the CRLFs away and
    # hide exactly the thing test_line_endings_are_not_rewritten is watching.
    assert target.read_bytes() == TEXT.encode('utf-8')
    assert leftovers(tmp_path) == []


def test_the_temporary_file_is_made_in_the_target_directory(tmp_path, monkeypatch):
    """Same filesystem, so os.replace is atomic — it only guarantees that within
    one — and a unique name, so a leftover from a crashed export cannot collide."""
    seen = {}
    real = export_io.tempfile.mkstemp

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(export_io.tempfile, 'mkstemp', spy)
    export_io.write_export(tmp_path / 'sub.csv', TEXT)

    assert seen['dir'] == tmp_path
