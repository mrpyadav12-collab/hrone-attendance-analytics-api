"""Employee Attendance & Analytics API.

Run with ``uvicorn app.main:app --port 8000``. Configure ``MONGO_URI`` and
``MONGO_DB`` through environment variables or a local ``.env`` file.
"""
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_type, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from dotenv import load_dotenv
from bson.decimal128 import Decimal128
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
MonthString = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        pass
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)
indexes_ready = False


def ensure_indexes() -> None:
    global indexes_ready
    if indexes_ready:
        return
    db.employees.create_index("emp_code", unique=True, name="employee_code_unique")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="department_joined_on")
    db.employees.create_index("joined_on", name="employee_joined_on")
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name="attendance_employee_date_unique"
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)], name="attendance_date_employee"
    )
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1), ("emp_code", 1)], name="attendance_status_date_employee"
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)], name="attendance_employee_punch_in"
    )
    indexes_ready = True


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: Optional[str] = None) -> int:
    punch_in = as_utc(punch_in).astimezone(IST).replace(microsecond=0)
    shift_day = date_type.fromisoformat(date_str) if date_str else punch_in.date()
    start = datetime.combine(shift_day, parse_time(shift_start), tzinfo=IST)
    elapsed_seconds = int((punch_in - start).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    punch_in = as_utc(punch_in).replace(microsecond=0)
    punch_out = as_utc(punch_out).replace(microsecond=0)
    seconds = int((punch_out - punch_in).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    punch_out = as_utc(punch_out).astimezone(IST).replace(microsecond=0)
    shift_day = date_type.fromisoformat(date_str)
    end_day = shift_day + timedelta(days=shift_end <= shift_start)
    end = datetime.combine(end_day, parse_time(shift_end), tzinfo=IST)
    elapsed_seconds = int((punch_out - end).total_seconds())
    overtime_minutes = elapsed_seconds // 60
    return overtime_minutes if overtime_minutes >= 30 else 0


def parse_time(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def from_epoch_millis(value: Optional[int]) -> datetime:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0)
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def to_epoch_millis(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(as_utc(value).timestamp()) * 1000


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = as_utc(punch_in).astimezone(IST).replace(microsecond=0)
    shift_day = local.date()
    if shift_end <= shift_start and local.time().replace(tzinfo=None) < parse_time(shift_end):
        shift_day -= timedelta(days=1)
    return shift_day.isoformat()


def serialize_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for entry in history or []:
        changes = {}
        for key, change in entry.get("changes", {}).items():
            changes[key] = {
                "from": to_epoch_millis(change["from"]) if isinstance(change.get("from"), datetime) else change.get("from"),
                "to": to_epoch_millis(change["to"]) if isinstance(change.get("to"), datetime) else change.get("to"),
            }
        output.append({
            "at": to_epoch_millis(entry.get("at")),
            "by": entry.get("by"),
            "reason": entry.get("reason"),
            "changes": changes,
        })
    return output


def serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_millis(doc.get("punch_in")),
        "punch_out": to_epoch_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": serialize_history(doc.get("history", [])),
    }


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_millis(doc["created_at"]),
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "09:30"
    shift_end: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        if date_type.fromisoformat(value).isoformat() != value:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[EpochMillis] = None

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def reject_null_timestamps(self):
        nullable_fields = ("status", "punch_in", "punch_out")
        if any(name in self.model_fields_set and getattr(self, name) is None for name in nullable_fields):
            raise ValueError("status and punch timestamps cannot be null")
        return self


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        ensure_indexes()
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    items = [
        serialize_employee(doc)
        for doc in db.employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = from_epoch_millis(body.punched_at)
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = from_epoch_millis(body.punched_at)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    punch_in_at = as_utc(record["punch_in"])
    elapsed = (ts - punch_in_at).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    work_hours = compute_work_hours(punch_in_at, ts)
    overtime = compute_overtime(ts, emp["shift_end"], record["date"], emp["shift_start"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": overtime,
            "half_day": work_hours < 4.50,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record is already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date_type] = None,
    date_to: Optional[date_type] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    items = [
        serialize_attendance(doc)
        for doc in db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: date_type, body: RegularizeIn):
    date_value = date.isoformat()
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_value})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    status = body.status or record["status"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE records cannot have punch times")
        punch_in_at = None
        punch_out_at = None
    else:
        punch_in_at = from_epoch_millis(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        punch_out_at = from_epoch_millis(body.punch_out) if body.punch_out is not None else record.get("punch_out")
        if punch_in_at is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_date(punch_in_at, emp["shift_start"], emp["shift_end"]) != date_value:
            raise HTTPException(422, "punch_in must belong to the attendance date")
        if punch_out_at is not None:
            elapsed = (as_utc(punch_out_at) - as_utc(punch_in_at)).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 60 * 60:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    late_minutes = compute_late_minutes(punch_in_at, emp["shift_start"], date_value) if punch_in_at else 0
    if punch_out_at is not None:
        work_hours = compute_work_hours(punch_in_at, punch_out_at)
        overtime_minutes = compute_overtime(punch_out_at, emp["shift_end"], date_value, emp["shift_start"])
        half_day = work_hours < 4.50
    else:
        work_hours = None
        overtime_minutes = 0
        half_day = False

    new_values = {
        "status": status,
        "punch_in": punch_in_at,
        "punch_out": punch_out_at,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }
    old_values = {
        "status": record.get("status"),
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    changes = {
        key: {"from": old_values[key], "to": value}
        for key, value in new_values.items()
        if old_values[key] != value
    }
    if not changes:
        raise HTTPException(422, "regularization does not change the record")

    history_filter = {"history": record["history"]} if "history" in record else {"history": {"$exists": False}}
    update_filter = {
        "_id": record["_id"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        **history_filter,
    }
    history_entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by,
                     "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        {"$set": new_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record changed concurrently")
    return serialize_attendance(updated)


# --------------------------------------------------------------------------- #
def month_bounds(month: str) -> tuple[str, str]:
    try:
        year, month_number = map(int, month.split("-"))
        first = date_type(year, month_number, 1)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "month must be a valid YYYY-MM") from exc
    if month_number == 12:
        last = date_type(year, 12, 31)
    else:
        last = date_type(year, month_number + 1, 1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def weekday_expression(date_expression: str) -> dict[str, Any]:
    return {
        "$in": [
            {"$dayOfWeek": {"$dateFromString": {"dateString": date_expression}}},
            [2, 3, 4, 5, 6],
        ]
    }


def round_half_up_expression(expression: Any, digits: int) -> dict[str, Any]:
    scale = Decimal128(str(10 ** digits))
    return {
        "$divide": [{"$floor": {"$add": [{"$multiply": [{"$toDecimal": expression}, scale]}, Decimal128("0.5")]}}, scale]
    }


def month_log_lookup(start: str, end: str) -> dict[str, Any]:
    return {
        "$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", start]},
                    {"$lte": ["$date", end]},
                ]}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "work_hours": 1,
                              "late_minutes": 1, "overtime_minutes": 1, "half_day": 1}},
            ],
            "as": "logs",
        }
    }


def working_days_expression(start: str, end: str) -> dict[str, Any]:
    month_start = {"$dateFromString": {"dateString": start}}
    month_end = {"$dateFromString": {"dateString": end}}
    joined_date = {"$dateFromString": {"dateString": "$joined_on"}}
    return {
        "$cond": [
            {"$gt": ["$joined_on", end]},
            0,
            {"$let": {
                "vars": {"first_day": {"$cond": [{"$gt": ["$joined_on", start]}, joined_date, month_start]}},
                "in": {"$size": {"$filter": {
                    "input": {"$map": {
                        "input": {"$range": [0, {"$add": [
                            {"$dateDiff": {"startDate": "$$first_day", "endDate": month_end, "unit": "day"}}, 1
                        ]}]},
                        "as": "offset",
                        "in": {"$dateAdd": {"startDate": "$$first_day", "unit": "day", "amount": "$$offset"}},
                    }},
                    "as": "day",
                    "cond": {"$in": [{"$dayOfWeek": "$$day"}, [2, 3, 4, 5, 6]]},
                }}}
            }},
        ]
    }


def log_metric_stages() -> list[dict[str, Any]]:
    return [
        {"$set": {
            "_present_logs": {"$filter": {"input": "$logs", "as": "log", "cond": {"$and": [
                {"$in": ["$$log.status", list(PRESENCE_STATUSES)]}, weekday_expression("$$log.date")
            ]}}},
            "_leave_logs": {"$filter": {"input": "$logs", "as": "log", "cond": {"$eq": ["$$log.status", "LEAVE"]}}},
            "_late_logs": {"$filter": {"input": "$logs", "as": "log", "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}},
            "_work_logs": {"$filter": {"input": "$logs", "as": "log", "cond": {"$and": [
                {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
                {"$ne": ["$$log.work_hours", None]},
            ]}}},
            "_duty_logs": {"$filter": {"input": "$logs", "as": "log", "cond": {"$eq": ["$$log.status", "ON_DUTY"]}}},
        }},
        {"$set": {
            "present_days": {"$sum": {"$map": {"input": "$_present_logs", "as": "log", "in": {
                "$cond": [{"$ifNull": ["$$log.half_day", False]}, Decimal128("0.5"), Decimal128("1.0")]
            }}}},
            "leave_days": {"$size": "$_leave_logs"},
            "late_count": {"$size": "$_late_logs"},
            "total_late_minutes": {"$sum": {"$map": {"input": "$_late_logs", "as": "log", "in": {"$ifNull": ["$$log.late_minutes", 0]}}}},
            "total_overtime_minutes": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$ifNull": ["$$log.overtime_minutes", 0]}}}},
            "_work_hours_sum": {"$sum": {"$map": {"input": "$_work_logs", "as": "log", "in": {
                "$toDecimal": {"$toString": "$$log.work_hours"}
            }}}},
            "_work_hours_count": {"$size": "$_work_logs"},
            "on_duty_count": {"$size": "$_duty_logs"},
        }},
    ]


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code}},
        month_log_lookup(start, end),
        {"$set": {"working_days": working_days_expression(start, end)}},
        *log_metric_stages(),
        {"$set": {"attendance_pct": {"$cond": [
            {"$eq": ["$working_days", 0]}, None,
            {"$toDouble": round_half_up_expression({"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]}, 4)},
        ]}}},
        {"$project": {"_id": 0, "emp_code": 1, "working_days": 1, "present_days": {"$toDouble": "$present_days"},
                      "leave_days": 1, "late_count": 1, "total_late_minutes": 1,
                      "total_overtime_minutes": 1, "attendance_pct": 1}},
    ]


def department_summary_pipeline(month: str, department: Optional[str] = None) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    employee_filter: dict[str, Any] = {"joined_on": {"$lte": end}}
    if department is not None:
        employee_filter["department"] = department
    return [
        {"$match": employee_filter},
        month_log_lookup(start, end),
        *log_metric_stages(),
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_days"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "work_hours_sum": {"$sum": "$_work_hours_sum"},
            "work_hours_count": {"$sum": "$_work_hours_count"},
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "avg_work_hours": {"$cond": [
                {"$eq": ["$work_hours_count", 0]}, None,
                {"$toDouble": round_half_up_expression({"$divide": ["$work_hours_sum", "$work_hours_count"]}, 2)},
            ]},
            "present_days": {"$toDouble": "$present_days"},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None) -> list[dict[str, Any]]:
    start, end = month_bounds(month)
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1,
                      "total_late_minutes": 1, "late_count": 1}},
    ])
    return pipeline


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthString):
    if db.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(404, "employee not found")
    results = list(db.employees.aggregate(employee_monthly_pipeline(emp_code, month)))
    result = results[0]
    result["month"] = month
    return result


@app.get("/analytics/departments/summary")
def department_summary(month: MonthString, department: Optional[str] = None):
    items = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthString,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


def department_trend_pipeline(department: str, from_date: date_type, to_date: date_type) -> list[dict[str, Any]]:
    first_day = datetime.combine(from_date, time.min, tzinfo=UTC)
    if to_date == date_type.max:
        end_exclusive = datetime.combine(to_date, time.max, tzinfo=UTC)
    else:
        end_exclusive = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=UTC)
    return [
        {"$match": {"date": {"$gte": from_date.isoformat(), "$lte": to_date.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$project": {
            "_id": 0,
            "day": {"$dateFromString": {"dateString": "$date"}},
            "present_count": {"$cond": [
                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, Decimal128("0.5"), Decimal128("1.0")]},
                Decimal128("0.0"),
            ]},
            "late_count": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]},
        }},
        {"$unionWith": {"pipeline": [{"$documents": [{
            "day": first_day, "present_count": 0, "late_count": 0
        }]}]}},
        {"$group": {
            "_id": "$day",
            "present_count": {"$sum": "$present_count"},
            "late_count": {"$sum": "$late_count"},
        }},
        {"$densify": {"field": "_id", "range": {
            "step": 1, "unit": "day", "bounds": [first_day, end_exclusive]
        }}},
        {"$set": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"trend_day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}}},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$trend_day"]}}},
                {"$count": "headcount"},
            ],
            "as": "headcount_result",
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_result.headcount", 0]}, 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$_id"}, [2, 3, 4, 5, 6]]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            round_half_up_expression({"$divide": ["$present_count", "$headcount"]}, 4),
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"_id": 1},
            "output": {"moving_avg_7d": {
                "$avg": "$attendance_rate",
                "window": {"documents": [-6, 0]},
            }},
        }},
        {"$set": {"moving_avg_7d": {"$cond": [
            {"$eq": ["$moving_avg_7d", None]}, None,
            round_half_up_expression("$moving_avg_7d", 4),
        ]}}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": {"$toDouble": "$present_count"},
            "late_count": 1,
            "attendance_rate": {"$toDouble": "$attendance_rate"},
            "moving_avg_7d": {"$toDouble": "$moving_avg_7d"},
        }},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: Annotated[date_type, Query(alias="from")],
    to_date: Annotated[date_type, Query(alias="to")],
):
    if to_date < from_date or (to_date - from_date).days + 1 > 92:
        raise HTTPException(422, "date range must be ordered and no longer than 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    items = list(db.attendance_logs.aggregate(department_trend_pipeline(department, from_date, to_date)))
    return {"department": department, "items": items}


def attendance_filter(
    emp_code: Optional[str],
    date_from: Optional[date_type],
    date_to: Optional[date_type],
    status: Optional[str],
) -> dict[str, Any]:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to:
            query["date"]["$lte"] = date_to.isoformat()
    if status:
        query["status"] = status
    return query


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[date_type] = None,
    date_to: Optional[date_type] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[date_type] = Query(default=None, alias="from"),
    to_date: Optional[date_type] = Query(default=None, alias="to"),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if endpoint == "attendance_list":
        query = attendance_filter(emp_code, date_from, date_to, status)
        command = {"explain": {"find": "attendance_logs", "filter": query,
                              "sort": {"date": -1, "emp_code": 1},
                              "skip": (page - 1) * page_size, "limit": page_size},
                   "verbosity": "executionStats"}
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(422, "month must be YYYY-MM")
        pipeline = employee_monthly_pipeline(emp_code, month)
        collection = "employees"
        command = {"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
                   "verbosity": "executionStats"}
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(422, "month must be YYYY-MM")
        pipeline = department_summary_pipeline(month, department)
        collection = "employees"
        command = {"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
                   "verbosity": "executionStats"}
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(422, "month must be YYYY-MM")
        pipeline = late_leaderboard_pipeline(month, limit, department)
        collection = "attendance_logs"
        command = {"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
                   "verbosity": "executionStats"}
    else:
        if not department or from_date is None or to_date is None:
            raise HTTPException(422, "department, from and to are required")
        if to_date < from_date or (to_date - from_date).days + 1 > 92:
            raise HTTPException(422, "date range must be ordered and no longer than 92 days")
        pipeline = department_trend_pipeline(department, from_date, to_date)
        collection = "attendance_logs"
        command = {"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
                   "verbosity": "executionStats"}
    explain = db.command(command)
    from bson import json_util
    from bson.json_util import JSONMode, JSONOptions
    json_explain = json.loads(json_util.dumps(
        explain, json_options=JSONOptions(json_mode=JSONMode.RELAXED)
    ))
    return {"endpoint": endpoint, "collection": collection, "explain": json_explain}
# --------------------------------------------------------------------------- #
