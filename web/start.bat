@echo off
setlocal
cd /d "%~dp0.."
python -B -m web.server %*
endlocal
