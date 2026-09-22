@echo off
REM One-click start for RiskDesk on Windows. First run installs deps and loads the sample data.
cd /d "%~dp0"
if not exist db.sqlite3 (
  pip install -r requirements.txt
  python manage.py migrate
  python manage.py load_sample_data
)
echo.
echo RiskDesk running at http://127.0.0.1:8000  (login: cio / riskdesk)
echo.
python manage.py runserver 127.0.0.1:8000
