@echo off
rem Double-click to start Route Mapper on Windows. Keep this file in the same
rem folder as route_mapper.py. Close this window (or press Ctrl+C) to stop it.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py route_mapper.py
) else (
  python route_mapper.py
)
echo.
echo Route Mapper has stopped. If you see an error above, check that Python 3 is
echo installed (python.org) with "Add python.exe to PATH" ticked.
pause
