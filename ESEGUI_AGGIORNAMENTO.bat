@echo off
cd /d C:\cruscotto
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe aggiorna_tutor.py C:\cruscotto
) else (
  py -3 aggiorna_tutor.py C:\cruscotto
)
pause
