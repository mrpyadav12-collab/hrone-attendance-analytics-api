# Employee Attendance & Analytics API

FastAPI and MongoDB implementation for the HROne trainee assignment. All API code is in `app/main.py`; MongoDB 6.0+ is required.

## Run on Windows
Install Python 3.11 or newer. From this directory, create a virtual environment and install dependencies:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
python -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` may also be set in a local `.env` file; real environment variables take precedence. The grader seeds its own dataset directly into MongoDB.
