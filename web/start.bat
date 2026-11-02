@echo off
setlocal
cd /d "%~dp0.."
if not defined SCADA_PYTHON set "SCADA_PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%SCADA_PYTHON%" set "SCADA_PYTHON=python"
"%SCADA_PYTHON%" -B -m web.server %*
endlocal
