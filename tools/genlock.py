"""Generate hash-pinned lock files from requirements*.txt.

Covers win_amd64 and manylinux x86_64 across the Python versions
AccountVault_Windows.bat might find, so --require-hashes does not turn into
"works only on the machine that generated it".
"""
import hashlib
import pathlib
import re
import subprocess
import sys
import tempfile

# One entry per OS, each a set of tags pip is offered together — packages
# disagree about which manylinux baseline they build against (PySide6 ships
# manylinux_2_34, cffi manylinux2014), and pip picks whichever of the tags a
# given package actually has.
PLATFORM_SETS = [
    ['win_amd64'],
    ['manylinux_2_34_x86_64', 'manylinux_2_28_x86_64',
     'manylinux2014_x86_64', 'manylinux_2_17_x86_64'],
]
# What AccountVault_Windows.bat might find. Most wheels here are abi3 or pure
# Python and cover all four with one artifact; cffi builds per version, hence
# the sweep.
PYTHONS = ['3.11', '3.12', '3.13', '3.14']
PY = sys.executable
REQUIREMENTS = pathlib.Path(__file__).resolve().parent.parent / 'requirements'


def pins(path):
    """(name, version) for each pinned line, in file order."""
    out = []
    for line in pathlib.Path(path).read_text(encoding='utf-8').splitlines():
        line = line.split('#')[0].strip()
        match = re.fullmatch(r'([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.\-]+)', line)
        if match:
            out.append((match.group(1), match.group(2)))
    return out


def collect(reqs, dest):
    """Every wheel pip would pick for these pins, over every platform/version."""
    for tags in PLATFORM_SETS:
        for version in PYTHONS:
            flags = sum((['--platform', tag] for tag in tags), [])
            subprocess.run(
                [PY, '-m', 'pip', 'download', *sum((['-r', str(r)] for r in reqs), []),
                 '-d', str(dest), '--only-binary=:all:', '--no-deps',
                 *flags, '--python-version', version, '-q'],
                check=False, capture_output=True, text=True,
            )


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def normalise(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def build(reqs, out_path, header):
    dest = pathlib.Path(tempfile.mkdtemp())
    collect(reqs, dest)

    by_package = {}
    for wheel in sorted(dest.glob('*.whl')):
        package = normalise(wheel.name.split('-')[0])
        by_package.setdefault(package, []).append(wheel)

    lines = [header.rstrip(), '']
    missing = []
    for req in reqs:
        for name, version in pins(req):
            wheels = by_package.get(normalise(name), [])
            if not wheels:
                missing.append(name)
                continue
            lines.append(f'{name}=={version} \\')
            hashes = sorted({sha256(w) for w in wheels})
            for index, digest in enumerate(hashes):
                tail = '' if index == len(hashes) - 1 else ' \\'
                lines.append(f'    --hash=sha256:{digest}{tail}')
            lines.append('')

    pathlib.Path(out_path).write_text('\n'.join(lines), encoding='utf-8')
    print(f'{pathlib.Path(out_path).name}: {len(lines)} lines, '
          f'{sum(1 for line in lines if line.startswith("    --hash"))} hashes'
          + (f'  MISSING: {missing}' if missing else ''))


RUNTIME_HEADER = '''# Hash-pinned install list — generated, do not edit by hand.
#
# requirements.txt pins versions; this pins the actual artifacts. Without it
# `pip install -r requirements.txt` accepts whatever the index serves for those
# version numbers, which for an app whose entire job is holding secrets is the
# one dependency check worth having: a substituted cryptography or
# argon2-cffi wheel runs inside the process that holds the master password.
#
# Every hash under one entry is an alternative — the wheels for win_amd64 and
# manylinux x86_64 across Python 3.11 to 3.14, so this is not a lock that only
# works on the machine that produced it. A platform outside that set has no
# matching hash and will be refused rather than silently unverified; install
# from requirements.txt there, knowingly.
#
# Regenerate after any version bump (Dependabot edits requirements.txt, not
# this file), from a machine with network access:
#
#     python tools/genlock.py
#
# Install with:
#
#     python -m pip install --require-hashes -r requirements/requirements.lock
'''

DEV_HEADER = '''# Hash-pinned dev install list — generated, do not edit by hand.
#
# The companion to requirements.lock, split for the same reason
# requirements-dev.txt is split from requirements.txt. --require-hashes applies
# to a whole pip invocation, so a hashed file cannot be mixed with an unhashed
# one: the test job needs its own.
#
# See requirements.lock for what the hashes are for and how to regenerate.
'''

if __name__ == '__main__':
    build([REQUIREMENTS / 'requirements.txt'],
          REQUIREMENTS / 'requirements.lock', RUNTIME_HEADER)
    build([REQUIREMENTS / 'requirements-dev.txt'],
          REQUIREMENTS / 'requirements-dev.lock', DEV_HEADER)
