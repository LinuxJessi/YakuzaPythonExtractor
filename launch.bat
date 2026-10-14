@echo off
rem Yakuza Extractor — Windows launcher.
cd /d "%~dp0"
where py >nul 2>nul && (py -3 gui.py %* & goto :eof)
where python >nul 2>nul && (python gui.py %* & goto :eof)
echo Python 3 is not installed. Get it from https://www.python.org/downloads/ (tick "Add python to PATH").
pause
