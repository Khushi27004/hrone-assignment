"""Employee Attendance & Analytics API (FastAPI + MongoDB)."""
import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

DateType = date  # alias: the PATCH route has a path parameter named `date`

load_dotenv()  # real environment variables win over .env

# --------------------------------------------------------------------------- #
# Database connection
# --------------------------------------------------------------------------- #
client = MongoClient(
    os.environ["MONGO_URI"],
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.environ["MONGO_DB"]]

# --------------------------------------------------------------------------- #
# Indexes (created at startup, safe to run many times)
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    # one employee per emp_code; also serves lookups and the default list order
    db.employees.create_index("emp_code", unique=True, name="emp_code_unique")
    # list employees of one department, already sorted by emp_code
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="dept_emp_code")
    # headcount per department: employees who joined on or before a date
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="dept_joined_on")
    # at most one attendance record per employee per day
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name="emp_date_unique"
    )
    # attendance list with no filter or a date range: newest first, ties by emp_code
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_emp_code")
    # attendance list filtered by status, same sort order
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_emp_code"
    )
    _indexes_ready = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ensure_indexes()
    except Exception:
        pass  # /health retries, so a slow database does not stop the app starting
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# Time helpers (rules R1 - R5)
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
GRACE_SECONDS = 10 * 60


def now_utc() -> datetime:
    """Current time, kept to whole milliseconds (what MongoDB stores)."""
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def parse_hhmm(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end) and ist.time() < parse_hhmm(shift_end):
        day -= timedelta(days=1)
    return day


def shift_start_dt(day: date, shift_start: str) -> datetime:
    return datetime.combine(day, parse_hhmm(shift_start), tzinfo=IST)


def shift_end_dt(day: date, shift_start: str, shift_end: str) -> datetime:
    end_day = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    return datetime.combine(end_day, parse_hhmm(shift_end), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, day: date, shift_start: str) -> int:
    seconds = (punch_in - shift_start_dt(day, shift_start)).total_seconds()
    return int(seconds // 60) if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, day: date, shift_start: str, shift_end: str) -> int:
    seconds = (punch_out - shift_end_dt(day, shift_start, shift_end)).total_seconds()
    minutes = int(seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Request models (these produce the 422 errors)
# --------------------------------------------------------------------------- #
# strict=True means 1.5 or "123" are refused instead of being quietly converted
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


def _check_real_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValueError("must be a real calendar date, YYYY-MM-DD")
    return value


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_a_date(cls, v: str) -> str:
        return _check_real_date(v)

    @model_validator(mode="after")
    def shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
StatusValue = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class RegularizeIn(BaseModel):
    status: Optional[StatusValue] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


MAX_SHIFT_SECONDS = 24 * 3600


def unprocessable(field: str, msg: str) -> HTTPException:
    return HTTPException(422, [{"loc": ["body", field], "msg": msg, "type": "value_error"}])


DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


def check_date_param(name: str, value: Optional[str], where: str = "query") -> Optional[str]:
    """A query-string date must be a real calendar date, otherwise 422."""
    if value is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            422,
            [{"loc": [where, name], "msg": "must be a real calendar date", "type": "value_error"}],
        )
    return value


# --------------------------------------------------------------------------- #
# Analytics helpers
# --------------------------------------------------------------------------- #
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


def month_bounds(month: str) -> tuple:
    """'2026-07' -> (date(2026, 7, 1), date(2026, 7, 31)); year 0000 is a 422."""
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        raise HTTPException(
            422, [{"loc": ["query", "month"], "msg": "year out of range", "type": "value_error"}]
        )
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def weekdays_between(first: date, last: date) -> list:
    """Every Monday-Friday date string from first to last, inclusive (R7)."""
    out, day = [], first
    while day <= last:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def round_half_up(value, places: int) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    """One employee, one month: a single $group over the stored derived values."""
    weekdays = weekdays_between(first, last)
    late = {"$ifNull": ["$late_minutes", 0]}
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [
                                {"$in": ["$status", list(PRESENCE)]},
                                {"$in": ["$date", weekdays]},  # Mon-Fri only
                            ]},
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": late},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_pipelines(first: date, last: date, department: Optional[str]) -> tuple:
    """Returns (logs_pipeline, headcount_pipeline) for the department summary.

    headcount_pipeline reads EMPLOYEES, so people with zero logs are counted (R9).
    logs_pipeline reads the month's ATTENDANCE LOGS and joins each one to its employee."""
    weekdays = weekdays_between(first, last)
    first_iso, last_iso = first.isoformat(), last.isoformat()
    late = {"$ifNull": ["$late_minutes", 0]}
    is_presence = {"$in": ["$status", list(PRESENCE)]}
    has_hours = {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}
    presence_with_hours = {"$and": [is_presence, has_hours]}

    emp_match = {"joined_on": {"$lte": last_iso}}
    joined_match = {"emp.joined_on": {"$lte": last_iso}}
    if department is not None:
        emp_match["department"] = department
        joined_match["emp.department"] = department

    logs_pipeline = [
        {"$match": {"date": {"$gte": first_iso, "$lte": last_iso}}},
        {"$lookup": {"from": "employees", "localField": "emp_code",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
        {"$match": joined_match},
        {
            "$group": {
                "_id": "$emp.department",
                "present_days": {"$sum": {"$cond": [
                    {"$and": [is_presence, {"$in": ["$date", weekdays]}]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                    0,
                ]}},
                "work_hours_sum": {"$sum": {"$cond": [presence_with_hours, "$work_hours", 0]}},
                "work_hours_n": {"$sum": {"$cond": [presence_with_hours, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": late},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        },
    ]
    headcount_pipeline = [
        {"$match": emp_match},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]
    return logs_pipeline, headcount_pipeline


def leaderboard_pipeline(first: date, last: date, department: Optional[str]) -> list:
    """Per-employee late totals for the month, joined to employees, best (latest) first."""
    late = {"$ifNull": ["$late_minutes", 0]}
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": late},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department
    pipeline.append({"$sort": {"total_late_minutes": -1, "_id": 1}})
    return pipeline


def trend_pipelines(codes: list, department: str, first: date, last: date) -> tuple:
    """Returns (logs_pipeline, headcount_pipeline) for the department trend."""
    start = datetime(first.year, first.month, first.day, tzinfo=UTC)
    stop = datetime(last.year, last.month, last.day, tzinfo=UTC) + timedelta(days=1)
    logs_pipeline = [
        {"$match": {
            "emp_code": {"$in": codes},
            "date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", list(PRESENCE)]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # gap filling: one document for EVERY day from first to last, even with no records
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [start, stop]}}},
        {"$match": {"d": {"$lt": stop}}},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
    ]
    # employees who joined before the range (one bucket) or on each day inside it
    headcount_pipeline = [
        {"$match": {"department": department, "joined_on": {"$lte": last.isoformat()}}},
        {"$group": {
            "_id": {"$cond": [{"$lt": ["$joined_on", first.isoformat()]}, "before", "$joined_on"]},
            "n": {"$sum": 1},
        }},
    ]
    return logs_pipeline, headcount_pipeline


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


def attendance_query(emp_code, date_from, date_to, status) -> dict:
    """The filter of GET /attendance (validates the dates, 422 on bad input)."""
    check_date_param("date_from", date_from)
    check_date_param("date_to", date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            422,
            [{"loc": ["query", "date_from"], "msg": "date_from must not be after date_to", "type": "value_error"}],
        )
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


def trend_range(from_: str, to: str) -> tuple:
    """Validates the from/to of the department trend; returns (first, last) dates."""
    check_date_param("from", from_)
    check_date_param("to", to)
    first, last = date.fromisoformat(from_), date.fromisoformat(to)
    n_days = (last - first).days + 1
    if n_days < 1:
        raise HTTPException(422, [{"loc": ["query", "to"], "msg": "to must not be before from", "type": "value_error"}])
    if n_days > 92:
        raise HTTPException(422, [{"loc": ["query", "to"], "msg": "range must not exceed 92 days", "type": "value_error"}])
    return first, last


# --------------------------------------------------------------------------- #
# Output helpers (what leaves the API: no _id, instants as epoch ms)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def history_out(entry: dict) -> dict:
    changes = {}
    for field, pair in entry["changes"].items():
        if field in ("punch_in", "punch_out"):  # dates hiding inside a nested object
            pair = {k: (dt_to_ms(v) if isinstance(v, datetime) else v) for k, v in pair.items()}
        changes[field] = pair
    return {
        "at": dt_to_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes,
    }


def record_out(doc: dict) -> dict:
    """Older records may lack some fields: missing means 0 / false / empty."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc["punch_in"]) if doc.get("punch_in") else None,
        "punch_out": dt_to_ms(doc["punch_out"]) if doc.get("punch_out") else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in doc.get("history") or []],
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        ensure_indexes()
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index decides who wins a race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {} if department is None else {"department": department}
    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    if body.punched_at is not None:
        ts = ms_to_dt(body.punched_at)  # cut to whole seconds (R1)
    else:
        ts = now_utc().replace(microsecond=0)
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index settles races
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [record_out(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    if body.punched_at is not None:
        ts = ms_to_dt(body.punched_at)
    else:
        ts = now_utc().replace(microsecond=0)

    # newest record that started at or before this punch-out (works across midnight)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts, "$type": "date"}},
        sort=[("date", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    elapsed = (ts - rec["punch_in"]).total_seconds()
    if elapsed <= 0:
        raise unprocessable("punched_at", "must be after punch_in")
    if elapsed > MAX_SHIFT_SECONDS:
        raise unprocessable("punched_at", "must be within 24 hours of punch_in")

    day = date.fromisoformat(rec["date"])
    hours = compute_work_hours(rec["punch_in"], ts)
    changes = {
        "punch_out": ts,
        "work_hours": hours,
        "overtime_minutes": compute_overtime(ts, day, emp["shift_start"], emp["shift_end"]),
        "half_day": compute_half_day(hours),
    }
    # compare-and-set: only succeeds if the record is STILL open
    result = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None}, {"$set": changes}
    )
    if result.matched_count == 0:
        raise HTTPException(409, "already punched out")
    rec.update(changes)
    return record_out(rec)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    body: RegularizeIn,
    date: str = Path(pattern=DATE_PATTERN),
):
    check_date_param("date", date, "path")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    # what is stored now (older records may lack some fields)
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }

    # ---- decide the final status and punch times, validating as we go ----
    status = body.status or old["status"]
    if status not in PRESENCE:  # ABSENT / LEAVE clear both punch times
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable("status", "ABSENT or LEAVE cannot come with punch times")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if new_in is None:
            raise unprocessable("punch_in", "a presence status needs a punch_in")
        if body.punch_in is not None:
            day_of_punch = attendance_date(new_in, emp["shift_start"], emp["shift_end"])
            if day_of_punch.isoformat() != date:
                raise unprocessable("punch_in", "must stay on the record's attendance date")
        if new_out is not None:
            elapsed = (new_out - new_in).total_seconds()
            if elapsed <= 0:
                raise unprocessable("punch_out", "must be after punch_in")
            if elapsed > MAX_SHIFT_SECONDS:
                raise unprocessable("punch_out", "must be within 24 hours of punch_in")

    # ---- recompute every derived field from the final times (R2 - R5) ----
    if new_in is None:
        derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        day = DateType.fromisoformat(date)
        derived = {
            "work_hours": None,
            "late_minutes": compute_late_minutes(new_in, day, emp["shift_start"]),
            "overtime_minutes": 0,
            "half_day": False,
        }
        if new_out is not None:
            hours = compute_work_hours(new_in, new_out)
            derived["work_hours"] = hours
            derived["overtime_minutes"] = compute_overtime(
                new_out, day, emp["shift_start"], emp["shift_end"]
            )
            derived["half_day"] = compute_half_day(hours)
    new = {"status": status, "punch_in": new_in, "punch_out": new_out, **derived}

    # ---- only the fields that really changed go into the audit entry ----
    changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
    if not changes:
        raise unprocessable("body", "the request changes nothing")
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # ---- write only if nobody changed the record since we read it ----
    n_history = len(rec.get("history") or [])
    if n_history:
        history_guard = {"history": {"$size": n_history}}
    else:
        history_guard = {"$or": [{"history": {"$size": 0}}, {"history": None}]}
    result = db.attendance_logs.update_one(
        {
            "_id": rec["_id"],
            "status": old["status"],
            "punch_in": old["punch_in"],
            "punch_out": old["punch_out"],
            **history_guard,
        },
        {"$set": new, "$push": {"history": entry}},
    )
    if result.matched_count == 0:
        raise HTTPException(409, "record was changed by someone else, please retry")
    rec.update(new)
    rec["history"] = list(rec.get("history") or []) + [entry]
    return record_out(rec)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    first, last = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, last)))
    totals = rows[0] if rows else {}

    # R7: working days count from the joining date for someone who joined mid-month
    start = max(first, date.fromisoformat(emp["joined_on"]))
    working_days = len(weekdays_between(start, last)) if start <= last else 0

    present_days = totals.get("present_days", 0)
    attendance_pct = None
    if working_days > 0:
        pct = Decimal(str(present_days)) / Decimal(working_days) * 100
        attendance_pct = float(pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present_days),
        "leave_days": totals.get("leave_days", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": totals.get("total_late_minutes", 0),
        "total_overtime_minutes": totals.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_PATTERN),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    logs_pipeline, headcount_pipeline = department_pipelines(first, last, department)
    sums = {row["_id"]: row for row in db.attendance_logs.aggregate(logs_pipeline)}

    items = []
    for row in db.employees.aggregate(headcount_pipeline):  # already sorted by department
        name = row["_id"]
        s = sums.get(name, {})
        n = s.get("work_hours_n", 0)
        avg_work_hours = None
        if n > 0:  # mean over RECORDS (not per-employee), half-up to 2 decimals
            total = Decimal(str(round(s["work_hours_sum"], 6)))
            avg_work_hours = float((total / n).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        items.append({
            "department": name,
            "headcount": row["headcount"],
            "present_days": float(s.get("present_days", 0)),
            "avg_work_hours": avg_work_hours,
            "late_count": s.get("late_count", 0),
            "total_late_minutes": s.get("total_late_minutes", 0),
            "leave_count": s.get("leave_count", 0),
            "on_duty_count": s.get("on_duty_count", 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    items = []
    rank, previous_total = 0, None
    for position, row in enumerate(
        db.attendance_logs.aggregate(leaderboard_pipeline(first, last, department)), start=1
    ):
        total = row["total_late_minutes"]
        if total != previous_total:  # a new total: rank jumps to this position (1, 2, 2, 4)
            rank, previous_total = position, total
        if rank > limit:  # limit applies AFTER ranking, so ties at the cut-off stay in
            break
        items.append({
            "rank": rank,
            "emp_code": row["_id"],
            "name": row["emp"]["name"],
            "department": row["emp"]["department"],
            "total_late_minutes": total,
            "late_count": row["late_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=DATE_PATTERN),
    to: str = Query(..., pattern=DATE_PATTERN),
):
    first, last = trend_range(from_, to)
    n_days = (last - first).days + 1

    codes = db.employees.distinct("emp_code", {"department": department})
    if not codes:
        raise HTTPException(404, "department not found")

    logs_pipeline, headcount_pipeline = trend_pipelines(codes, department, first, last)
    per_day = {row["date"]: row for row in db.attendance_logs.aggregate(logs_pipeline)}
    joined = {row["_id"]: row["n"] for row in db.employees.aggregate(headcount_pipeline)}

    headcount = joined.get("before", 0)  # R9: everyone who joined on or before that day
    rows, rates = [], []
    for offset in range(n_days):
        day = first + timedelta(days=offset)
        key = day.isoformat()
        headcount += joined.get(key, 0)
        stats = per_day.get(key, {})  # $densify already supplies every day; this is a safety net
        present = stats.get("present_count", 0)
        working = day.weekday() < 5
        rate = None
        if working and headcount > 0:
            rate = float((Decimal(str(present)) / headcount).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        rates.append(rate)
        window = [Decimal(str(r)) for r in rates[-7:] if r is not None]  # this row + up to 6 before
        moving = None
        if window:
            moving = float((sum(window) / len(window)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        rows.append({
            "date": key,
            "is_working_day": working,
            "headcount": headcount,
            "present_count": float(present),
            "late_count": stats.get("late_count", 0),
            "attendance_rate": rate,
            "moving_avg_7d": moving,
        })
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# Part C: explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal[
    "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
]


def require(**values) -> None:
    """422 for every required query parameter that was not given."""
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise HTTPException(
            422, [{"loc": ["query", n], "msg": "field required", "type": "missing"} for n in missing]
        )


def run_explain(command: dict) -> dict:
    """MongoDB explain with executionStats, returned as plain JSON."""
    result = db.command("explain", command, verbosity="executionStats")
    return json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),  # accepted for compatibility; the pipeline has no $limit
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[StatusValue] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
    to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    collection = "attendance_logs"
    if endpoint == "attendance_list":
        query = attendance_query(emp_code, date_from, date_to, status)
        command = {
            "find": collection,
            "filter": query,
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
            "projection": {"_id": 0},
        }
    else:
        if endpoint == "employee_monthly":
            require(emp_code=emp_code, month=month)
            first, last = month_bounds(month)
            pipeline = monthly_pipeline(emp_code, first, last)
        elif endpoint == "department_summary":
            require(month=month)
            first, last = month_bounds(month)
            pipeline = department_pipelines(first, last, department)[0]
        elif endpoint == "late_leaderboard":
            require(month=month)
            first, last = month_bounds(month)
            pipeline = leaderboard_pipeline(first, last, department)
        else:  # department_trend
            require(department=department, **{"from": from_}, to=to)
            first, last = trend_range(from_, to)
            codes = db.employees.distinct("emp_code", {"department": department})
            if not codes:
                raise HTTPException(404, "department not found")
            pipeline = trend_pipelines(codes, department, first, last)[0]
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(command)}