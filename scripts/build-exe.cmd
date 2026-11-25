@echo off
rem Builds DanteTest.exe: one file, with the window, nothing to install on the PCs that run it.
rem Run this once on a Windows 10/11 PC with internet access, from a folder that also holds dantetest.py
rem (and DanteTest.ico, if the exe should have the icon).
rem
rem What it changes on this PC:
rem   - installs Python 3.12 from python.org (through winget) if it is not already installed
rem   - puts PyInstaller and the audio libraries in %LOCALAPPDATA%\DanteTest-build (delete that folder afterwards if you like)
rem It leaves DanteTest.exe next to this script.
setlocal
pushd "%~dp0"
if not exist dantetest.py (echo dantetest.py must be in the same folder as this script. & goto fail)

set "PY="
for %%P in ("%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%ProgramFiles%\Python312\python.exe") do if exist %%P set "PY=%%~P"
if not defined PY (
    echo Installing Python 3.12 ...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    for %%P in ("%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%ProgramFiles%\Python312\python.exe") do if exist %%P set "PY=%%~P"
)
if not defined PY (echo Python 3.12 was not found. Install it from https://www.python.org/downloads/ and run this again. & goto fail)

set "B=%LOCALAPPDATA%\DanteTest-build"
echo Using %PY%
echo Installing the build tools into %B% ...
"%PY%" -m venv "%B%\venv" || goto fail
"%B%\venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check pyinstaller netaudio sounddevice numpy || goto fail

echo Building DanteTest.exe (a few minutes) ...
if exist DanteTest.exe del DanteTest.exe
if exist DanteTest.exe (echo DanteTest.exe is open. Close every DanteTest window and run this again. & goto fail)
set "ICON="
if exist "%~dp0DanteTest.ico" set ICON=--icon "%~dp0DanteTest.ico"
"%B%\venv\Scripts\pyinstaller.exe" --noconfirm --clean --log-level WARN --onefile --windowed --name DanteTest %ICON% ^
    --collect-all netaudio --collect-all sounddevice --collect-all _sounddevice_data ^
    --distpath "%~dp0." --workpath "%B%\work" --specpath "%B%" dantetest.py || goto fail
if not exist DanteTest.exe goto fail

rem Self-check: the exe must be able to run its built-in Dante controller.
del "%TEMP%\dantetest-check.txt" 2>nul
start "" /wait DanteTest.exe _netaudio "%TEMP%\dantetest-check.txt" --version
findstr /c:"netaudio" "%TEMP%\dantetest-check.txt" >nul || (echo The exe was built but its self-check failed. & goto fail)

echo.
echo Done: %~dp0DanteTest.exe
echo Double-click it to open the test window.
popd
pause
exit /b 0

:fail
echo.
echo BUILD FAILED. Please send a screenshot of this window.
popd
pause
exit /b 1
