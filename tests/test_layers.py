# -----------------------------------------------------------------------------
# tests/test_layers.py
# core/ is the bottom layer: it imports no Qt, and nothing above it.
#
# Nothing else holds that line. A core module that picks up a PySide6 import
# still passes every other test, because the suite has Qt installed — and from
# then on tools/make_demo_vault.py, which builds a vault with no window, needs a
# display to run. One that reaches up into services/ or ui/ works too, right up
# until the two import each other and one of them loads half-built.
#
# So this reads the source rather than importing it: an import inside a
# function counts, though importing the module would never run it.
# -----------------------------------------------------------------------------

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parent.parent / 'core'
MODULES = sorted(CORE.glob('*.py'))

# Qt, and every package above core/. Matched on the first part of the dotted
# name, so PySide6.QtCore and services.accounts are both caught.
FORBIDDEN = {'PySide6', 'shiboken6', 'services', 'ui', 'main'}


def imported_names(source):
    """(line, module) for every absolute import in the source, at any depth."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield node.lineno, node.module


def upward_imports(source):
    return [f'line {line}: {name}' for line, name in imported_names(source)
            if name.split('.')[0] in FORBIDDEN]


def test_the_scan_finds_the_core_modules():
    # An empty parameter set is a skip, not a failure: if core/ moved, the test
    # below would stop checking anything and nobody would hear about it.
    assert {p.name for p in MODULES} >= {'auth.py', 'database.py', 'encryption.py'}


def test_the_scan_sees_an_import_inside_a_function():
    source = (
        'from core import database\n'
        'def show():\n'
        '    from PySide6.QtWidgets import QMessageBox\n'
        '    import ui.dialogs\n'
    )
    assert upward_imports(source) == ['line 3: PySide6.QtWidgets', 'line 4: ui.dialogs']


@pytest.mark.parametrize('path', MODULES, ids=lambda p: p.name)
def test_core_imports_nothing_above_it(path):
    found = upward_imports(path.read_text(encoding='utf-8'))
    assert not found, f'core/{path.name} imports upward: {found}'
