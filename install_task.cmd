@echo off
rem Registers a Windows Task Scheduler job that runs one scan every 5 minutes
rem (silently, no console window) while you are logged on.
set "PYW=%LocalAppData%\Programs\Python\Python312\pythonw.exe"
if not exist "%PYW%" (
  echo pythonw.exe not found at %PYW% - edit this file to point at your Python.
  pause
  exit /b 1
)
schtasks /Create /F /SC MINUTE /MO 5 /TN "GPU Alert Bot" /TR "\"%PYW%\" \"%~dp0gpu_alert.py\""
if errorlevel 1 (
  echo Failed to create the task.
) else (
  echo Task "GPU Alert Bot" created: scans every 5 minutes. Run uninstall_task.cmd to remove it.
)
pause
