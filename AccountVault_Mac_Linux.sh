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

# Sets BASE_PY to the Python to build the .venv from: one requirements.lock
# covers (3.11-3.15) if there is one, else any 3.11 or later, which gets the
# unverified install offered further down. Versioned names too: a Python
# installed beside the system one (Homebrew, deadsnakes) is often only
# reachable as python3.14.
find_python() {
    local candidate
    BASE_PY=""
    for candidate in python3 python3.15 python3.14 python3.13 python3.12 python3.11; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c \
                'import sys; sys.exit(not (3, 11) <= sys.version_info[:2] <= (3, 15))' 2>/dev/null; then
            BASE_PY="$candidate"
            return 0
        fi
    done
    if command -v python3 >/dev/null 2>&1 \
            && python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        BASE_PY=python3
        return 0
    fi
    return 1
}

# Offers to install Python with the package manager this machine has, then
# looks again. Succeeds only if a usable Python exists afterwards: on an older
# distribution the packaged python3 can still be older than 3.11.
offer_python_install() {
    local manager="" command_text=""
    if [ "$(uname -s)" = "Darwin" ]; then
        if command -v brew >/dev/null 2>&1; then
            manager=brew
            command_text="brew install python@3.14"
        fi
    elif command -v apt-get >/dev/null 2>&1; then
        manager=apt
        command_text="sudo apt-get update && sudo apt-get install -y python3 python3-venv"
    elif command -v dnf >/dev/null 2>&1; then
        manager=dnf
        command_text="sudo dnf install -y python3"
    elif command -v pacman >/dev/null 2>&1; then
        manager=pacman
        command_text="sudo pacman -S --needed --noconfirm python"
    fi
    [ -n "$manager" ] || return 1

    echo
    echo "AccountVault can install Python now by running:"
    echo "    $command_text"
    if [ "$manager" != brew ]; then
        echo "This uses sudo, so it will ask for your password."
    fi
    echo
    ask_yes "Install Python now?" || return 1
    echo
    case "$manager" in
        brew)   brew install python@3.14 ;;
        apt)    sudo apt-get update && sudo apt-get install -y python3 python3-venv ;;
        dnf)    sudo dnf install -y python3 ;;
        pacman) sudo pacman -S --needed --noconfirm python ;;
    esac || { echo; echo "Python did not install - see the messages above."; return 1; }
    hash -r
    find_python
}

python_help() {
    echo
    if command -v python3 >/dev/null 2>&1; then
        echo "AccountVault needs Python 3.11 or later; this computer has $(python3 -V 2>&1)."
    else
        echo "Python 3 was not found on this computer."
        echo "AccountVault needs Python 3.11 or later installed before it can start."
    fi
    echo
    echo "Install it with your package manager, for example:"
    echo "    Fedora:          sudo dnf install python3"
    echo "    Debian/Ubuntu:   sudo apt install python3 python3-venv"
    echo "    macOS:           brew install python@3.14"
    echo "                     or https://www.python.org/downloads/"
    echo
    echo "Then run ./AccountVault_Mac_Linux.sh again."
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
    # Asking needs a terminal; launched from a file manager there may be none.
    if ! find_python; then
        if [ ! -t 0 ] || ! offer_python_install; then
            python_help
            exit 1
        fi
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
# silicon, and Windows, on Python 3.11-3.15. Not Intel Macs: cryptography
# publishes no wheel for them. Anywhere else pip would
# refuse every package, so check up front and offer the unverified install
# knowingly instead of failing midway.
# BASE_PY was set by find_python when the .venv is still to be created.
if [ "$NEED_VENV" = 0 ]; then BASE_PY="$VENV_PY"; fi
LOCK_COVERED=0
SYSTEM="$(uname -s)"
MACHINE="$(uname -m)"
if { { [ "$SYSTEM" = "Linux" ] && [ "$MACHINE" = "x86_64" ]; } \
        || { [ "$SYSTEM" = "Darwin" ] && [ "$MACHINE" = "arm64" ]; }; } \
    && "$BASE_PY" -c 'import sys; sys.exit(not (3, 11) <= sys.version_info[:2] <= (3, 15))'; then
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
    if ! "$BASE_PY" -m venv .venv; then
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
    echo "macOS on Apple silicon, and Windows, on Python 3.11-3.15 only. The packages can"
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
