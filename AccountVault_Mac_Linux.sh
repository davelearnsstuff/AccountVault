#!/bin/bash
cd "$(dirname "$0")" || exit 1

VENV_PY=".venv/bin/python"
REQUIRED_IMPORTS="import PySide6, cryptography, argon2"

has_requirements() {
    "$1" -c "$REQUIRED_IMPORTS" >/dev/null 2>&1
}

# Prefer the project virtual environment if one has been created. If it is
# there but incomplete (an interrupted install), finish the install into it.
if [ -x "$VENV_PY" ]; then
    if has_requirements "$VENV_PY"; then
        exec "$VENV_PY" main.py
    fi
    NEED_VENV=0
else
    if ! command -v python3 >/dev/null 2>&1; then
        echo
        echo "Python 3 was not found on this computer."
        echo "AccountVault needs Python 3.11 or later installed before it can start."
        echo
        echo "Install it with your package manager, for example:"
        echo "    Fedora:          sudo dnf install python3"
        echo "    Debian/Ubuntu:   sudo apt install python3 python3-venv"
        echo "    macOS:           https://www.python.org/downloads/"
        echo
        echo "Then run ./AccountVault_Mac_Linux.sh again."
        exit 1
    fi

    if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
        echo
        echo "AccountVault needs Python 3.11 or later; this computer has $(python3 -V 2>&1)."
        echo "Install a newer Python 3, then run ./AccountVault_Mac_Linux.sh again."
        exit 1
    fi

    # A system Python that already has the requirements is good enough - launch
    # straight from it rather than building a virtual environment nobody asked for.
    if has_requirements python3; then
        exec python3 main.py
    fi
    NEED_VENV=1
fi

# From here on we need to ask questions, which needs a terminal. Launched from
# a file manager there may be none, and read would see end-of-file - say what
# to do instead of exiting silently.
if [ ! -t 0 ]; then
    echo "AccountVault needs to install some Python packages first."
    echo "Open a terminal in this folder and run ./AccountVault_Mac_Linux.sh"
    exit 1
fi

ask_yes() {
    local answer
    read -r -p "$1 [y/N] " answer
    [[ "$answer" =~ ^[Yy] ]]
}

# requirements.lock holds hashes for the x86-64 Linux and Windows wheels on
# Python 3.11-3.14. Anywhere else pip would refuse every package, so check up
# front and offer the unverified install knowingly instead of failing midway.
if [ "$NEED_VENV" = 1 ]; then BASE_PY=python3; else BASE_PY="$VENV_PY"; fi
LOCK_COVERED=0
if [ "$(uname -s)" = "Linux" ] && [ "$(uname -m)" = "x86_64" ] \
    && "$BASE_PY" -c 'import sys; sys.exit(not (3, 11) <= sys.version_info[:2] <= (3, 14))'; then
    LOCK_COVERED=1
fi

if [ "$NEED_VENV" = 1 ]; then
    echo
    echo "AccountVault needs a few Python packages that aren't installed yet:"
    grep -v '^#' requirements/requirements.txt | grep -v '^[[:space:]]*$'
    echo
    echo "They can be installed into a .venv folder here, leaving your system"
    echo "Python untouched. This downloads a few hundred MB and takes a few minutes."
    echo
    if ! ask_yes "Set that up now?"; then
        echo
        echo "Nothing was installed. Run ./AccountVault_Mac_Linux.sh again when you're ready."
        exit 1
    fi

    echo
    echo "Creating .venv ..."
    if ! python3 -m venv .venv; then
        rm -rf .venv
        echo
        echo "Could not create the .venv folder."
        echo "On Debian and Ubuntu this needs the venv module:"
        echo "    sudo apt install python3-venv"
        echo "Otherwise, check that you can write to this directory."
        echo "Then run ./AccountVault_Mac_Linux.sh again."
        exit 1
    fi
fi

echo
echo "Installing requirements ..."

# --require-hashes against requirements.lock, not a plain install of
# requirements.txt: this pins the actual artifacts, so a substituted
# cryptography or argon2-cffi wheel is refused rather than installed into the
# process that will hold the master password. pip is deliberately not upgraded
# first - that would fetch and run an unverified copy of the tool about to do
# the verifying.
if [ "$LOCK_COVERED" = 1 ]; then
    "$VENV_PY" -m pip install --require-hashes -r requirements/requirements.lock
    status=$?
else
    echo
    echo "This computer ($(uname -s) $(uname -m), $("$BASE_PY" -V 2>&1)) is not covered by"
    echo "requirements.lock, which holds verified fingerprints for x86-64 Linux and"
    echo "Windows on Python 3.11-3.14 only. The packages can still be installed at the"
    echo "pinned versions, but the downloaded files won't be checked against known"
    echo "fingerprints."
    echo
    if ! ask_yes "Install without the fingerprint check?"; then
        echo
        echo "Nothing was installed. Run ./AccountVault_Mac_Linux.sh again when you're ready."
        exit 1
    fi
    "$VENV_PY" -m pip install -r requirements/requirements.txt
    status=$?
fi

if [ "$status" -ne 0 ] || ! has_requirements "$VENV_PY"; then
    echo
    echo "The packages failed to install - see the messages above."
    echo "A network connection is required. Once the problem is fixed, run"
    echo "./AccountVault_Mac_Linux.sh again to retry."
    exit 1
fi

echo
echo "Setup complete - starting AccountVault."
exec "$VENV_PY" main.py
