@echo off
chcp 65001 >nul
cd /d "%~dp0"

title ربات صدور لایسنس - بله
echo ========================================================
echo   ربات مدیریت و صدور لایسنس (پیام‌رسان بله)
echo ========================================================
echo.

if exist "%~dp0runtime\python\python.exe" (
    echo [اطلاع] در حال اجرای ربات با پایتون پرتابل...
    "%~dp0runtime\python\python.exe" admin_bot.py
) else (
    echo [هشدار] پایتون پرتابل یافت نشد؛ اجرا با پایتون سیستم...
    python admin_bot.py
)

if errorlevel 1 (
    echo.
    echo [خطا] اجرای ربات با مشکل مواجه شد یا متوقف گردید.
    pause
)
