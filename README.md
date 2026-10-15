# Employee Attendance & Analytics API

A high-performance REST API for employee attendance tracking, regularization, and multi-dimensional analytics built on FastAPI and MongoDB. The system enforces strict business rules (R1–R10) including Indian Standard Time (IST) overnight shifts, 10-minute grace periods, half-up rounding, atomic race safety, standard competition ranking with ties, and calendar gap-filling with windowed moving averages.

---

## Technology Stack

- **Framework**: FastAPI (Pydantic v2)
- **ASGI Server**: Uvicorn
- **Database**: MongoDB 7.0+
- **Driver**: PyMongo 4.7+
- **Language**: Python 3.11+
- **Configuration**: python-dotenv

---

## Prerequisites

- **Python 3.11 or newer**
- **Docker** (or an existing MongoDB 7.0+ deployment)

---

## 1. Start MongoDB (Docker)

If you already have a MongoDB Docker container named `mongo`:
```bash
docker start mongo
```

Or run a new MongoDB 7.0 container on port 27017:
```bash
docker run -d --name mongo -p 27017:27017 mongo:7
```

---

## 2. Installation

1. Create and activate a Python virtual environment:
   ```bash
   # Windows (PowerShell)
   python -m venv .venv
   .venv\Scripts\Activate.ps1

   # Linux / macOS
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

---

## 3. Environment Configuration

The application reads configuration from environment variables or a local `.env` file (copied from `.env.example`):
```bash
cp .env.example .env
```

Default settings:
```ini
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

---

## 4. Seed Sample Data (Optional)

Load the sample dataset into MongoDB:
```bash
python sample_seed.py
```

---

## 5. Run the Application

Start the FastAPI application with Uvicorn:
```bash
python -m uvicorn app.main:app --port 8000 --reload
```

Indexes are created idempotently on application startup via the FastAPI lifespan handler.

---

## 6. API Documentation

Interactive API documentation is generated automatically by FastAPI:
- **Swagger UI**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc**: [http://localhost:8000/redoc](http://localhost:8000/redoc)
- **OpenAPI JSON**: [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)

---

## Implemented Endpoints

### System & Health
- `GET /health`: Health check that pings MongoDB (`ping` command) and returns HTTP 200 if connected or HTTP 503 if unavailable.

### Employee Management
- `POST /employees`: Register a new employee with unique `emp_code`, email format validation, and distinct shift times. Returns HTTP 409 on duplicate code.
- `GET /employees`: Paginated employee listing with optional `department` filtering, sorted by `emp_code` ascending.

### Attendance Tracking
- `POST /attendance/punch-in`: Record attendance punch-in with IST conversion, overnight shift attribution (R1), and late minute calculation with 10-minute grace period (R2). Prevents duplicate daily records atomically via unique index constraint (HTTP 409).
- `POST /attendance/punch-out`: Record punch-out, computing work hours with half-up rounding (R4), overtime past shift end (R3), and half-day flag (R5).
- `GET /attendance`: Filter attendance logs by `emp_code`, date bounds (`date_from`, `date_to`), and `status`, sorted by `date` DESC and `emp_code` ASC with cursor-level pagination (R10).
- `PATCH /attendance/{emp_code}/{date}`: HR regularization endpoint. Recomputes all derived metrics, appends audit trail to `history`, and uses optimistic locking to prevent concurrent history loss.

### Analytics
- `GET /analytics/employees/{emp_code}/monthly`: Monthly attendance summary computing working days from `joined_on`, Mon–Fri present days (R7), weekend overtime inclusion, and attendance percentage (R8).
- `GET /analytics/departments/summary`: Monthly department metrics via cross-collection `$lookup` and `$group`. Correctly includes employees with zero logs in headcount (R9) and calculates record-level average work hours.
- `GET /analytics/leaderboard/late`: Top late-comers ranked using standard competition ranking (1, 2, 2, 4) via MongoDB `$setWindowFields` and `$rank`, applying `limit` after ranking with deterministic tie-breaking.
- `GET /analytics/departments/{department}/trend`: Daily attendance time-series using MongoDB `$densify` for gap filling (including weekends/empty days) and calculating 7-day moving averages in the aggregation pipeline.

### Admin & Performance
- `GET /admin/explain/{endpoint}`: Returns raw MongoDB `executionStats` for the main queries and aggregation pipelines of `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, and `department_trend`. Validates that all explained operations use index scans (`IXSCAN`) and avoid collection scans (`COLLSCAN`).

---

## Running Tests

Automated test discovery is configured using Python's standard `unittest` framework:

```bash
# Run all discovered tests
python -m unittest discover -s tests
```

Or run individual test modules directly:

```bash
# Phase 1: Business rule helpers (R1-R5, R8), timezone handling, and model validations
python -m unittest tests/test_phase1_business_rules.py

# Phase 2: Employee CRUD, punch-in race safety, and attendance listing
python -m unittest tests/test_phase2_endpoints.py

# Phase 3: Punch-out, overnight shift calculations, regularizations, and timezone regression
python -m unittest tests/test_phase3_endpoints.py

# Phase 4: Aggregation pipelines, monthly calculations, zero-log headcounts, ties, and $densify trends
python -m unittest tests/test_phase4_analytics.py

# Phase 5: Admin explain endpoints, schema validation, and 422 error handling
python -m unittest tests/test_phase5_explain.py

# Health & Docs: Fast failover, timeout check, Swagger / ReDoc docs, and OpenAPI schema
python -m unittest tests/test_health_and_docs.py
```

### PowerShell Test Runner (Windows)
```powershell
.\run_tests.ps1
```
