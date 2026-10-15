"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status as http_status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

# Timezones
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Idempotent Index Initialization
# --------------------------------------------------------------------------- #
def init_indexes():
    """Create all required MongoDB indexes idempotently."""
    # employees collection
    # 1. Unique index on emp_code for fast lookups and race safety
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    # 2. Compound index on department and emp_code for filtered/sorted listing
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    # 3. Compound index on department and joined_on for headcount lookups
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    # 4. Index on joined_on for period headcount filtering
    db.employees.create_index([("joined_on", ASCENDING)])

    # attendance_logs collection
    # 1. Unique compound index on (emp_code, date) for 1 record per employee per day
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    # 2. Compound index for GET /attendance: sorted by date DESC, emp_code ASC
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    # 3. Compound index for employee history/monthly queries: emp_code ASC, date DESC
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)])
    # 4. Compound index for status queries: status ASC, date DESC, emp_code ASC
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)])
    # 5. Compound index for late leaderboard: date ASC, late_minutes ASC
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING)])
    # 6. Compound index for punch-out lookups: emp_code ASC, punch_in DESC
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for index initialization and client cleanup."""
    try:
        init_indexes()
    except Exception:
        # If MongoDB is temporarily down at boot, continue so the app can start
        # and report status via the /health endpoint
        pass
    yield
    client.close()


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root():
    """Redirect root to Swagger documentation."""
    return RedirectResponse(url="/docs")


# --------------------------------------------------------------------------- #
# Helpers: Datetime, Rounding, and Conversions
# --------------------------------------------------------------------------- #
def to_utc_dt(dt: datetime) -> datetime:
    """Ensure datetime is timezone-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def truncate_to_seconds(dt: datetime) -> datetime:
    """R1: Truncate instant to whole seconds before any calculation."""
    return to_utc_dt(dt).replace(microsecond=0)


def epoch_ms_to_dt(ms: int) -> datetime:
    """Convert integer epoch milliseconds to truncated UTC datetime."""
    seconds = ms // 1000
    return datetime.fromtimestamp(seconds, tz=UTC)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    dt_utc = truncate_to_seconds(dt)
    return int(dt_utc.timestamp() * 1000)


def round_half_up(value: float | Decimal, decimals: int = 2) -> float:
    """R4 / R8: Round half-up (not banker's rounding) to specified decimal places."""
    d = Decimal(str(value))
    exp = Decimal("10") ** -decimals
    return float(d.quantize(exp, rounding=ROUND_HALF_UP))


def compute_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Compute attendance date (YYYY-MM-DD) in IST.
    For overnight shifts (shift_end <= shift_start), a punch-in strictly earlier
    than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = truncate_to_seconds(punch_in_dt).astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        punch_time = (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second)
        if punch_time < (end_h, end_m, 0):
            prev_date = punch_in_ist.date() - timedelta(days=1)
            return prev_date.isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: Optional[str] = None) -> int:
    """
    R2: Late calculation with 10-minute grace period.
    Punch-in is late only if strictly > 10:00 (600 seconds) after shift_start.
    When late, late_minutes = whole minutes (floor) since shift_start. Otherwise 0.
    """
    p_in = truncate_to_seconds(punch_in).astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    if date_str:
        y, mo, d = map(int, date_str.split("-"))
        shift_start_dt = datetime(y, mo, d, h, m, 0, tzinfo=IST)
    else:
        shift_start_dt = p_in.replace(hour=h, minute=m, second=0, microsecond=0)

    elapsed_seconds = (p_in - shift_start_dt).total_seconds()
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    Null until punch-out.
    """
    if punch_in is None or punch_out is None:
        return None
    p_in = truncate_to_seconds(punch_in)
    p_out = truncate_to_seconds(punch_out)
    seconds = (p_out - p_in).total_seconds()
    if seconds <= 0:
        return 0.0
    hours = Decimal(str(seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: Optional[datetime],
    shift_end: str,
    date_str: str,
    shift_start: Optional[str] = None,
) -> int:
    """
    R3: Overtime calculation.
    Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    For overnight shifts (shift_end <= shift_start), shift_end falls on next calendar day.
    """
    if punch_out is None:
        return 0
    p_out = truncate_to_seconds(punch_out).astimezone(IST)
    end_h, end_m = map(int, shift_end.split(":"))
    y, mo, d = map(int, date_str.split("-"))
    end_dt = datetime(y, mo, d, end_h, end_m, 0, tzinfo=IST)

    if shift_start is not None and shift_end <= shift_start:
        end_dt += timedelta(days=1)

    elapsed_seconds = (p_out - end_dt).total_seconds()
    if elapsed_seconds < 0:
        return 0
    minutes = int(elapsed_seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: True when rounded work_hours < 4.50 (after punch-out)."""
    return bool(work_hours is not None and work_hours < 4.50)


def parse_month_bounds(month_str: str) -> tuple[str, str, int, int, int]:
    """Validate and return (first_day, last_day, days_in_month, year, month)."""
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month_str):
        raise HTTPException(status_code=422, detail="Invalid month format, expected YYYY-MM")
    y, m = map(int, month_str.split("-"))
    _, days_in_month = calendar.monthrange(y, m)
    first_day = f"{month_str}-01"
    last_day = f"{month_str}-{days_in_month:02d}"
    return first_day, last_day, days_in_month, y, m


# --------------------------------------------------------------------------- #
# Document Formatters (Enforcing OpenAPI schemas: no internal _id or id)
# --------------------------------------------------------------------------- #
def format_employee(doc: dict) -> dict:
    """Format employee document for API response."""
    c_at = doc.get("created_at")
    created_at_ms = dt_to_epoch_ms(c_at) if isinstance(c_at, datetime) else c_at
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


def format_history_entry(h: dict) -> dict:
    """Format history entry for audit trail."""
    changes = {}
    for k, v in h.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            from_val = dt_to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val
            to_val = dt_to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val
        changes[k] = {"from": from_val, "to": to_val}

    return {
        "at": dt_to_epoch_ms(h.get("at")) if isinstance(h.get("at"), datetime) else h.get("at"),
        "by": h.get("by"),
        "reason": h.get("reason"),
        "changes": changes,
    }


def format_attendance_doc(doc: dict) -> dict:
    """Format attendance document for API response."""
    p_in = doc.get("punch_in")
    p_out = doc.get("punch_out")
    punch_in_ms = dt_to_epoch_ms(p_in) if isinstance(p_in, datetime) else p_in
    punch_out_ms = dt_to_epoch_ms(p_out) if isinstance(p_out, datetime) else p_out

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": punch_in_ms,
        "punch_out": punch_out_ms,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": doc.get("half_day") or False,
        "history": [format_history_entry(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_employee(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            dt_date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid date YYYY-MM-DD")
        return self


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class AttendanceStatus(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


def validate_epoch_millis(v: Any) -> int:
    """Validate instant is an integer epoch milliseconds."""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or isinstance(v, float):
        raise ValueError("Must be an integer epoch milliseconds")
    if not (100000000000 <= v <= 4102444800000):
        raise ValueError("Epoch milliseconds out of range (100000000000 to 4102444800000)")
    return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None
    status: PresenceStatus = PresenceStatus.PRESENT

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v):
        return validate_epoch_millis(v)


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v):
        return validate_epoch_millis(v)


class RegularizeIn(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[AttendanceStatus] = None
    punch_in: Optional[Any] = None
    punch_out: Optional[Any] = None

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def check_instants(cls, v):
        return validate_epoch_millis(v)


class ErrorResponse(BaseModel):
    detail: str


# --------------------------------------------------------------------------- #
# Endpoints: CRUD & Attendance Operations
# --------------------------------------------------------------------------- #
@app.get(
    "/health",
    responses={
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
def health():
    """Readiness probe: returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )


@app.post(
    "/employees",
    status_code=201,
    responses={
        409: {"model": ErrorResponse, "description": "emp_code already exists"},
        422: {"model": ErrorResponse, "description": "Validation error"},
    },
)
def create_employee(body: EmployeeIn):
    """
    Create an employee.
    Enforced by database unique index on emp_code (race-safe: duplicate creates yield 409).
    """
    doc = body.model_dump()
    doc["created_at"] = truncate_to_seconds(datetime.now(UTC))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )
    return format_employee(doc)


@app.get(
    "/employees",
    responses={
        422: {"model": ErrorResponse, "description": "Validation error"},
    },
)
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    items = [format_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post(
    "/attendance/punch-in",
    status_code=201,
    responses={
        404: {"model": ErrorResponse, "description": "Employee not found"},
        409: {"model": ErrorResponse, "description": "already punched in for this date"},
        422: {"model": ErrorResponse, "description": "Validation error"},
    },
)
def punch_in(body: PunchInIn):
    """
    Record a punch-in.
    Race-safe via unique compound index on (emp_code, date) without check-then-insert.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    if body.punched_at is not None:
        ts = epoch_ms_to_dt(body.punched_at)
    else:
        ts = truncate_to_seconds(datetime.now(UTC))

    att_date = compute_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(ts, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status.value,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    return format_attendance_doc(doc)


@app.post(
    "/attendance/punch-out",
    status_code=200,
    responses={
        404: {"model": ErrorResponse, "description": "Employee not found or no open punch-in record found"},
        409: {"model": ErrorResponse, "description": "Record already punched out"},
        422: {"model": ErrorResponse, "description": "punched_at must be strictly after punch_in or exceeds 24 hours"},
    },
)
def punch_out(body: PunchOutIn):
    """
    Record a punch-out.
    Closes the employee's most recent record whose punch_in is <= punched_at.
    Computes work_hours (R4), overtime_minutes (R3) and half_day (R5).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts = epoch_ms_to_dt(body.punched_at)
    else:
        ts = truncate_to_seconds(datetime.now(UTC))

    # Find most recent record with punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts, "$ne": None},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        future_record = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$gt": ts},
            },
            sort=[("punch_in", ASCENDING)],
        )
        if future_record:
            raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")
        raise HTTPException(status_code=404, detail="No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    p_in = to_utc_dt(record["punch_in"])
    if ts <= p_in:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")

    if (ts - p_in).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(p_in, ts)
    overtime_mins = compute_overtime(ts, emp["shift_end"], record["date"], emp["shift_start"])
    half_day = compute_half_day(work_hours)

    res = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not res:
        raise HTTPException(status_code=409, detail="Record already punched out")

    return format_attendance_doc(res)


@app.get(
    "/attendance",
    responses={
        422: {"model": ErrorResponse, "description": "Validation error"},
    },
)
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    Fast on ~100k records: paginated and sorted at the database level.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be after date_to",
        )
    if date_from:
        try:
            dt_date.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="Invalid date_from format",
            )
    if date_to:
        try:
            dt_date.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="Invalid date_to format",
            )
    if status and status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status: {status}",
        )

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch(
    "/attendance/{emp_code}/{date}",
    status_code=200,
    responses={
        404: {"model": ErrorResponse, "description": "Employee or attendance record not found"},
        409: {"model": ErrorResponse, "description": "Concurrent edit conflict"},
        422: {"model": ErrorResponse, "description": "Invalid regularization payload or request changed nothing"},
    },
)
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    """
    Regularize (manually correct) one day's record.
    Recomputes derived fields, appends to history, and prevents concurrent history loss.
    """
    try:
        dt_date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format in path")

    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    old_status = record["status"]
    old_punch_in = to_utc_dt(record["punch_in"]) if record.get("punch_in") is not None else None
    old_punch_out = to_utc_dt(record["punch_out"]) if record.get("punch_out") is not None else None
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes") or 0
    old_overtime_minutes = record.get("overtime_minutes") or 0
    old_half_day = record.get("half_day") or False

    target_status = body.status.value if body.status is not None else old_status

    # Rule: setting status ABSENT or LEAVE clears both punch times, so supplying punch_in or punch_out with it is 422
    if body.status in (AttendanceStatus.ABSENT, AttendanceStatus.LEAVE) and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(
            status_code=422,
            detail="Cannot supply punch_in or punch_out when setting status to ABSENT or LEAVE",
        )

    if target_status in ("ABSENT", "LEAVE"):
        target_punch_in = None
        target_punch_out = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            target_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            target_punch_in = old_punch_in

        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        target_punch_in = to_utc_dt(target_punch_in)

        # punch_in must stay on record's attendance date
        if compute_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(status_code=422, detail="punch_in does not belong to record attendance date")

        if body.punch_out is not None:
            target_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            target_punch_out = old_punch_out

        if target_punch_out is not None:
            target_punch_out = to_utc_dt(target_punch_out)
            if target_punch_out <= target_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if (target_punch_out - target_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=422, detail="punch_out cannot be more than 24 hours after punch_in"
                )

        target_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date)
        target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
        target_overtime_minutes = compute_overtime(
            target_punch_out, emp["shift_end"], date, emp["shift_start"]
        )
        target_half_day = compute_half_day(target_work_hours)

    # Detect actual changes
    changes = {}
    if old_status != target_status:
        changes["status"] = {"from": old_status, "to": target_status}
    if old_punch_in != target_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": target_punch_in}
    if old_punch_out != target_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": target_punch_out}
    if old_work_hours != target_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": target_work_hours}
    if old_late_minutes != target_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": target_late_minutes}
    if old_overtime_minutes != target_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": target_overtime_minutes}
    if old_half_day != target_half_day:
        changes["half_day"] = {"from": old_half_day, "to": target_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changed nothing")

    now_utc = truncate_to_seconds(datetime.now(UTC))
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history = record.get("history")
    filter_history = current_history if current_history is not None else {"$in": [None, []]}

    res = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": filter_history,
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": target_work_hours,
                "late_minutes": target_late_minutes,
                "overtime_minutes": target_overtime_minutes,
                "half_day": target_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if not res:
        raise HTTPException(status_code=409, detail="Concurrent edit conflict")

    return format_attendance_doc(res)


# --------------------------------------------------------------------------- #
# Endpoints: Analytics (Phase 4)
# --------------------------------------------------------------------------- #
@app.get(
    "/analytics/employees/{emp_code}/monthly",
    responses={
        404: {"model": ErrorResponse, "description": "Employee not found"},
        422: {"model": ErrorResponse, "description": "Validation error"},
    },
)
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """
    Monthly attendance summary for one employee.
    Calculated via a MongoDB aggregation pipeline. working_days per R7. present_days per R5-R7.
    """
    first_day, last_day, days_in_month, y, m = parse_month_bounds(month)

    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "emp_code": 1, "joined_on": 1})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    emp_joined = dt_date.fromisoformat(emp["joined_on"])
    month_start = dt_date(y, m, 1)
    month_end = dt_date(y, m, days_in_month)

    # R7: Working days Mon-Fri, counted from joined_on for mid-month joiners
    start_date = max(month_start, emp_joined)
    if start_date > month_end:
        working_days = 0
    else:
        working_days = sum(
            1 for d in range((month_end - start_date).days + 1)
            if (start_date + timedelta(days=d)).weekday() < 5
        )

    weekday_dates = [
        (month_start + timedelta(days=d)).isoformat()
        for d in range(days_in_month)
        if (month_start + timedelta(days=d)).weekday() < 5
    ]

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day, "$lte": last_day},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$date", weekday_dates]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        row = res[0]
        present_days = float(row.get("present_days") or 0.0)
        leave_days = int(row.get("leave_days") or 0)
        late_count = int(row.get("late_count") or 0)
        total_late_minutes = int(row.get("total_late_minutes") or 0)
        total_overtime_minutes = int(row.get("total_overtime_minutes") or 0)
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = (
        round_half_up((present_days / working_days) * 100, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get(
    "/analytics/departments/summary",
    responses={
        422: {"model": ErrorResponse, "description": "Validation error"},
    },
)
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department summary for a month.
    Aggregation across collections. Headcount per R9 (includes people with 0 logs).
    Metrics calculated entirely within MongoDB aggregation pipeline, avoiding $push 16MB limit.
    """
    first_day, last_day, days_in_month, y, m = parse_month_bounds(month)
    month_start = dt_date(y, m, 1)
    weekday_dates = [
        (month_start + timedelta(days=d)).isoformat()
        for d in range(days_in_month)
        if (month_start + timedelta(days=d)).weekday() < 5
    ]

    match_emp = {"joined_on": {"$lte": last_day}}
    if department:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", first_day]},
                                    {"$lte": ["$date", last_day]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$logs.date", weekday_dates]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$logs.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    res = list(db.employees.aggregate(pipeline))
    items = []

    for r in res:
        headcount = r["headcount"]
        if headcount == 0:
            continue

        raw_avg_wh = r.get("avg_work_hours")
        avg_wh = round_half_up(raw_avg_wh, 2) if raw_avg_wh is not None else None

        items.append({
            "department": r["department"],
            "headcount": headcount,
            "present_days": float(r.get("present_days") or 0.0),
            "avg_work_hours": avg_wh,
            "late_count": int(r.get("late_count") or 0),
            "total_late_minutes": int(r.get("total_late_minutes") or 0),
            "leave_count": int(r.get("leave_count") or 0),
            "on_duty_count": int(r.get("on_duty_count") or 0),
        })

    return {"month": month, "items": items}


@app.get(
    "/analytics/leaderboard/late",
    responses={
        422: {"model": ErrorResponse, "description": "Validation error"},
    },
)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Late-comers leaderboard for a month.
    Ranks employees by total late_minutes (only total > 0).
    Standard competition ranking (1, 2, 2, 4) using $setWindowFields and $rank.
    """
    first_day, last_day, _, _, _ = parse_month_bounds(month)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": first_day, "$lte": last_day},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        {
            "$match": {"rank": {"$lte": limit}},
        },
        {
            "$sort": {"total_late_minutes": -1, "_id": 1},
        },
    ])

    docs = list(db.attendance_logs.aggregate(pipeline))
    items = [
        {
            "rank": d["rank"],
            "emp_code": d["_id"],
            "name": d["emp"]["name"],
            "department": d["emp"]["department"],
            "total_late_minutes": d["total_late_minutes"],
            "late_count": d["late_count"],
        }
        for d in docs
    ]

    return {"month": month, "items": items}


@app.get(
    "/analytics/departments/{department}/trend",
    responses={
        404: {"model": ErrorResponse, "description": "Department not found"},
        422: {"model": ErrorResponse, "description": "Validation error"},
    },
)
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to_: str = Query(..., alias="to"),
):
    """
    Daily attendance trend for a department with gap filling via $densify and 7-day moving average.
    Work is performed in MongoDB aggregation.
    """
    try:
        from_date = dt_date.fromisoformat(from_)
        to_date = dt_date.fromisoformat(to_)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to cannot be before from")

    if (to_date - from_date).days > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

    # 404 if department has no employees
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]

    from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=UTC)
    to_dt = datetime(to_date.year, to_date.month, to_date.day, tzinfo=UTC)
    to_dt_plus_1 = to_dt + timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_, "$lte": to_},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "date_dt": {"$literal": from_dt},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$date_dt",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {
            "$project": {
                "date_dt": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"date": "$date_dt", "format": "%Y-%m-%d"}},
                "day_of_week": {"$dayOfWeek": "$date_dt"},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]

    docs = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for d in docs:
        rate = d["attendance_rate"]
        mov_avg = d["moving_avg_7d"]
        rate_rounded = round_half_up(rate, 4) if rate is not None else None
        mov_rounded = round_half_up(mov_avg, 4) if mov_avg is not None else None

        items.append({
            "date": d["date"],
            "is_working_day": d["is_working_day"],
            "headcount": d["headcount"],
            "present_count": d["present_count"],
            "late_count": d["late_count"],
            "attendance_rate": rate_rounded,
            "moving_avg_7d": mov_rounded,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint (Phase 5)
# --------------------------------------------------------------------------- #
class ExplainEndpoint(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


@app.get(
    "/admin/explain/{endpoint}",
    responses={
        422: {"model": ErrorResponse, "description": "Validation error or unsupported endpoint"},
    },
)
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to_: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    MongoDB explain with executionStats verbosity for an endpoint's main query or pipeline.
    Built exactly as that endpoint builds it (same filters, sort, pagination).
    """
    ep = endpoint.value

    if ep == "attendance_list":
        if date_from:
            try:
                dt_date.fromisoformat(date_from)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date_from format")
        if date_to:
            try:
                dt_date.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date_to format")
        if status and status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise HTTPException(status_code=422, detail=f"Invalid status: {status}")

        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        skip = (page - 1) * page_size
        cursor = (
            db.attendance_logs.find(q, {"_id": 0})
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip(skip)
            .limit(page_size)
        )
        explain_doc = cursor.explain()
        return {
            "endpoint": ep,
            "collection": "attendance_logs",
            "explain": explain_doc,
        }

    elif ep == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")

        first_day, last_day, days_in_month, y, m = parse_month_bounds(month)
        month_start = dt_date(y, m, 1)
        weekday_dates = [
            (month_start + timedelta(days=d)).isoformat()
            for d in range(days_in_month)
            if (month_start + timedelta(days=d)).weekday() < 5
        ]

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": first_day, "$lte": last_day},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$in": ["$date", weekday_dates]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "total_overtime_minutes": {
                        "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                    },
                }
            },
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": ep,
            "collection": "attendance_logs",
            "explain": explain_doc,
        }

    elif ep == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        first_day, last_day, days_in_month, y, m = parse_month_bounds(month)
        month_start = dt_date(y, m, 1)
        weekday_dates = [
            (month_start + timedelta(days=d)).isoformat()
            for d in range(days_in_month)
            if (month_start + timedelta(days=d)).weekday() < 5
        ]
        match_emp = {"joined_on": {"$lte": last_day}}
        if department:
            match_emp["department"] = department

        pipeline = [
            {"$match": match_emp},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", first_day]},
                                        {"$lte": ["$date", last_day]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {
                "$group": {
                    "_id": "$department",
                    "employees": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$in": ["$logs.date", weekday_dates]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "avg_work_hours": {
                        "$avg": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$ne": ["$logs.work_hours", None]},
                                    ]
                                },
                                "$logs.work_hours",
                                None,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$logs.late_minutes", 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "ON_DUTY"]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$project": {
                    "department": "$_id",
                    "headcount": {"$size": "$employees"},
                    "present_days": 1,
                    "avg_work_hours": 1,
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            },
            {"$sort": {"department": 1}},
        ]
        explain_doc = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": ep,
            "collection": "employees",
            "explain": explain_doc,
        }

    elif ep == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        first_day, last_day, _, _, _ = parse_month_bounds(month)
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": first_day, "$lte": last_day},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "emp",
                }
            },
            {"$unwind": "$emp"},
        ]

        if department:
            pipeline.append({"$match": {"emp.department": department}})

        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}},
                    },
                }
            },
            {
                "$match": {"rank": {"$lte": limit}},
            },
            {
                "$sort": {"total_late_minutes": -1, "_id": 1},
            },
        ])

        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": ep,
            "collection": "attendance_logs",
            "explain": explain_doc,
        }

    elif ep == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_:
            raise HTTPException(status_code=422, detail="from is required for department_trend")
        if not to_:
            raise HTTPException(status_code=422, detail="to is required for department_trend")

        try:
            from_date = dt_date.fromisoformat(from_)
            to_date = dt_date.fromisoformat(to_)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD")

        if to_date < from_date:
            raise HTTPException(status_code=422, detail="to cannot be before from")

        if (to_date - from_date).days > 92:
            raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

        emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
        from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=UTC)
        to_dt = datetime(to_date.year, to_date.month, to_date.day, tzinfo=UTC)
        to_dt_plus_1 = to_dt + timedelta(days=1)

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": emp_codes},
                    "date": {"$gte": from_, "$lte": to_},
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$late_minutes", 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$project": {
                    "date_dt": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                    "present_count": 1,
                    "late_count": 1,
                }
            },
            {
                "$unionWith": {
                    "coll": "employees",
                    "pipeline": [
                        {"$match": {"department": department}},
                        {"$limit": 1},
                        {
                            "$project": {
                                "_id": 0,
                                "date_dt": {"$literal": from_dt},
                                "present_count": {"$literal": 0.0},
                                "late_count": {"$literal": 0},
                            }
                        },
                    ],
                }
            },
            {
                "$group": {
                    "_id": "$date_dt",
                    "present_count": {"$sum": "$present_count"},
                    "late_count": {"$sum": "$late_count"},
                }
            },
            {
                "$project": {
                    "date_dt": "$_id",
                    "present_count": 1,
                    "late_count": 1,
                }
            },
            {
                "$densify": {
                    "field": "date_dt",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [from_dt, to_dt_plus_1],
                    },
                }
            },
            {
                "$project": {
                    "date": {"$dateToString": {"date": "$date_dt", "format": "%Y-%m-%d"}},
                    "day_of_week": {"$dayOfWeek": "$date_dt"},
                    "present_count": {"$ifNull": ["$present_count", 0.0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                }
            },
            {
                "$addFields": {
                    "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"d": "$date"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$department", department]},
                                        {"$lte": ["$joined_on", "$$d"]},
                                    ]
                                }
                            }
                        },
                        {"$count": "count"},
                    ],
                    "as": "hc",
                }
            },
            {
                "$addFields": {
                    "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]},
                }
            },
            {
                "$addFields": {
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$is_working_day", True]},
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {"$divide": ["$present_count", "$headcount"]},
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {
                                "documents": [-6, "current"],
                            },
                        }
                    },
                }
            },
            {"$sort": {"date": 1}},
        ]

        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": ep,
            "collection": "attendance_logs",
            "explain": explain_doc,
        }

    raise HTTPException(status_code=422, detail=f"Unsupported endpoint: {ep}")

