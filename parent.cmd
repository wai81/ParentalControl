@echo off
rem Opens the parent control panel with administrator rights
set "PCPS=%ProgramData%\ParentalControl\parent.ps1"
if not exist "%PCPS%" (
  echo Parental control is not installed. Run install.cmd first.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%PCPS%\"'"
