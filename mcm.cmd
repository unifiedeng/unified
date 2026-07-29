@echo off
rem McMaster-Carr live data at API speed, from any folder. Auto-starts the
rem siteapi daemon (headed logged-in browser window) on first use.
rem   mcm search "socket head cap screw"     keyword or part-number search
rem   mcm product 91290A115                  full product JSON (specs, CAD, image)
rem   mcm cadlinks 91290A115                 available CAD files for a part
rem   mcm cad 91290A115 STEP                 download CAD to browser-agent\files
python "%~dp0siteapi.py" mcmaster %*
