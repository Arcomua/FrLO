@echo off
setlocal
cd /d "%~dp0"
if exist "dist\FrLO-Publisher\FrLO-Publisher.exe" (
    start "" "dist\FrLO-Publisher\FrLO-Publisher.exe"
    exit /b 0
)
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
    if not errorlevel 1 (
        py -3 -m frlo_release.gui
        exit /b
    )
)
where python >nul 2>&1
if not errorlevel 1 (
    python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
    if not errorlevel 1 (
        python -m frlo_release.gui
        exit /b
    )
)
echo Please install Python 3.11 or newer and add it to PATH.
pause
exit /b 1
