@echo off
setlocal

REM Check setup has been run
if not exist venv (
    echo Virtual environment not found.
    echo Please run "setup.bat" first.
    echo.
    pause
    exit /b 1
)

echo ============================================================
echo  No Man's Sky bHaptics Mod
echo ============================================================
echo.
echo Make sure bHaptics Player is running before continuing.
echo The mod will launch No Man's Sky automatically.
echo.
echo Press any key to start...
pause >nul

venv\Scripts\pymhf run NoMansSky_bhaptics_nmspy.py

REM If pymhf exits with an error, keep the window open
if errorlevel 1 (
    echo.
    echo The mod exited with an error. Check the log file for details.
    echo Log files are named "pymhf-*.log" in this folder.
    echo.
    pause
)
