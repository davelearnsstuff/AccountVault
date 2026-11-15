#!/bin/bash
cd "$(dirname "$0")" || exit 1

VENV_PY=".venv/bin/python"
REQUIRED_IMPORTS="import PySide6, cryptography, argon2"
LOCK="requirements/requirements.lock"

# A copy of the lock the .venv was installed from. When the lock changes (a
# version bump), the two differ and the .venv is brought up to date, so it
# never drifts from the hash-checked list.
STAMP=".venv/installed-from.lock"

# Packages earlier versions installed and this one does not: the PySide6
# meta-package and the add-ons it pulled in (Qt WebEngine, a whole Chromium,
# and Qt Pdf), none of which the app uses.
DROPPED="PySide6 PySide6_Addons PySide6_WebEngine PySide6_Pdf"

has_requirements() {
    "$1" -c "$REQUIRED_IMPORTS" >/dev/null 2>&1
}

ask_yes() {
    local answer
    read -r -p "$1 [y/N] " answer
    [[ "$answer" =~ ^[Yy] ]]
}

# Always the project's own .venv, never a system Python that happens to have
# the packages: only the .venv was installed with --require-hashes, so only it
# is known to hold the checked files.
if [ -x "$VENV_PY" ] && has_requirements "$VENV_PY"; then
    if cmp -s "$LOCK" "$STAMP"; then
        exec "$VENV_PY" main.py
    fi
    # The lock changed. Without a terminal there is nobody to ask, and the
    # .venv still works, so start it as it is rather than not at all.
    if [ ! -t 0 ]; then
        exec "$VENV_PY" main.py
    fi
    echo
    echo "AccountVault's list of packages has changed since its .venv was set up,"
    echo "most likely because a newer version is installed. Updating downloads the"
    echo "new packages and checks each one against requirements.lock."
    echo
    if ! ask_yes "Update now?"; then
        exec "$VENV_PY" main.py
    fi
    NEED_VENV=0
elif [ -x "$VENV_PY" ]; then
    # There but incomplete (an interrupted install): finish the install into it.
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

# requirements.lock holds hashes for 64-bit Linux (x86-64), macOS on Apple
# silicon, and Windows, on Python 3.11-3.14. Not Intel Macs: cryptography
# publishes no wheel for them. Anywhere else pip would
# refuse every package, so check up front and offer the unverified install
# knowingly instead of failing midway.
if [ "$NEED_VENV" = 1 ]; then BASE_PY=python3; else BASE_PY="$VENV_PY"; fi
LOCK_COVERED=0
SYSTEM="$(uname -s)"
MACHINE="$(uname -m)"
if { { [ "$SYSTEM" = "Linux" ] && [ "$MACHINE" = "x86_64" ]; } \
        || { [ "$SYSTEM" = "Darwin" ] && [ "$MACHINE" = "arm64" ]; }; } \
    && "$BASE_PY" -c 'import sys; sys.exit(not (3, 11) <= sys.version_info[:2] <= (3, 14))'; then
    LOCK_COVERED=1
fi

if [ "$NEED_VENV" = 1 ]; then
    echo
    echo "AccountVault needs a few Python packages:"
    grep -v '^#' requirements/requirements.txt | grep -v '^[[:space:]]*$'
    echo
    echo "They are installed into a .venv folder here, leaving your system Python"
    echo "untouched, and each is checked against requirements.lock. This downloads"
    echo "a few hundred MB and takes a few minutes."
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

# Removed first, and everything reinstalled: the dropped packages share the
# PySide6 folder with the ones kept, and uninstalling them takes shared files
# with them.
# shellcheck disable=SC2086  # DROPPED is a list of names
"$VENV_PY" -m pip uninstall -y $DROPPED >/dev/null 2>&1

# --require-hashes against requirements.lock, not a plain install of
# requirements.txt: this pins the actual artifacts, so a substituted
# cryptography or argon2-cffi wheel is refused rather than installed into the
# process that will hold the master password. pip is deliberately not upgraded
# first - that would fetch and run an unverified copy of the tool about to do
# the verifying.
if [ "$LOCK_COVERED" = 1 ]; then
    "$VENV_PY" -m pip install --require-hashes --force-reinstall -r "$LOCK"
    status=$?
else
    echo
    echo "This computer ($SYSTEM $MACHINE, $("$BASE_PY" -V 2>&1)) is not covered by"
    echo "requirements.lock, which holds verified fingerprints for 64-bit Linux (x86-64),"
    echo "macOS on Apple silicon, and Windows, on Python 3.11-3.14 only. The packages can"
    echo "still be installed at the pinned versions, but the downloaded files won't be"
    echo "checked against known fingerprints."
    echo
    if ! ask_yes "Install without the fingerprint check?"; then
        echo
        echo "Nothing was installed. Run ./AccountVault_Mac_Linux.sh again when you're ready."
        exit 1
    fi
    "$VENV_PY" -m pip install --force-reinstall -r requirements/requirements.txt
    status=$?
fi

if [ "$status" -ne 0 ] || ! has_requirements "$VENV_PY"; then
    echo
    echo "The packages failed to install - see the messages above."
    echo "A network connection is required. Once the problem is fixed, run"
    echo "./AccountVault_Mac_Linux.sh again to retry."
    exit 1
fi
cp "$LOCK" "$STAMP"

echo
echo "Setup complete - starting AccountVault."
exec "$VENV_PY" main.py
