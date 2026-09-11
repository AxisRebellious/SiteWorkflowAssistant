@echo off
chcp 65001 >nul
echo متوقف کردن سرور سایت اسیستنت...
taskkill /F /IM uvicorn.exe /T >nul 2>&1
taskkill /F /IM python.exe /T >nul 2>&1
echo سرویس‌ها با موفقیت متوقف شدند.
timeout /t 3 >nul
