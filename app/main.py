"""
Employee Attendance & Analytics API (FastAPI + MongoDB)
Full production-grade implementation strictly adhering to openapi.yaml,
DATA_MODEL.md, and business rules R1 through R10.
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import logging
import os
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Configure structured logging
logger = logging.getLogger("attendance_api")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Load environment configuration (real environment variables take precedence)
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(
    MONGO_URI,
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[MONGO_DB]
employees = db["employees"]
attendance_logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Helpers & Business Logic (R1 - R10)
# --------------------------------------------------------------------------- #
def dt_to_epoch_ms(dt: Union[datetime, int, float, str, None]) -> Optional[int]:
    """
    Convert datetime (BSON/Python), int, float, or ISO string to integer epoch ms.

    - Returns None if dt is None or unparseable.
    - If already an int or float, returns integer epoch milliseconds.
    - If string, parses ISO format (including 'Z' UTC designator).
    - If datetime, standardizes naive datetimes to UTC and uses round() to
      prevent floating point precision truncation under IEEE 754.
    """
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return int(dt)
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(round(dt.timestamp() * 1000))
    return None


def epoch_ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    """Convert integer epoch milliseconds to UTC datetime, truncated to whole seconds (R1)."""
    if ms is None:
        return None
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def now_epoch_ms() -> int:
    """Current UTC instant in epoch milliseconds, truncated to whole seconds (R1)."""
    sec = int(datetime.now(timezone.utc).timestamp())
    return sec * 1000


def round_half_up(val: float, decimals: int) -> float:
    """Standard half-up rounding (R8)."""
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(Decimal(str(val)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start): a punch-in whose IST time is
    strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    is_overnight = shift_end <= shift_start
    if is_overnight:
        end_h, end_m = map(int, shift_end.split(":"))
        if (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second) < (end_h, end_m, 0):
            prev_day = punch_in_ist.date() - timedelta(days=1)
            return prev_day.isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(
    punch_in_dt: datetime, shift_start: str, shift_end: str, date_str: str
) -> int:
    """
    R2 Late. Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 (600 seconds) after shift_start;
    then late_minutes = whole minutes (floor) elapsed since shift_start. Otherwise 0.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    start_dt_ist = datetime.fromisoformat(date_str).replace(
        hour=start_h, minute=start_m, second=0, microsecond=0, tzinfo=IST
    )
    diff_sec = (punch_in_ist - start_dt_ist).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4 Work hours.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    sec = int((punch_out_dt - punch_in_dt).total_seconds())
    hours_dec = (Decimal(sec) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(hours_dec)


def compute_overtime(
    punch_out_dt: datetime, shift_start: str, shift_end: str, date_str: str
) -> int:
    """
    R3 Overtime.
    Whole minutes (floor) from shift_end to punch_out, counted only if that is >= 30; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day (date + 1 day).
    """
    punch_out_ist = punch_out_dt.astimezone(IST)
    end_date = datetime.fromisoformat(date_str).date()
    if shift_end <= shift_start:
        end_date += timedelta(days=1)
    end_h, end_m = map(int, shift_end.split(":"))
    end_dt_ist = datetime(
        end_date.year, end_date.month, end_date.day, end_h, end_m, 0, tzinfo=IST
    )

    diff_sec = (punch_out_ist - end_dt_ist).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def calculate_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7 Working days.
    Monday - Friday, no holiday calendar. For an employee who joined mid-month,
    working days are counted from joined_on to month-end.
    """
    year, month = map(int, month_str.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    m_start = date(year, month, 1)
    m_end = date(year, month, last_day)

    j_date = date.fromisoformat(joined_on_str)
    start = max(m_start, j_date)
    if start > m_end:
        return 0

    count = 0
    curr = start
    while curr <= m_end:
        if curr.weekday() < 5:  # Monday=0 ... Friday=4
            count += 1
        curr += timedelta(days=1)
    return count


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format employee document for API response (never exposing _id, created_at in epoch ms)."""
    d = dict(doc)
    d.pop("_id", None)
    d.pop("id", None)
    d["created_at"] = dt_to_epoch_ms(d.get("created_at"))
    return d


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format attendance record for API response (never exposing _id/id, datetimes in epoch ms)."""
    d = dict(doc)
    d.pop("_id", None)
    d.pop("id", None)
    d["punch_in"] = dt_to_epoch_ms(d.get("punch_in"))
    d["punch_out"] = dt_to_epoch_ms(d.get("punch_out"))
    d["half_day"] = d.get("half_day", False)
    d["late_minutes"] = d.get("late_minutes", 0)
    d["overtime_minutes"] = d.get("overtime_minutes", 0)

    formatted_history = []
    for h in d.get("history", []):
        h_copy = dict(h)
        h_copy["at"] = dt_to_epoch_ms(h_copy.get("at"))
        raw_changes = h_copy.get("changes", {})
        fmt_changes = {}
        for k, v in raw_changes.items():
            ch_copy = dict(v)
            if k in ("punch_in", "punch_out"):
                ch_copy["from"] = dt_to_epoch_ms(ch_copy.get("from"))
                ch_copy["to"] = dt_to_epoch_ms(ch_copy.get("to"))
            fmt_changes[k] = ch_copy
        h_copy["changes"] = fmt_changes
        formatted_history.append(h_copy)
    d["history"] = formatted_history
    return d


# --------------------------------------------------------------------------- #
# Pydantic Models & Schemas
# --------------------------------------------------------------------------- #
EpochMillisType = Annotated[
    int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)
]
EpochMillisNullableType = Optional[
    Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
]

StatusLiteral = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusLiteral = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shifts_and_date(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date YYYY-MM-DD")
        return self


class EmployeeOut(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: List[EmployeeOut]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillisType] = None
    status: PresenceStatusLiteral = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillisType] = None


class RegularizeRequest(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Optional[StatusLiteral] = None
    punch_in: Optional[EpochMillisType] = None
    punch_out: Optional[EpochMillisType] = None


class HistoryEntryOut(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Dict[str, Any]]


class AttendanceRecordOut(BaseModel):
    emp_code: str
    date: str
    status: StatusLiteral
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    work_hours: Optional[float] = None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntryOut] = Field(default_factory=list)


class AttendancePage(BaseModel):
    items: List[AttendanceRecordOut]
    total: int
    page: int
    page_size: int


class EmployeeMonthlyOut(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryOut(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardOut(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class TrendOut(BaseModel):
    department: str
    items: List[TrendItem]


class ExplainOut(BaseModel):
    endpoint: str
    collection: str
    explain: Dict[str, Any]


# --------------------------------------------------------------------------- #
# Application Lifecycle & Index Creation
# --------------------------------------------------------------------------- #
def init_indexes():
    """Create all required indexes idempotently at startup."""
    try:
        # employees indexes
        db.employees.create_index(
            [("emp_code", 1)], unique=True, name="idx_emp_code_unique"
        )
        db.employees.create_index(
            [("department", 1), ("emp_code", 1)], name="idx_emp_dept_code"
        )
        db.employees.create_index(
            [("joined_on", 1), ("department", 1)], name="idx_emp_joined_dept"
        )

        # attendance_logs indexes
        db.attendance_logs.create_index(
            [("emp_code", 1), ("date", 1)],
            unique=True,
            name="idx_att_emp_date_unique",
        )
        db.attendance_logs.create_index(
            [("date", -1), ("emp_code", 1)], name="idx_att_date_desc_emp_asc"
        )
        db.attendance_logs.create_index(
            [("emp_code", 1), ("punch_in", -1)], name="idx_att_emp_punch_in"
        )
        db.attendance_logs.create_index(
            [("date", 1), ("late_minutes", 1), ("emp_code", 1)],
            name="idx_att_date_late_emp",
        )
        db.attendance_logs.create_index(
            [("status", 1), ("date", -1), ("emp_code", 1)],
            name="idx_att_status_date_emp",
        )
        logger.info("MongoDB indexes verified/created successfully.")
    except Exception as exc:
        logger.warning("Index initialization notice: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", response_model=Dict[str, str])
def health():
    """Readiness probe. 200 only when MongoDB answers a ping; else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201, response_model=EmployeeOut)
def create_employee(body: EmployeeCreate):
    """
    Create an employee.
    Unique emp_code enforced by database unique index.
    created_at is set by the server and returned as epoch milliseconds.
    """
    now_dt = epoch_ms_to_dt(now_epoch_ms())
    doc = body.model_dump()
    doc["created_at"] = now_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return format_employee_doc(doc)


@app.get("/employees", response_model=EmployeePage)
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending. Paginated."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecordOut)
def punch_in(body: PunchInRequest):
    """Record a punch-in. Creates day's record (R1, R2); race-safe via unique index."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ms = body.punched_at if body.punched_at is not None else now_epoch_ms()
    ms = (ms // 1000) * 1000  # Truncate to whole seconds per R1
    punch_in_dt = epoch_ms_to_dt(ms)

    att_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(
        punch_in_dt, emp["shift_start"], emp["shift_end"], att_date
    )

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
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
            status_code=409, detail="Already punched in for this date"
        )

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200, response_model=AttendanceRecordOut)
def punch_out(body: PunchOutRequest):
    """Record a punch-out. Computes work_hours, overtime, half_day (R3 - R5)."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    ms = body.punched_at if body.punched_at is not None else now_epoch_ms()
    ms = (ms // 1000) * 1000  # Truncate to whole seconds per R1
    punch_out_dt = epoch_ms_to_dt(ms)

    # Find employee's most recent record whose punch_in <= punch_out_dt
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not rec:
        raise HTTPException(status_code=404, detail="No punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Already punched out")

    punch_in_dt = rec["punch_in"]
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    if (punch_out_dt - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(
            status_code=422,
            detail="punch_out cannot be more than 24 hours after punch_in",
        )

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_mins = compute_overtime(
        punch_out_dt, emp["shift_start"], emp["shift_end"], rec["date"]
    )
    half_day = bool(work_hours < 4.50)

    # Atomic update to prevent race conditions on simultaneous punch-out
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance", response_model=AttendancePage)
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusLiteral] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date desc, emp_code asc. Paginated."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecordOut)
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """
    Regularize (manually correct) one day's record.
    Recomputes derived fields and appends one entry to history.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # Evaluate new status and punch times
    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch times when status is ABSENT or LEAVE",
            )
        new_status = body.status
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_status = body.status if body.status is not None else rec["status"]
        if new_status in ("ABSENT", "LEAVE"):
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(
                    status_code=422,
                    detail="Cannot supply punch times when status is ABSENT or LEAVE",
                )
            new_punch_in = None
            new_punch_out = None
            new_work_hours = None
            new_late_minutes = 0
            new_overtime_minutes = 0
            new_half_day = False
        else:
            # Presence status requires punch_in
            if body.punch_in is not None:
                new_punch_in = epoch_ms_to_dt((body.punch_in // 1000) * 1000)
            else:
                new_punch_in = rec.get("punch_in")

            if new_punch_in is None:
                raise HTTPException(
                    status_code=422, detail="Presence status requires punch_in"
                )

            # Validate punch_in stays on record's attendance date
            computed_date = get_attendance_date(
                new_punch_in, emp["shift_start"], emp["shift_end"]
            )
            if computed_date != date:
                raise HTTPException(
                    status_code=422,
                    detail="punch_in must stay on the record's attendance date",
                )

            if body.punch_out is not None:
                new_punch_out = epoch_ms_to_dt((body.punch_out // 1000) * 1000)
            else:
                new_punch_out = rec.get("punch_out")

            if new_punch_out is not None:
                if new_punch_out <= new_punch_in:
                    raise HTTPException(
                        status_code=422, detail="punch_out must be after punch_in"
                    )
                if (new_punch_out - new_punch_in).total_seconds() > 86400:
                    raise HTTPException(
                        status_code=422,
                        detail="punch_out must be within 24 hours of punch_in",
                    )
                new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
                new_overtime_minutes = compute_overtime(
                    new_punch_out, emp["shift_start"], emp["shift_end"], date
                )
                new_half_day = bool(new_work_hours < 4.50)
            else:
                new_work_hours = None
                new_overtime_minutes = 0
                new_half_day = False

            new_late_minutes = compute_late_minutes(
                new_punch_in, emp["shift_start"], emp["shift_end"], date
            )

    # Determine what actually changed
    field_pairs = [
        ("status", rec.get("status"), new_status),
        ("punch_in", rec.get("punch_in"), new_punch_in),
        ("punch_out", rec.get("punch_out"), new_punch_out),
        ("work_hours", rec.get("work_hours"), new_work_hours),
        ("late_minutes", rec.get("late_minutes", 0), new_late_minutes),
        ("overtime_minutes", rec.get("overtime_minutes", 0), new_overtime_minutes),
        ("half_day", rec.get("half_day", False), new_half_day),
    ]

    changes_mongo: Dict[str, Dict[str, Any]] = {}
    for f_name, old_v, new_v in field_pairs:
        if old_v != new_v:
            changes_mongo[f_name] = {"from": old_v, "to": new_v}

    if not changes_mongo:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_dt = epoch_ms_to_dt(now_epoch_ms())
    hist_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_mongo,
    }

    current_history = rec.get("history", [])
    # Atomic optimistic check on history size ensures concurrent updates do not lose history
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "history": {"$size": len(current_history)}},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": hist_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(
            status_code=409, detail="Concurrent modification conflict"
        )

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregations)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(
    emp_code: str, month: str
) -> List[Dict[str, Any]]:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
            }
        },
        {
            "$project": {
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "day_of_week": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                },
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
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
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
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthlyOut)
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee (aggregation)."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = calculate_working_days(month, emp["joined_on"])
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(
    month: str, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    year, month_num = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_num)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    match_q: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_q["department"] = department

    return [
        {"$match": match_q},
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
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "is_weekday": {
                                "$in": [
                                    {
                                        "$dayOfWeek": {
                                            "$dateFromString": {
                                                "dateString": "$date"
                                            }
                                        }
                                    },
                                    [2, 3, 4, 5, 6],
                                ]
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                "$$this.is_weekday",
                                                {
                                                    "$in": [
                                                        "$$this.status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                            ]
                                        },
                                        {"$cond": ["$$this.half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "work_hours_list": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {
                                    "$in": [
                                        "$$l.status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {"$add": ["$$value", "$$this.late_minutes"]},
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$round": [{"$avg": "$work_hours_list.work_hours"}, 2]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary", response_model=DepartmentSummaryOut)
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    """Department summary for a month (aggregation across both collections)."""
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        items.append(
            {
                "department": r["department"],
                "headcount": r["headcount"],
                "present_days": float(r["present_days"]),
                "avg_work_hours": (
                    float(r["avg_work_hours"])
                    if r["avg_work_hours"] is not None
                    else None
                ),
                "late_count": r["late_count"],
                "total_late_minutes": r["total_late_minutes"],
                "leave_count": r["leave_count"],
                "on_duty_count": r["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month: str, limit: int, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    month_start = f"{month}-01"
    month_end = f"{month}-31"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
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

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "_id": 1,
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


@app.get("/analytics/leaderboard/late", response_model=LeaderboardOut)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """Late-comers ranking with competition ties (aggregation, window function)."""
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(
    department: str, from_str: str, to_str: str
) -> List[Dict[str, Any]]:
    return [
        {"$match": {"date": {"$gte": from_str, "$lte": to_str}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
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
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        # Union with from and to endpoint dates to ensure full span for $densify
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "dates": [
                                {"$dateFromString": {"dateString": from_str}},
                                {"$dateFromString": {"dateString": to_str}},
                            ]
                        }
                    },
                    {"$unwind": "$dates"},
                    {
                        "$project": {
                            "_id": 0,
                            "date_dt": "$dates",
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
                "present_count": {"$max": "$present_count"},
                "late_count": {"$max": "$late_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date_dt": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        # Gap-filling: densify daily series in the database
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": "full",
                },
            }
        },
        {
            "$project": {
                "date": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}
                },
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$dayOfWeek": "$date_dt"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"_id": 0, "joined_on": 1}},
                ],
                "as": "dept_emps",
            }
        },
        {
            "$project": {
                "date": 1,
                "present_count": 1,
                "late_count": 1,
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend", response_model=TrendOut)
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily series with gap filling and 7-day moving average (aggregation)."""
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(status_code=422, detail="range exceeds 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", response_model=ExplainOut)
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusLiteral] = Query(None),
    from_date: Optional[str] = Query(
        None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"
    ),
    to_date: Optional[str] = Query(
        None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Runs MongoDB explain with executionStats verbosity for endpoint's main query."""
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=422, detail="date_from must be <= date_to"
            )
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        skip = (page - 1) * page_size
        find_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        explain_raw = db.command(
            "explain",
            find_cmd,
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(explain_raw)),
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422, detail="emp_code and month are required"
            )
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_raw = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(explain_raw)),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_department_summary_pipeline(month, department)
        explain_raw = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": json.loads(json_util.dumps(explain_raw)),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        explain_raw = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(explain_raw)),
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422, detail="department, from, and to are required"
            )
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from:
            raise HTTPException(
                status_code=422, detail="to must be on or after from"
            )
        if (d_to - d_from).days + 1 > 92:
            raise HTTPException(status_code=422, detail="range exceeds 92 days")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        explain_raw = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": json.loads(json_util.dumps(explain_raw)),
        }
