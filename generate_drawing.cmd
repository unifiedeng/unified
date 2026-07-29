@echo off
rem Generate an ASME-compliant drawing (.prt + .pdf + report) from an NX part.
rem Usage: generate_drawing.cmd path\to\model.prt
setlocal

if "%~1"=="" (
    echo Usage: generate_drawing.cmd path\to\model.prt
    echo Outputs ^<model^>_dwg.prt, ^<model^>_dwg.pdf, ^<model^>_dwg_report.txt next to the model.
    exit /b 1
)
set "NXDRAW_MODEL=%~f1"

call :find_nx || exit /b 1
rem Pin the runtime to the chosen install so a stale global UGII_BASE_DIR can't
rem silently redirect run_journal to a different NX.
set "UGII_BASE_DIR=%NX_DIR%"
if exist "%NX_DIR%\UGII\NXSTUDENTLICENSE.lic" set "SPLM_LICENSE_SERVER=%NX_DIR%\UGII\NXSTUDENTLICENSE.lic"

rem --- Guard against the auth deadlock -------------------------------------
rem DesigncenterX2606 uses a cloud (named-user) license. If its token is stale,
rem run_journal opens a Siemens sign-in in the browser whose OAuth redirect
rem (redirect_uri=http://localhost:8500) collides with cgm2pdf's plot socket on
rem the SAME port 8500 -- the redirect can never land, so it reopens the browser
rem forever. If port 8500 is already occupied before we start, something is
rem wedged; abort now instead of spawning an endless login loop.
call :port8500_busy && (
    echo ERROR: Port 8500 is already in use -- refusing to launch NX to avoid the
    echo        Siemens cloud-auth redirect deadlock. Kill the stuck process first:
    echo        powershell "Get-NetTCPConnection -LocalPort 8500 ^| ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"
    exit /b 2
)

echo Using NX at %NX_DIR%
echo Generating drawing for %NXDRAW_MODEL% ...
"%NX_DIR%\NXBIN\run_journal.exe" "%~dp0nx_drawing_generator.py"
exit /b

:find_nx
rem Honor an explicit NX_DIR override first.
if defined NX_DIR if exist "%NX_DIR%\NXBIN\run_journal.exe" exit /b 0
rem Prefer the Designcenter build: its cloud license permits NX Open / journal
rem execution. The Student Edition is deliberately NOT preferred -- its license
rem has no journal-run feature (fails 3615094: Unable to reserve license).
for /d %%D in ("C:\Program Files\Siemens\Designcenter*") do (
    if exist "%%D\NXBIN\run_journal.exe" (
        set "NX_DIR=%%D"
        exit /b 0
    )
)
rem Fall back to any NX install that has run_journal.
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

:port8500_busy
rem Returns success (errorlevel 0) if something is LISTENING on port 8500.
netstat -ano -p tcp | findstr /r /c:":8500 .*LISTENING" >nul 2>&1
exit /b %errorlevel%
