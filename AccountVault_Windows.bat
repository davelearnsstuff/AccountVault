@echo off
setlocal

rem Without this, cmd looks for python.exe in the current folder before PATH,
rem and the next line makes the current folder this one: a python.exe dropped
rem beside this script would run in place of the real one.
set "NoDefaultCurrentDirectoryInExePath=1"
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
set "VENV_PYW=.venv\Scripts\pythonw.exe"
set "LOCK=requirements\requirements.lock"

rem A copy of the lock the .venv was installed from. When the lock changes (a
rem version bump), the two differ and the .venv is brought up to date, so it
rem never drifts from the hash-checked list.
set "STAMP=.venv\installed-from.lock"

rem Packages earlier versions installed and this one does not: the PySide6
rem meta-package and the add-ons it pulled in (Qt WebEngine, a whole Chromium,
rem and Qt Pdf), none of which the app uses.
set "DROPPED=PySide6 PySide6_Addons PySide6_WebEngine PySide6_Pdf"

rem Always the project's own .venv, never a system Python that happens to have
rem the packages: only the .venv was installed with --require-hashes, so only
rem it is known to hold the checked files.
if not exist "%VENV_PYW%" goto need_venv

"%VENV_PY%" -c "import PySide6, cryptography, argon2" >nul 2>nul
if errorlevel 1 goto repair

fc /b "%LOCK%" "%STAMP%" >nul 2>nul
if errorlevel 1 goto update

start "" "%VENV_PYW%" main.py
exit /b

:update
echo.
echo AccountVault's list of packages has changed since its .venv was set up,
echo most likely because a newer version is installed. Updating downloads the
echo new packages and checks each one against requirements.lock.
echo.
choice /c YN /n /m "Update now? [Y/N] "
if errorlevel 2 goto start_as_is
goto install

:start_as_is
start "" "%VENV_PYW%" main.py
exit /b

:repair
echo.
echo The packages in .venv are incomplete, most likely from an interrupted
echo install. Reinstalling them.
goto install

:need_venv
rem Make sure a Python exists first: pythonw.exe fails silently when launched
rem with "start", so without this check a missing Python looks like the app
rem simply refusing to open.
rem
rem Actually run python instead of using "where": Windows ships an app-execution
rem alias in WindowsApps that answers "where python" even when nothing is
rem installed, and only fails once you try to run it.
call :find_python
if not defined BASE_PY goto no_python

:python_ready
echo.
echo AccountVault needs a few Python packages:
type requirements\requirements.txt
echo.
echo They are installed into a .venv folder here, leaving your system Python
echo untouched, and each is checked against requirements.lock. This downloads
echo a few hundred MB and takes a few minutes.
echo.
choice /c YN /n /m "Set that up now? [Y/N] "
if errorlevel 2 goto declined
echo.
echo Creating .venv ...
"%BASE_PY%" -m venv .venv
if errorlevel 1 goto venv_failed

:install
echo.
echo Installing requirements ...

rem Removed first, and everything reinstalled: the dropped packages share the
rem PySide6 folder with the ones kept, and uninstalling them takes shared files
rem with them.
"%VENV_PY%" -m pip uninstall -y %DROPPED% >nul 2>nul

rem --require-hashes against requirements.lock, not a plain install of
rem requirements.txt: this pins the actual artifacts, so a substituted
rem cryptography or argon2-cffi wheel is refused rather than installed into the
rem process that will hold the master password. pip is deliberately not
rem upgraded first - that would fetch and run an unverified copy of the tool
rem about to do the verifying.
"%VENV_PY%" -m pip install --require-hashes --force-reinstall -r "%LOCK%"
if errorlevel 1 goto install_failed
copy /y "%LOCK%" "%STAMP%" >nul

echo.
echo Setup complete - starting AccountVault.
start "" "%VENV_PYW%" main.py
exit /b

:declined
echo.
echo Nothing was installed. Run AccountVault_Windows.bat again when you're ready.
echo.
pause
exit /b 1

:venv_failed
echo.
echo Could not create the .venv folder.
echo Check that you can write to this directory, then run AccountVault_Windows.bat again.
echo.
pause
exit /b 1

:install_failed
echo.
echo The packages failed to install - see the messages above.
echo.
echo A network connection is required. If the error mentions hashes, this
echo machine is on a platform requirements.lock does not cover - it holds the
echo 64-bit Windows wheels for Python 3.11 to 3.15.
echo.
echo Once the problem is fixed, run AccountVault_Windows.bat again to retry.
echo.
pause
exit /b 1

:no_python
rem What python.exe is, if it runs at all, so the message can say why it is
rem not being used.
set "OTHER_PY="
for /f "delims=" %%V in ('python.exe -V 2^>nul') do set "OTHER_PY=%%V"

echo.
if defined OTHER_PY goto python_unsupported
echo Python was not found on this computer.
goto offer_python
:python_unsupported
echo AccountVault needs Python 3.11 to 3.15; this computer has %OTHER_PY%.
echo requirements.lock only holds checked packages for those versions.

:offer_python
rem winget ships with Windows 10 and 11 through App Installer, but can be
rem missing on older or stripped-down installs; then only the manual route.
winget --version >nul 2>nul
if errorlevel 1 goto manual_python
echo.
echo AccountVault can install Python 3.14 from python.org now, using winget.
echo It installs for your Windows account only and needs no administrator rights.
if defined OTHER_PY echo Your current %OTHER_PY% is left as it is.
echo.
choice /c YN /n /m "Install Python 3.14 now? [Y/N] "
if errorlevel 2 goto manual_python
echo.
echo Installing Python 3.14 ...

rem --scope user puts it at a known path under LOCALAPPDATA, which matters: the
rem installer adds Python to PATH, but this window keeps the PATH it started
rem with, so find_python has to look there rather than on PATH. The exit code
rem is not trusted either way (winget also fails when it is already
rem installed); whether a usable Python now exists is what counts.
winget install -e --id Python.Python.3.14 --scope user --silent --accept-package-agreements --accept-source-agreements
call :find_python
if defined BASE_PY goto python_ready
echo.
echo Python 3.14 did not install - see the messages above.

:manual_python
echo.
echo Download Python 3.14 from:
echo     https://www.python.org/downloads/
echo.
echo Or paste this command into a terminal to install it:
echo     winget install -e --id Python.Python.3.14 --scope user --accept-package-agreements --accept-source-agreements
echo.
echo During a manual install, tick "Add python.exe to PATH" on the first screen.
echo After installing, close this window and run AccountVault_Windows.bat again.
echo.
pause
exit /b 1

rem Sets BASE_PY to a Python that requirements.lock covers, or leaves it empty.
rem PATH first, then the folders the python.org installer uses, newest first:
rem those catch an install made without "Add to PATH" and one made a moment
rem ago by :offer_python, which this window's PATH does not include yet.
:find_python
set "BASE_PY="
call :try_python python.exe
for %%V in (315 314 313 312 311) do if not defined BASE_PY call :try_python "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
for %%V in (315 314 313 312 311) do if not defined BASE_PY call :try_python "%ProgramFiles%\Python%%V\python.exe"
exit /b

:try_python
if defined BASE_PY exit /b
"%~1" -c "import sys; sys.exit(not (3, 11) <= sys.version_info[:2] <= (3, 15))" >nul 2>nul
if not errorlevel 1 set "BASE_PY=%~1"
exit /b
