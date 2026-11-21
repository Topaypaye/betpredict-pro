@echo off
cd /d "%~dp0"
set "PATH=%PATH%;C:\Program Files\Git\cmd"
echo Staging and committing any changes...
git add -A
git commit -m "Auto-update %date% %time%" 2>nul
echo Pushing to GitHub (https://github.com/Topaypaye/betpredict-pro.git)...
git push -u origin main
echo.
echo Done! You can close this window.
pause
