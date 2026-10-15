# Employee Attendance & Analytics API

Python 3.11+, FastAPI, PyMongo and MongoDB 6.0+.
## Setup and Run

### 1. Create a virtual environment

**Windows (PowerShell)**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux/macOS**

```bash
python -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 3. Configure MongoDB

Create a local `.env` file in the project root:

```env
MONGO_URI=your_mongodb_connection_uri
MONGO_DB=attendance_db
```

Replace the placeholder with your MongoDB connection URI. Do not commit `.env` or any database credentials.

The application should use the `MONGO_URI` and `MONGO_DB` environment variables supplied by the runtime environment.

### 4. Start the API

```bash
uvicorn app.main:app --port 8000
```

### 5. Explore the API

- Interactive API documentation: http://localhost:8000/docs
- Health check: http://localhost:8000/health

Use the API documentation to inspect available endpoints and test requests.

### Database compatibility

The application requires MongoDB 6.0 or later. Use the assignment's provided seed data and evaluation instructions when testing analytics and aggregation endpoints.
