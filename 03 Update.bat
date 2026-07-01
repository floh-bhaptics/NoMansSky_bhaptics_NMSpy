@echo off
setlocal

echo ============================================================
echo  No Man's Sky bHaptics Mod - Update
echo ============================================================
echo.

REM Always run from the folder where this bat file lives,
REM regardless of how it was launched.
cd /d "%~dp0"
echo Working directory: %CD%
echo.

if not exist venv (
    echo ERROR: Virtual environment not found.
    echo Please run "01 Setup.bat" first.
    echo.
    pause
    exit /b 1
)

echo Step 1: Upgrading pip...
venv\Scripts\python -m pip install --upgrade pip
if errorlevel 1 (
    echo ERROR: pip upgrade failed.
    pause
    exit /b 1
)

echo.
echo Step 2: Upgrading nmspy and bhaptics_python...
venv\Scripts\pip install --upgrade nmspy bhaptics_python
if errorlevel 1 (
    echo ERROR: Package upgrade failed.
    pause
    exit /b 1
)

echo.
echo Step 3: Downloading latest mod files...
curl -L -o NoMansSky_bhaptics_NMSpy_update.zip "https://github.com/floh-bhaptics/NoMansSky_bhaptics_NMSpy/releases/latest/download/NoMansSky_bhaptics_NMSpy.zip"
if errorlevel 1 (
    echo ERROR: Download failed. Check your internet connection and try again.
    del /q NoMansSky_bhaptics_NMSpy_update.zip 2>nul
    pause
    exit /b 1
)
echo Download saved to: %CD%\NoMansSky_bhaptics_NMSpy_update.zip
if not exist NoMansSky_bhaptics_NMSpy_update.zip (
    echo ERROR: ZIP file not found after download — curl may have silently failed.
    pause
    exit /b 1
)

echo.
echo Step 4: Extracting mod files...
if exist _update_tmp rmdir /s /q _update_tmp
mkdir _update_tmp
echo Extracting into: %CD%\_update_tmp
tar -xf NoMansSky_bhaptics_NMSpy_update.zip -C _update_tmp
if errorlevel 1 (
    echo ERROR: Extraction failed.
    rmdir /s /q _update_tmp 2>nul
    del /q NoMansSky_bhaptics_NMSpy_update.zip 2>nul
    pause
    exit /b 1
)

echo Contents of _update_tmp after extraction:
dir /b _update_tmp

echo Copying mod files...
copy /y "_update_tmp\NoMansSky_bhaptics_nmspy.py" "NoMansSky_bhaptics_nmspy.py"
copy /y "_update_tmp\bhaptics_library.py" "bhaptics_library.py"

echo.
echo Step 5: Cleaning up...
rmdir /s /q _update_tmp
del /q NoMansSky_bhaptics_NMSpy_update.zip

echo.
echo ============================================================
echo  Update complete!
echo ============================================================
echo.
pause