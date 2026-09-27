@echo off
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
set "VENV_PYW=.venv\Scripts\pythonw.exe"

rem Prefer the project virtual environment if one has been created.
if exist "%VENV_PYW%" (
    "%VENV_PY%" -c "import PySide6, cryptography, argon2" >nul 2>nul
    if errorlevel 1 goto install_into_venv
    start "" "%VENV_PYW%" main.py
    exit /b
)

rem No virtual environment, so fall back to a system Python. Make sure one exists
rem first: pythonw.exe fails silently when launched with "start", so without this
rem check a missing Python looks like the app simply refusing to open.
rem
rem Actually run python instead of using "where": Windows ships an app-execution
rem alias in WindowsApps that answers "where python" even when nothing is
rem installed, and only fails once you try to run it.
python.exe -c "" >nul 2>nul
if errorlevel 1 goto no_python

rem A system Python that already has the requirements is good enough - launch
rem straight from it rather than building a virtual environment nobody asked for.
python.exe -c "import PySide6, cryptography, argon2" >nul 2>nul
if errorlevel 1 goto create_venv

start "" pythonw main.py
exit /b

:create_venv
echo.
echo AccountVault needs a few Python packages that aren't installed yet:
type requirements.txt
echo.
echo They can be installed into a .venv folder here, leaving your system
echo Python untouched. This downloads a few hundred MB and takes a few minutes.
echo.
choice /c YN /n /m "Set that up now? [Y/N] "
if errorlevel 2 goto declined
echo.
echo Creating .venv ...
python.exe -m venv .venv
if errorlevel 1 goto venv_failed

:install_into_venv
echo.
echo Installing requirements ...
"%VENV_PY%" -m pip install --upgrade pip

rem --require-hashes against requirements.lock, not a plain install of
rem requirements.txt. The versions are pinned either way; this pins the actual
rem artifacts, so a substituted cryptography or argon2-cffi wheel is refused
rem rather than installed into the process that will hold the master password.
"%VENV_PY%" -m pip install --require-hashes -r requirements.lock
if errorlevel 1 goto install_failed

echo.
echo Setup complete - starting AccountVault.
start "" "%VENV_PYW%" main.py
exit /b

:declined
echo.
echo Nothing was installed. Run start.bat again when you're ready,
echo or install the packages yourself with:
echo     python -m pip install -r requirements.txt
echo.
pause
exit /b 1

:venv_failed
echo.
echo Could not create the .venv folder.
echo Check that you can write to this directory, then run start.bat again.
echo.
pause
exit /b 1

:install_failed
echo.
echo The packages failed to install - see the messages above.
echo.
echo A network connection is required. If the error mentions hashes, this
echo machine is on a platform requirements.lock does not cover - it holds the
echo Windows and Linux x86-64 wheels. You can install without that check with:
echo     python -m pip install -r requirements.txt
echo.
echo Once the problem is fixed, run start.bat again to retry.
echo.
pause
exit /b 1

:no_python
echo.
echo Python was not found on this computer.
echo AccountVault needs Python 3 installed before it can start.
echo.
echo Download it from:
echo     https://www.python.org/downloads/
echo.
echo Or paste this command into a terminal to install it:
echo     winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements
echo.
echo During a manual install, tick "Add python.exe to PATH" on the first screen.
echo After installing, close this window and run start.bat again.
echo.
pause
exit /b 1
