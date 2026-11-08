@echo off
rem Starts Duplicate Finder.
rem 1. Checks that Python is installed.
rem 2. First start only: installs the packages for "Find similar files" (requirements.txt).
rem 3. Asks Windows for administrator rights, so the fast scan mode (reading the drive's
rem    file table directly, read-only) can be used. Click "Yes". If you click "No", the app
rem    still starts, in the slower standard mode.
setlocal
cd /d "%~dp0"

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if errorlevel 1 goto nopython

python -c "import cv2, numpy, pymupdf, docx" >nul 2>&1
if not errorlevel 1 goto elevate
echo First start: installing the packages needed for "Find similar files".
echo This happens only once and takes a few minutes...
python -m pip install --user --disable-pip-version-check -r requirements.txt
if errorlevel 1 echo Package installation failed. Finding duplicates still works; "Find similar files" will be unavailable.

:elevate
net session >nul 2>&1
if %errorlevel% equ 0 goto run
powershell -NoProfile -Command "try { Start-Process -FilePath '%~f0' -Verb RunAs -ErrorAction Stop; exit 0 } catch { exit 1 }"
if %errorlevel% equ 0 exit /b

:run
cd /d "%~dp0"
python app.py
pause
exit /b

:nopython
echo Python 3.9 or newer is required but was not found.
echo Install it from https://www.python.org/downloads/
echo During installation tick "Add python.exe to PATH", then start this file again.
pause
