@echo off
setlocal
cd /d "%~dp0" || exit /b 1
if not exist ".venv\Scripts\zotero-pdf-pipeline.exe" (
    echo Run .\setup-windows.cmd first.
    exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\zotero-pdf-pipeline.exe" %*
exit /b %errorlevel%
