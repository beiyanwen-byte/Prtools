@echo off
setlocal DisableDelayedExpansion
set "SCRIPT_DIR=%~dp0"

where pyw >nul 2>nul
if not errorlevel 1 (
  start "" pyw -3 "%SCRIPT_DIR%app\desktop_gui.py"
  exit /b
)

where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw "%SCRIPT_DIR%app\desktop_gui.py"
  exit /b
)

echo Python GUI runtime not found. Please install Python with Tcl/Tk support.
pause
