@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        python -m venv .venv
    )
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import qrcode" >nul 2>nul
if errorlevel 1 (
    echo Installing terminal QR dependency...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --no-input -r requirements.txt
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" taptap_save.py qr-export
if errorlevel 1 goto failed
echo Export finished. You can close this window.
pause >nul
exit /b 0
:failed
echo Export failed. Please review the message above. Python 3.9+ is required.
pause
exit /b 1
