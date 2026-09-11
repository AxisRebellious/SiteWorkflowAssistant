@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PLAYWRIGHT_BROWSERS_PATH=%~dp0runtime\browsers
echo در حال اجرای دستیار وب...
echo در حال انتخاب پورت آزاد و بالا آمدن سرور...
runtime\python\python.exe launch.py
pause
