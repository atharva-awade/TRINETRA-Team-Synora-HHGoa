@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% --version >nul 2>nul || (echo Python 3.10+ is required. Install from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b 1)
if not exist .venv (%PY% -m venv .venv || (echo could not create venv & pause & exit /b 1))
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt || (echo dependency install failed & pause & exit /b 1)
if not exist .env copy .env.example .env
python -m verified.cli models || (echo model download failed & pause & exit /b 1)
python -m verified.cli doctor
echo.
echo Starting the UI at http://127.0.0.1:8000  (Ctrl+C to stop)
python -m verified.cli serve
pause
