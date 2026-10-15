# Employee Attendance & Analytics API

Python 3.11+, FastAPI, PyMongo and MongoDB 6.0+.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
uvicorn app.main:app --port 8000
```

The service loads a local `.env` when present; already-exported environment variables take precedence. Indexes are created idempotently at startup. Do not commit `.env` or secrets.

The application source is `app/main.py`. Use MongoDB 6.0+ for `$densify` and `$setWindowFields`.
