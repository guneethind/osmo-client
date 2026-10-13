@echo off
rem Osmo Client: starts the app and opens its page (arguments pass through, e.g. osmo-client.bat --off).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0osmo-client.ps1" %*
rem Keep the window open after an error, so the message can be read.
if errorlevel 1 pause
