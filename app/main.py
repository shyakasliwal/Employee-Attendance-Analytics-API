"""Employee Attendance & Analytics API, contract v2.0."""
import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure
from bson import json_util
import json

load_dotenv(override=False)
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=1500)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
logs = db.attendance_logs
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def ensure_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_status_employee")
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_employee_punchin")


@app.on_event("startup")
def startup():
    ensure_indexes()


def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def truncate_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def from_ms(value: int) -> datetime:
    validate_ms(value)
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def validate_ms(value):
    if type(value) is not int or value < MIN_MS or value > MAX_MS:
        raise ValueError("must be an integer epoch-millisecond timestamp in the supported range")


def to_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def output_doc(doc):
    doc = dict(doc)
    doc.pop("_id", None)
    for key in ("created_at", "punch_in", "punch_out"):
        if key in doc:
            doc[key] = to_ms(doc[key])
    if "date" in doc and "emp_code" in doc:
        doc["history"] = doc.get("history") or []
        for entry in doc["history"]:
            if "at" in entry: entry["at"] = to_ms(entry["at"])
            for change in (entry.get("changes") or {}).values():
                for side in ("from", "to"):
                    if isinstance(change.get(side), datetime): change[side] = to_ms(change[side])
        doc.setdefault("half_day", False)
        doc.setdefault("late_minutes", 0)
        doc.setdefault("overtime_minutes", 0)
        doc.setdefault("work_hours", None)
    return doc


def round_half_up(value, places=2):
    if value is None: return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def parse_date(value: str) -> date:
    try:
        d = date.fromisoformat(value)
        if d.isoformat() != value: raise ValueError
        return d
    except Exception:
        raise ValueError("date must be YYYY-MM-DD")


def parse_month(month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise ValueError("month must be YYYY-MM")
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y, m, calendar.monthrange(y, m)[1])
    return start, end


def shift_dt(day: date, shift: str) -> datetime:
    h, m = map(int, shift.split(":"))
    return datetime.combine(day, time(h, m), IST).astimezone(UTC).replace(microsecond=0)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = punch_in.astimezone(IST)
    start_hm = local.strftime("%H:%M")
    if shift_end <= shift_start and start_hm < shift_end:
        local = local - timedelta(days=1)
    return local.date().isoformat()


def late_minutes(punch_in: datetime, shift_start: str, attendance_day: str) -> int:
    local = punch_in.astimezone(IST)
    day = parse_date(attendance_day)
    start = shift_dt(day, shift_start)
    delta = (punch_in - start).total_seconds()
    return int(delta // 60) if delta > 600 else 0


def overtime_minutes(punch_out: datetime, shift_start: str, shift_end: str, attendance_day: str) -> int:
    day = parse_date(attendance_day)
    end_day = day + timedelta(days=1) if shift_end <= shift_start else day
    end = shift_dt(end_day, shift_end)
    mins = int((punch_out - end).total_seconds() // 60)
    return mins if mins >= 30 else 0


def derive_fields(emp, day, punch_in, punch_out):
    if punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = late_minutes(punch_in, emp["shift_start"], day)
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    seconds = int((punch_out - punch_in).total_seconds())
    hours = round_half_up(Decimal(seconds) / Decimal(3600), 2)
    return {"late_minutes": late, "work_hours": hours,
            "overtime_minutes": overtime_minutes(punch_out, emp["shift_start"], emp["shift_end"], day),
            "half_day": hours < 4.50}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def valid_joined(cls, v): parse_date(v); return v
    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None: validate_ms(v)
        return v


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)
    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None: validate_ms(v)
        return v


class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = Field(default=None, strict=True)
    punch_out: Optional[int] = Field(default=None, strict=True)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, v):
        if v is not None: validate_ms(v)
        return v


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try: employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return output_doc(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    cur = employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)
    return {"items": [output_doc(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "unknown employee")
    ts = truncate_dt(from_ms(body.punched_at) if body.punched_at is not None else now_utc())
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts,
           "punch_out": None, "work_hours": None,
           "late_minutes": late_minutes(ts, emp["shift_start"], day), "overtime_minutes": 0,
           "half_day": False, "history": []}
    try: logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "already punched in for this date")
    return output_doc(doc)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if status is not None: q["status"] = status
    try:
        if date_from: parse_date(date_from)
        if date_to: parse_date(date_to)
    except ValueError as e: raise HTTPException(422, str(e))
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    total = logs.count_documents(q)
    cur = logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items": [output_doc(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "unknown employee")
    ts = truncate_dt(from_ms(body.punched_at) if body.punched_at is not None else now_utc())
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not rec: raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None: raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"] or (ts-rec["punch_in"]).total_seconds() > 86400:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    derived = derive_fields(emp, rec["date"], rec["punch_in"], ts)
    updated = logs.find_one_and_update({"_id": rec["_id"], "punch_out": None}, {"$set": {"punch_out": ts, **derived}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "already punched out")
    return output_doc(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    try: parse_date(date)
    except ValueError as e: raise HTTPException(422, str(e))
    emp = employees.find_one({"emp_code": emp_code})
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if not emp or not rec: raise HTTPException(404, "employee or attendance record not found")
    original = dict(rec)
    status = body.status if body.status is not None else rec["status"]
    if status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
        pi = po = None
    else:
        pi = from_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        po = from_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if pi is None: raise HTTPException(422, "presence status requires punch_in")
        if attendance_date(pi, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must belong to the attendance date")
        if po is not None and (po <= pi or (po-pi).total_seconds() > 86400):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    derived = derive_fields(emp, date, pi, po)
    desired = {"status": status, "punch_in": pi, "punch_out": po, **derived}
    fields = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes = {}
    for f in fields:
        old = original.get(f, False if f == "half_day" else 0 if f in ("late_minutes", "overtime_minutes") else None)
        new = desired[f]
        if old != new: changes[f] = {"from": old, "to": new}
    if not changes: raise HTTPException(422, "request does not change the record")
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Atomic compare-and-set protects history from lost concurrent updates.
    selector = {"_id": rec["_id"], "history": original.get("history", [])}
    if "history" not in original: selector["history"] = {"$exists": False}
    updated = logs.find_one_and_update(selector, {"$set": desired, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "record changed concurrently; retry")
    return output_doc(updated)


def month_match(month):
    start, end = parse_month(month)
    return {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}, start, end


def monthly_pipeline(emp_code, month):
    q, start, end = month_match(month)
    q["emp_code"] = emp_code
    working = sum(1 for i in range((end-start).days+1) if (start+timedelta(days=i)).weekday() < 5 and start+timedelta(days=i) >= date.fromisoformat("0001-01-01"))
    # joined_on adjustment is applied in pipeline after employee lookup.
    return [ {"$match": q}, {"$set": {"_half": {"$ifNull": ["$half_day", False]}, "_late": {"$ifNull": ["$late_minutes", 0]}, "_ot": {"$ifNull": ["$overtime_minutes", 0]}}},
      {"$group": {"_id": None,
       "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENT)]}, {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]}, {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 1]}]}, {"$cond": ["$_half", 0.5, 1]}, 0]}},
       "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
       "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}}, "total_late_minutes": {"$sum": "$_late"}, "total_overtime_minutes": {"$sum": "$_ot"}}} ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    try: start, end = parse_month(month)
    except ValueError as e: raise HTTPException(422, str(e))
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if not emp: raise HTTPException(404, "unknown employee")
    joined = max(start, date.fromisoformat(emp["joined_on"]))
    working_days = sum(1 for n in range(max(0, (end-joined).days+1)) if (joined+timedelta(days=n)).weekday() < 5)
    result = list(logs.aggregate(monthly_pipeline(emp_code, month)))
    r = result[0] if result else {}
    present = round_half_up(r.get("present_days", 0), 2)
    pct = round_half_up(present / working_days * 100, 4) if working_days else None
    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": present,
            "leave_days": r.get("leave_days", 0), "late_count": r.get("late_count", 0),
            "total_late_minutes": r.get("total_late_minutes", 0), "total_overtime_minutes": r.get("total_overtime_minutes", 0), "attendance_pct": pct}


def department_summary_pipeline(month, department=None):
    q, start, end = month_match(month)
    pipeline = [
      {"$match": {"joined_on": {"$lte": end.isoformat()}, **({"department": department} if department is not None else {})}},
      {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]}]}}}], "as": "month_logs"}},
      {"$unwind": {"path": "$month_logs", "preserveNullAndEmptyArrays": True}},
      {"$group": {"_id": "$department", "headcount_codes": {"$addToSet": "$emp_code"},
        "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$month_logs.status", list(PRESENT)]}, {"$ne": ["$month_logs", None]}, {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$month_logs.date"}}}, 7]}, {"$gt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$month_logs.date"}}}, 1]}]}, {"$cond": [{"$ifNull": ["$month_logs.half_day", False]}, 0.5, 1]}, 0]}},
        "avg_work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$month_logs.status", list(PRESENT)]}, {"$ne": ["$month_logs.work_hours", None]}]}, "$month_logs.work_hours", None]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$month_logs.late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$month_logs.late_minutes", 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$month_logs.status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$month_logs.status", "ON_DUTY"]}, 1, 0]}}}},
      {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount_codes"}, "present_days": 1,
        "avg_work_hours": {"$ifNull": ["$avg_work_hours", None]},
        "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
      {"$match": {"headcount": {"$gt": 0}}}, {"$sort": {"department": 1}}]
    return pipeline


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    try: parse_month(month)
    except ValueError as e: raise HTTPException(422, str(e))
    items = list(employees.aggregate(department_summary_pipeline(month, department)))
    for x in items:
        x["present_days"] = round_half_up(x.get("present_days", 0), 2)
        x["avg_work_hours"] = round_half_up(x.get("avg_work_hours"), 2)
    return {"month": month, "items": items}


def leaderboard_pipeline(month, department=None, limit=10):
    q, start, end = month_match(month)
    pipeline = [{"$match": {**q, "late_minutes": {"$gt": 0}}},
      {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": 1}}},
      {"$match": {"total_late_minutes": {"$gt": 0}}},
      {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
      {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
      {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    try: parse_month(month)
    except ValueError as e: raise HTTPException(422, str(e))
    return {"month": month, "items": list(logs.aggregate(leaderboard_pipeline(month, department, limit)))}


def trend_pipeline(department, start, end):
    # Begin with a department employee so $densify still emits dates when there are no logs.
    start_dt = datetime.combine(start, time.min)
    end_dt = datetime.combine(end + timedelta(days=1), time.min)
    return [
      {"$match": {"department": department}}, {"$limit": 1},
      {"$project": {"_id": 0, "date": {"$literal": start_dt}}},
      {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]}}},
      {"$set": {"date_str": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d", "timezone": "UTC"}}}},
      {"$lookup": {"from": "employees", "let": {"d": "$date_str"}, "pipeline": [
          {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}}, {"$count": "n"}], "as": "hc"}},
      {"$lookup": {"from": "attendance_logs", "let": {"d": "$date_str"}, "pipeline": [
          {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
          {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
          {"$unwind": "$employee"}, {"$match": {"employee.department": department}},
          {"$group": {"_id": None,
             "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT)]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
             "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}
        ], "as": "daily"}},
      {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                 "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
                 "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]},
                 "is_working_day": {"$not": [{"$in": [{"$dayOfWeek": "$date"}, [1, 7]]}]}}},
      {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]}, None]}}},
      {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
      {"$project": {"_id": 0, "date": "$date_str", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
      {"$sort": {"date": 1}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    try: start, end = parse_date(from_), parse_date(to)
    except ValueError as e: raise HTTPException(422, str(e))
    if end < start: raise HTTPException(422, "to must be >= from")
    if (end-start).days+1 > 92: raise HTTPException(422, "range must not exceed 92 days")
    if not employees.find_one({"department": department}, {"_id": 1}): raise HTTPException(404, "unknown department")
    items = list(employees.aggregate(trend_pipeline(department, start, end)))
    # Keep rounding half-up on API numbers.
    for item in items:
        item["present_count"] = round_half_up(item.get("present_count", 0), 2)
        if item.get("attendance_rate") is not None: item["attendance_rate"] = round_half_up(item["attendance_rate"], 4)
        if item.get("moving_avg_7d") is not None: item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], 4)
    return {"department": department, "items": items}


def json_safe_explain(value):
    return json.loads(json_util.dumps(value))


def explain_command(collection, pipeline=None, query=None, sort=None, skip=0, limit=None):
    if pipeline is not None:
        return client[db.name].command({"explain": {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"})
    find = {"find": collection.name, "filter": query or {}}
    if sort: find["sort"] = {k: v for k, v in sort}
    if skip: find["skip"] = skip
    if limit: find["limit"] = limit
    return client[db.name].command({"explain": find, "verbosity": "executionStats"})


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                     department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                     date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
                     from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    try:
        if endpoint == "attendance_list":
            q = {}
            if emp_code is not None: q["emp_code"] = emp_code
            if status is not None: q["status"] = status
            if date_from: parse_date(date_from); q.setdefault("date", {})["$gte"] = date_from
            if date_to: parse_date(date_to); q.setdefault("date", {})["$lte"] = date_to
            if date_from and date_to and date_from > date_to: raise ValueError("date_from must be <= date_to")
            exp = explain_command(logs, query=q, sort=[("date", -1), ("emp_code", 1)], skip=(page-1)*page_size, limit=page_size)
            coll = "attendance_logs"
        elif endpoint == "employee_monthly":
            if not emp_code or not month: raise ValueError("emp_code and month are required")
            parse_month(month); exp = explain_command(logs, pipeline=monthly_pipeline(emp_code, month)); coll = "attendance_logs"
        elif endpoint == "department_summary":
            if not month: raise ValueError("month is required")
            parse_month(month); exp = explain_command(employees, pipeline=department_summary_pipeline(month, department)); coll = "employees"
        elif endpoint == "late_leaderboard":
            if not month: raise ValueError("month is required")
            parse_month(month); exp = explain_command(logs, pipeline=leaderboard_pipeline(month, department, limit)); coll = "attendance_logs"
        elif endpoint == "department_trend":
            if not department or not from_ or not to: raise ValueError("department, from and to are required")
            start, end = parse_date(from_), parse_date(to)
            if end < start or (end-start).days+1 > 92: raise ValueError("invalid date range")
            exp = explain_command(employees, pipeline=trend_pipeline(department, start, end)); coll = "employees"
        else: raise ValueError("unknown endpoint")
    except ValueError as e: raise HTTPException(422, str(e))
    return {"endpoint": endpoint, "collection": coll, "explain": json_safe_explain(exp)}
