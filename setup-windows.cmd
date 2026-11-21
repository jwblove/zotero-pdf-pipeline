@echo off
setlocal
set PYTHONUTF8=1
cd /d "%~dp0" || exit /b 1
if exist ".venv\Scripts\python.exe" goto install
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 (
    py -3 -m venv .venv || exit /b 1
    goto install
)
python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if errorlevel 1 (
    echo Install Python 3.11 or newer from python.org, reopen PowerShell and retry.
    exit /b 1
)
python -m venv .venv || exit /b 1
:install
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check . || exit /b 1
if not exist config.toml copy /-y config.example.toml config.toml >nul || exit /b 1
".venv\Scripts\zotero-pdf-pipeline.exe" --help >nul || exit /b 1
echo Installed. Run .\pipeline.cmd doctor
