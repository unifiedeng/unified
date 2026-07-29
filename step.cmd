@echo off
rem Convert an NX part to STEP, headless.
rem Usage: step.cmd model.prt [output.step] [203^|214^|242]
setlocal

if "%~1"=="" (
    echo Usage: step.cmd model.prt [output.step] [203^|214^|242]
    exit /b 1
)
set "NXSTEP_MODEL=%~f1"
set "NXSTEP_OUT=%~2"
set "NXSTEP_AP=%~3"
if not defined NXSTEP_AP set NXSTEP_AP=242

call :find_nx || exit /b 1
"%NX_DIR%\NXBIN\run_journal.exe" "%~dp0nx_step.py"
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
