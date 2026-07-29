@echo off
rem ASME drawing (.prt + .pdf + report) from a Siemens NX part, from any folder:
rem   draw C:\hardware\reaction_wheel\wheel.prt
rem Outputs land next to the model: <model>_dwg.prt, <model>_dwg.pdf,
rem <model>_dwg_report.txt. Requires a licensed NX install (auto-discovered).
call "%~dp0generate_drawing.cmd" %*
