@echo off
cd /d "%~dp0"
set "PATH=%PATH%;C:\Program Files\Git\cmd"
git add -A
git commit -m "Auto-update %date% %time%"
git push origin main
