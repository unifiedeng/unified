@echo off
rem Protolabs quote from anywhere, by any agent or human:
rem   quote C:\hardware\reaction_wheel\wheel.prt --material="Aluminum 6061-T651/T6" --qty=3
rem Starts the signed-in browser daemon automatically if it is not running.
rem Prints the quote JSON (price, lead time, PDF path). Never places orders.
python "%~dp0protolabs.py" client %*
