@echo off
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" (
    echo ERROR: Python 3.12 not found at %PY%
    echo Install with: winget install Python.Python.3.12
    pause
    exit /b 1
)

echo Using %PY%
"%PY%" -u voice_chat.py %*
