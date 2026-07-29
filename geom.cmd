@echo off
rem Extract machining-relevant geometry from an NX part to JSON, headless.
rem Usage: geom.cmd model.prt [output.json]
setlocal

if "%~1"=="" (
    echo Usage: geom.cmd model.prt [output.json]
    exit /b 1
)
set "NXGEOM_MODEL=%~f1"
set "NXGEOM_OUT=%~2"

call :find_nx || exit /b 1
"%NX_DIR%\NXBIN\run_journal.exe" "%~dp0nx_geom.py"
exit /b

:find_nx
if defined NX_DIR if exist "%NX_DIR%\NXBIN\run_journal.exe" exit /b 0
for /d %%D in ("C:\Program Files\Siemens\Designcenter*") do (
    if exist "%%D\NXBIN\run_journal.exe" (
        set "NX_DIR=%%D"
        exit /b 0
    )
)
for /d %%D in ("C:\Program Files\Siemens\*") do (
    if exist "%%D\NXBIN\run_journal.exe" (
        set "NX_DIR=%%D"
        exit /b 0
    )
)
if defined UGII_BASE_DIR if exist "%UGII_BASE_DIR%\NXBIN\run_journal.exe" (
    set "NX_DIR=%UGII_BASE_DIR%"
    exit /b 0
)
echo Could not find NX. Set NX_DIR to your NX install folder (the one containing NXBIN).
exit /b 1
