@echo off
rem Local first-principles CNC cost/time estimate for an NX part — no Protolabs.
rem   cost model.prt --material=6061 --qty=3          full cost breakdown
rem   cost model.prt --material=316 --qty=1 --time    machining time only
rem   cost model.prt --json                           machine-readable
rem Geometry is extracted headlessly via NX on first run (cached in
rem <model>_geom.json until the .prt changes).
python "%~dp0machinist.py" %*
