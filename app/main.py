"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP
import os
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes(target_db=None):
    database = target_db if target_db is not None else db
    database.employees.create_index([("emp_code", 1)], unique=True)
    database.employees.create_index([("department", 1), ("emp_code", 1)])
    database.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    database.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    database.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

try:
    init_indexes()
except Exception:
    pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_attendance_date_and_late(punch_in_utc: datetime, shift_start: str, shift_end: str) -> tuple[str, int]:
    punch_in_ist = punch_in_utc.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    is_overnight = (end_h, end_m) <= (start_h, start_m)
    if is_overnight and (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second) < (end_h, end_m, 0):
        att_date = punch_in_ist.date() - timedelta(days=1)
    else:
        att_date = punch_in_ist.date()

    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day, start_h, start_m, 0, tzinfo=IST)
    elapsed_seconds = (punch_in_ist - shift_start_dt).total_seconds()

    if elapsed_seconds <= 600:
        late_minutes = 0
    else:
        late_minutes = int(elapsed_seconds // 60)

    return att_date.isoformat(), late_minutes


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    elapsed_seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = (elapsed_seconds / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str = "09:30") -> int:
    punch_out_ist = punch_out.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    is_overnight = (end_h, end_m) <= (start_h, start_m)

    att_year, att_month, att_day = map(int, date_str.split("-"))
    att_date = datetime(att_year, att_month, att_day).date()
    if is_overnight:
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date

    shift_end_dt = datetime(end_date.year, end_date.month, end_date.day, end_h, end_m, 0, tzinfo=IST)
    ot_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    if ot_seconds < 1800:
        return 0
    return int(ot_seconds // 60)


def format_attendance_record(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    d.pop("id", None)

    if isinstance(d.get("punch_in"), datetime):
        dt_in = d["punch_in"]
        if dt_in.tzinfo is None:
            dt_in = dt_in.replace(tzinfo=timezone.utc)
        d["punch_in"] = int(dt_in.timestamp() * 1000)

    if isinstance(d.get("punch_out"), datetime):
        dt_out = d["punch_out"]
        if dt_out.tzinfo is None:
            dt_out = dt_out.replace(tzinfo=timezone.utc)
        d["punch_out"] = int(dt_out.timestamp() * 1000)

    # Defaults for legacy records (DATA_MODEL.md sample 9)
    if "history" not in d or d["history"] is None:
        d["history"] = []
    else:
        for h in d["history"]:
            if isinstance(h.get("at"), datetime):
                dt_at = h["at"]
                if dt_at.tzinfo is None:
                    dt_at = dt_at.replace(tzinfo=timezone.utc)
                h["at"] = int(dt_at.timestamp() * 1000)
            if "changes" in h and isinstance(h["changes"], dict):
                for change in h["changes"].values():
                    if isinstance(change, dict):
                        if isinstance(change.get("from"), datetime):
                            dt_c = change["from"]
                            if dt_c.tzinfo is None:
                                dt_c = dt_c.replace(tzinfo=timezone.utc)
                            change["from"] = int(dt_c.timestamp() * 1000)
                        if isinstance(change.get("to"), datetime):
                            dt_c = change["to"]
                            if dt_c.tzinfo is None:
                                dt_c = dt_c.replace(tzinfo=timezone.utc)
                            change["to"] = int(dt_c.timestamp() * 1000)

    if "half_day" not in d or d["half_day"] is None:
        d["half_day"] = False

    if "late_minutes" not in d or d["late_minutes"] is None:
        d["late_minutes"] = 0

    if "overtime_minutes" not in d or d["overtime_minutes"] is None:
        d["overtime_minutes"] = 0

    return d


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except (ValueError, TypeError):
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return v

    @model_validator(mode="after")
    def validate_shift_difference(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must not be equal")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        if v is None:
            return v
        if isinstance(v, bool):
            raise ValueError("punched_at cannot be boolean")
        if not isinstance(v, int):
            raise ValueError("punched_at must be an integer epoch milliseconds")
        if v < 100_000_000_000 or v > 4_102_444_800_000:
            raise ValueError("punched_at out of valid range [100000000000, 4102444800000]")
        return v


class PunchOutIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        if v is None:
            return v
        if isinstance(v, bool):
            raise ValueError("punched_at cannot be boolean")
        if not isinstance(v, int):
            raise ValueError("punched_at must be an integer epoch milliseconds")
        if v < 100_000_000_000 or v > 4_102_444_800_000:
            raise ValueError("punched_at out of valid range [100000000000, 4102444800000]")
        return v

class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_punch_epoch_ms(cls, v):
        if v is None:
            return v
        if isinstance(v, bool):
            raise ValueError("timestamp cannot be boolean")
        if not isinstance(v, int):
            raise ValueError("timestamp must be an integer epoch milliseconds")
        if v < 100_000_000_000 or v > 4_102_444_800_000:
            raise ValueError("timestamp out of valid range [100000000000, 4102444800000]")
        return v

# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    epoch_ms = int(now_utc.timestamp() * 1000)

    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = epoch_ms
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    p = page if isinstance(page, int) else page.default
    ps = page_size if isinstance(page_size, int) else page_size.default
    q = {}
    if department:
        q["department"] = department
    skip = (p - 1) * ps
    total = db.employees.count_documents(q)
    docs = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(ps))
    for d in docs:
        if isinstance(d.get("created_at"), datetime):
            dt = d["created_at"]
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            d["created_at"] = int(dt.timestamp() * 1000)
    return {"items": docs, "total": total, "page": p, "page_size": ps}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts_sec = body.punched_at // 1000
        ts_ms = ts_sec * 1000
        punch_in_utc = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
    else:
        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        ts_ms = int(now_utc.timestamp() * 1000)
        punch_in_utc = now_utc

    d, late_mins = compute_attendance_date_and_late(
        punch_in_utc, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    )

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": punch_in_utc,
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
        raise HTTPException(409, "already punched in for this date")

    doc.pop("_id", None)
    doc["punch_in"] = ts_ms
    return doc


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts_sec = body.punched_at // 1000
        ts_ms = ts_sec * 1000
        punch_out_utc = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
    else:
        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        ts_ms = int(now_utc.timestamp() * 1000)
        punch_out_utc = now_utc

    # Find the employee's most recent attendance record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc, "$ne": None}},
        sort=[("punch_in", -1), ("date", -1)],
    )
    if not record:
        raise HTTPException(404, "no punch-in record found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out for this record")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    if punch_out_utc <= punch_in_utc:
        raise HTTPException(422, "punch_out must be strictly after punch_in")

    if (punch_out_utc - punch_in_utc).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    half_day = work_hours < 4.50
    overtime_minutes = compute_overtime(
        punch_out_utc,
        emp.get("shift_end", "18:30"),
        record["date"],
        emp.get("shift_start", "09:30"),
    )

    # Atomic conditional update ensures concurrency safety
    res = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out for this record")

    updated_doc = db.attendance_logs.find_one({"_id": record["_id"]})
    return format_attendance_record(updated_doc)


VALID_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    p = page if isinstance(page, int) else page.default
    ps = page_size if isinstance(page_size, int) else page_size.default

    if status is not None and status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status: {status}")

    if date_from is not None:
        try:
            datetime.strptime(date_from, "%Y-%m-%d")
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="date_from must be a valid date in YYYY-MM-DD format")

    if date_to is not None:
        try:
            datetime.strptime(date_to, "%Y-%m-%d")
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="date_to must be a valid date in YYYY-MM-DD format")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

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

    skip = (p - 1) * ps
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(ps)
    )
    items = [format_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": p, "page_size": ps}


def calculate_working_days(year: int, month: int, joined_on_str: str) -> int:
    num_days = calendar.monthrange(year, month)[1]
    joined_date = datetime.strptime(joined_on_str, "%Y-%m-%d").date()
    start_date = max(date(year, month, 1), joined_date)
    end_date = date(year, month, num_days)
    if start_date > end_date:
        return 0
    working_days = 0
    cur = start_date
    while cur <= end_date:
        if cur.weekday() < 5:  # Mon=0, Fri=4
            working_days += 1
        cur += timedelta(days=1)
    return working_days


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date_str = f"{month}-01"
    end_date_str = f"{month}-{num_days:02d}"

    working_days = calculate_working_days(year, m, emp["joined_on"])

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date_str, "$lte": end_date_str},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "dayOfWeek": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                    }
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
                                    {"$gte": ["$dayOfWeek", 2]},
                                    {"$lte": ["$dayOfWeek", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
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
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        stats = agg_res[0]
        present_days = float(stats["present_days"])
        leave_days = int(stats["leave_days"])
        late_count = int(stats["late_count"])
        total_late_minutes = int(stats["total_late_minutes"])
        total_overtime_minutes = int(stats["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        pct_dec = (Decimal(str(present_days)) / Decimal(str(working_days)) * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        attendance_pct = float(pct_dec)

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


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start_date = f"{month}-01"
    month_end_date = f"{month}-{num_days:02d}"

    match_stage = {"joined_on": {"$lte": month_end_date}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
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
                                    {"$gte": ["$date", month_start_date]},
                                    {"$lte": ["$date", month_end_date]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "date": 1,
                            "status": 1,
                            "work_hours": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "dayOfWeek": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                                }
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
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$gte": ["$$log.dayOfWeek", 2]},
                                            {"$lte": ["$$log.dayOfWeek", 6]},
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$$log.half_day", True]},
                                            0.5,
                                            1.0,
                                        ]
                                    },
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$gt": ["$$log.late_minutes", 0]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$logs.late_minutes"
                },
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$log.status", "LEAVE"]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$log.status", "ON_DUTY"]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "present_work_hours": {
                    "$filter": {
                        "input": "$logs",
                        "as": "log",
                        "cond": {
                            "$and": [
                                {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$log.work_hours", None]},
                            ]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "work_hours_list": "$present_work_hours.work_hours",
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = []
    for row in db.employees.aggregate(pipeline):
        wh_list = [h for h in row.pop("work_hours_list", []) if h is not None]
        if wh_list:
            d_sum = sum(Decimal(str(h)) for h in wh_list)
            d_count = Decimal(len(wh_list))
            avg_dec = (d_sum / d_count).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            avg_work_hours = float(avg_dec)
        else:
            avg_work_hours = None

        row["avg_work_hours"] = avg_work_hours
        row["present_days"] = float(row["present_days"])
        row["headcount"] = int(row["headcount"])
        row["late_count"] = int(row["late_count"])
        row["total_late_minutes"] = int(row["total_late_minutes"])
        row["leave_count"] = int(row["leave_count"])
        row["on_duty_count"] = int(row["on_duty_count"])
        items.append(row)

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    lim = limit if isinstance(limit, int) else limit.default
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
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
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {
            "$match": {
                "rank": {"$lte": lim}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ])

    items = []
    for row in db.attendance_logs.aggregate(pipeline):
        items.append({
            "rank": int(row["rank"]),
            "emp_code": str(row["emp_code"]),
            "name": str(row["name"]),
            "department": str(row["department"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "late_count": int(row["late_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    try:
        from_dt = datetime.strptime(from_, "%Y-%m-%d")
        to_dt = datetime.strptime(to, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format. Expected YYYY-MM-DD.")

    if to < from_:
        raise HTTPException(status_code=422, detail="'to' date must not be earlier than 'from' date.")

    # Inclusive day count: (to_dt - from_dt).days + 1
    num_days = (to_dt - from_dt).days + 1
    if num_days > 92:
        raise HTTPException(status_code=422, detail="Date range must not exceed 92 days.")

    # 404 if department has no employees
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found or has no employees.")

    # Retrieve all employee join dates for this department to calculate headcount per day
    emp_join_dates = [
        e["joined_on"]
        for e in db.employees.find({"department": department}, {"_id": 0, "joined_on": 1})
    ]

    bounds_end_dt = to_dt + timedelta(days=1)

    # Aggregation with $densify on attendance_logs
    pipeline = [
        {
            "$match": {
                "date": {"$gte": from_, "$lte": to}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {
            "$match": {
                "emp.department": department
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
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
                "_id": 0,
                "date_obj": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$unionWith": {
                "pipeline": [
                    {
                        "$documents": [
                            {"date_obj": from_dt, "present_count": 0.0, "late_count": 0},
                            {"date_obj": to_dt, "present_count": 0.0, "late_count": 0},
                        ]
                    }
                ]
            }
        },
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, bounds_end_dt],
                },
            }
        },
        {
            "$group": {
                "_id": "$date_obj",
                "present_count": {"$max": "$present_count"},
                "late_count": {"$max": "$late_count"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"date": "$_id", "format": "%Y-%m-%d"}},
                "dayOfWeek": {"$dayOfWeek": "$_id"},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
    ]

    raw_days = list(db.attendance_logs.aggregate(pipeline))

    # Calculate headcount, is_working_day, attendance_rate per calendar day
    daily_items = []
    for row in raw_days:
        d_str = row["date"]
        dow = row["dayOfWeek"]  # Sunday=1, Monday=2, ..., Friday=6, Saturday=7
        is_working_day = 2 <= dow <= 6

        # Headcount: department employees joined on or before this day (R9)
        headcount = sum(1 for j in emp_join_dates if j <= d_str)

        present_count = float(row["present_count"])
        late_count = int(row["late_count"])

        # attendance_rate: present_count / headcount (4 decimals half-up) on working days with headcount > 0
        if is_working_day and headcount > 0:
            rate_dec = (Decimal(str(present_count)) / Decimal(str(headcount))).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            attendance_rate = float(rate_dec)
        else:
            attendance_rate = None

        daily_items.append({
            "date": d_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
        })

    # Calculate 7-day moving average over non-null attendance_rate values inside requested range
    final_items = []
    for i, item in enumerate(daily_items):
        window_start = max(0, i - 6)
        window = [
            daily_items[k]["attendance_rate"]
            for k in range(window_start, i + 1)
            if daily_items[k]["attendance_rate"] is not None
        ]
        if window:
            d_sum = sum(Decimal(str(r)) for r in window)
            d_len = Decimal(len(window))
            avg_dec = (d_sum / d_len).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            moving_avg = float(avg_dec)
        else:
            moving_avg = None

        item["moving_avg_7d"] = moving_avg
        final_items.append(item)

    return {"department": department, "items": final_items}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    # Validate emp_code pattern and date format
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format. Expected YYYY-MM-DD.")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found.")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found.")

    # Rule: setting status ABSENT or LEAVE clears both punch times; supplying punch_in or punch_out with it is 422
    target_status = body.status if body.status is not None else record["status"]
    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail=f"Cannot supply punch times when status is {target_status}.")
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence status
        if body.punch_in is not None:
            ts_sec = body.punch_in // 1000
            new_punch_in = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
        else:
            new_punch_in = record.get("punch_in")
            if new_punch_in is not None and new_punch_in.tzinfo is None:
                new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)

        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in timestamp.")

        if body.punch_out is not None:
            ts_out_sec = body.punch_out // 1000
            new_punch_out = datetime.fromtimestamp(ts_out_sec, tz=timezone.utc)
        else:
            new_punch_out = record.get("punch_out")
            if new_punch_out is not None and new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)

        # Validate punch_in stays on the record's attendance date (R1)
        derived_date, _ = compute_attendance_date_and_late(
            new_punch_in, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
        )
        if derived_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in must stay on the record's attendance date {date}; calculated date is {derived_date}.",
            )

        # If punch_out exists, validate punch_out > punch_in and <= 24 hours
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in.")
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in.")

    # Recompute derived fields (R2-R5)
    if target_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        _, new_late_minutes = compute_attendance_date_and_late(
            new_punch_in, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
        )
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_half_day = new_work_hours < 4.50
            new_overtime_minutes = compute_overtime(
                new_punch_out,
                emp.get("shift_end", "18:30"),
                date,
                emp.get("shift_start", "09:30"),
            )
        else:
            new_work_hours = None
            new_half_day = False
            new_overtime_minutes = 0

    # Old values normalization
    old_punch_in = record.get("punch_in")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)

    old_punch_out = record.get("punch_out")
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    old_status = record.get("status")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    # Detect actual changes
    changes = {}
    if target_status != old_status:
        changes["status"] = {"from": old_status, "to": target_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request made no changes to the record.")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic compare-and-swap update: match by _id and current history length
    # to ensure concurrent edits cannot silently overwrite each other
    current_history_len = len(record.get("history") or [])
    query = {
        "_id": record["_id"],
        "$or": [
            {"history": {"$size": current_history_len}},
            {"history": None},
            {"history": {"$exists": False}},
        ] if current_history_len == 0 else [{"history": {"$size": current_history_len}}],
    }

    update_doc = {
        "$set": {
            "status": target_status,
            "punch_in": new_punch_in,
            "punch_out": new_punch_out,
            "work_hours": new_work_hours,
            "late_minutes": new_late_minutes,
            "overtime_minutes": new_overtime_minutes,
            "half_day": new_half_day,
        },
        "$push": {
            "history": history_entry,
        },
    }

    res = db.attendance_logs.update_one(query, update_doc)
    if res.matched_count == 0:
        raise HTTPException(status_code=409, detail="Concurrent modification detected. Please retry.")

    updated_record = db.attendance_logs.find_one({"_id": record["_id"]})
    return format_attendance_record(updated_record)


# --------------------------------------------------------------------------- #
# C12: GET /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
EXPLAIN_ENDPOINTS = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}


def _sanitize_for_json(obj):
    """Recursively convert non-JSON-serializable BSON types to strings."""
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, datetime):
        return obj.isoformat()
    # bson.Timestamp, bson.ObjectId, Decimal128, etc.
    try:
        import json
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint not in EXPLAIN_ENDPOINTS:
        raise HTTPException(status_code=422, detail=f"Invalid endpoint: {endpoint}. Must be one of: {', '.join(sorted(EXPLAIN_ENDPOINTS))}")

    # ---- attendance_list ---- #
    if endpoint == "attendance_list":
        if status is not None and status not in VALID_STATUSES:
            raise HTTPException(status_code=422, detail=f"Invalid status: {status}")
        if date_from is not None:
            try:
                datetime.strptime(date_from, "%Y-%m-%d")
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail="date_from must be a valid date in YYYY-MM-DD format")
        if date_to is not None:
            try:
                datetime.strptime(date_to, "%Y-%m-%d")
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail="date_to must be a valid date in YYYY-MM-DD format")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

        q: dict = {}
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

        p = page if isinstance(page, int) else page.default
        ps = page_size if isinstance(page_size, int) else page_size.default
        skip = (p - 1) * ps

        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": ps,
        }
        explain_result = db.command("explain", cmd, verbosity="executionStats")
        collection = "attendance_logs"

    # ---- employee_monthly ---- #
    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month or not __import__("re").match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=422, detail="month is required and must match YYYY-MM format")

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date_str = f"{month}-01"
        end_date_str = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_date_str, "$lte": end_date_str},
                }
            },
            {
                "$project": {
                    "date": 1,
                    "status": 1,
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "dayOfWeek": {
                        "$dayOfWeek": {
                            "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                        }
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
                                        {"$gte": ["$dayOfWeek", 2]},
                                        {"$lte": ["$dayOfWeek", 6]},
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]

        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        explain_result = db.command("explain", cmd, verbosity="executionStats")
        collection = "attendance_logs"

    # ---- department_summary ---- #
    elif endpoint == "department_summary":
        if not month or not __import__("re").match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=422, detail="month is required and must match YYYY-MM format")

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        month_start_date = f"{month}-01"
        month_end_date = f"{month}-{num_days:02d}"

        match_stage: dict = {"joined_on": {"$lte": month_end_date}}
        if department:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
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
                                        {"$gte": ["$date", month_start_date]},
                                        {"$lte": ["$date", month_end_date]},
                                    ]
                                }
                            }
                        },
                        {
                            "$project": {
                                "date": 1,
                                "status": 1,
                                "work_hours": 1,
                                "half_day": {"$ifNull": ["$half_day", False]},
                                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                                "dayOfWeek": {
                                    "$dayOfWeek": {
                                        "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                                    }
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
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "log",
                                "in": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$gte": ["$$log.dayOfWeek", 2]},
                                                {"$lte": ["$$log.dayOfWeek", 6]},
                                            ]
                                        },
                                        {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            }
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "log",
                                "in": {"$cond": [{"$gt": ["$$log.late_minutes", 0]}, 1, 0]},
                            }
                        }
                    },
                    "total_late_minutes": {"$sum": "$logs.late_minutes"},
                    "leave_count": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "log",
                                "in": {"$cond": [{"$eq": ["$$log.status", "LEAVE"]}, 1, 0]},
                            }
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$map": {
                                "input": "$logs",
                                "as": "log",
                                "in": {"$cond": [{"$eq": ["$$log.status", "ON_DUTY"]}, 1, 0]},
                            }
                        }
                    },
                    "present_work_hours": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$and": [
                                    {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$$log.work_hours", None]},
                                ]
                            },
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": 1,
                    "headcount": 1,
                    "present_days": 1,
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                    "work_hours_list": "$present_work_hours.work_hours",
                }
            },
            {"$sort": {"department": 1}},
        ]

        cmd = {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}
        explain_result = db.command("explain", cmd, verbosity="executionStats")
        collection = "employees"

    # ---- late_leaderboard ---- #
    elif endpoint == "late_leaderboard":
        if not month or not __import__("re").match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
            raise HTTPException(status_code=422, detail="month is required and must match YYYY-MM format")

        lim = limit if isinstance(limit, int) else limit.default
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lte": end_date},
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
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}}
                    },
                }
            },
            {
                "$match": {
                    "rank": {"$lte": lim}
                }
            },
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1,
                }
            },
        ])

        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        explain_result = db.command("explain", cmd, verbosity="executionStats")
        collection = "attendance_logs"

    # ---- department_trend ---- #
    else:  # endpoint == "department_trend"
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_ or not to:
            raise HTTPException(status_code=422, detail="'from' and 'to' are required for department_trend")
        try:
            from_dt = datetime.strptime(from_, "%Y-%m-%d")
            to_dt = datetime.strptime(to, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format. Expected YYYY-MM-DD.")

        if to < from_:
            raise HTTPException(status_code=422, detail="'to' date must not be earlier than 'from' date.")
        num_days = (to_dt - from_dt).days + 1
        if num_days > 92:
            raise HTTPException(status_code=422, detail="Date range must not exceed 92 days.")

        bounds_end_dt = to_dt + timedelta(days=1)

        pipeline = [
            {"$match": {"date": {"$gte": from_, "$lte": to}}},
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
                "$project": {
                    "date": 1,
                    "status": 1,
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
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
                            "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date_obj": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                    "present_count": 1,
                    "late_count": 1,
                }
            },
            {
                "$unionWith": {
                    "pipeline": [
                        {
                            "$documents": [
                                {"date_obj": from_dt, "present_count": 0.0, "late_count": 0},
                                {"date_obj": to_dt, "present_count": 0.0, "late_count": 0},
                            ]
                        }
                    ]
                }
            },
            {
                "$densify": {
                    "field": "date_obj",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [from_dt, bounds_end_dt],
                    },
                }
            },
            {
                "$group": {
                    "_id": "$date_obj",
                    "present_count": {"$max": "$present_count"},
                    "late_count": {"$max": "$late_count"},
                }
            },
            {"$sort": {"_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "date": {"$dateToString": {"date": "$_id", "format": "%Y-%m-%d"}},
                    "dayOfWeek": {"$dayOfWeek": "$_id"},
                    "present_count": {"$ifNull": ["$present_count", 0.0]},
                    "late_count": {"$ifNull": ["$late_count", 0]},
                }
            },
        ]

        cmd = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
        explain_result = db.command("explain", cmd, verbosity="executionStats")
        collection = "attendance_logs"

    # Strip server-internal fields and sanitize BSON types for JSON
    for key in ("$clusterTime", "operationTime", "serverInfo", "serverParameters", "ok"):
        explain_result.pop(key, None)

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": _sanitize_for_json(explain_result),
    }
