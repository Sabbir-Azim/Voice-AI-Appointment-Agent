"""Appointment booking against the `doctors` and `appointments` collections.

Doctor working hours are stored in the clinic's local time zone. Appointment
start/end times are stored as UTC datetimes; every value the LLM sees or sends
is a local time string like "2026-11-24T09:30" so it never has to do time-zone
math itself.

Double bookings are prevented by the partial unique index
`appointments_slot_unique` on (doctor_id, start) where status == "booked"
(created by db/indexes.ts). book_appointment relies on it, so two callers
racing for the same slot cannot both succeed.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

LOCAL_FORMAT = "%Y-%m-%dT%H:%M"
MAX_SLOTS_RETURNED = 8
SEARCH_AHEAD_DAYS = 14
_WEEKDAYS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


class AppointmentError(Exception):
    """A booking problem the agent should explain to the caller."""


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def speakable(dt: datetime, tz: ZoneInfo) -> str:
    """Format a datetime for TTS, e.g. 'Tuesday, November 24 at 9:30 AM'."""
    local = dt.astimezone(tz)
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return (
        f"{_WEEKDAYS[local.weekday()]}, {local.strftime('%B')} {local.day} "
        f"at {hour}:{local.minute:02d} {suffix}"
    )


def parse_local(value: str, tz: ZoneInfo) -> datetime:
    """Parse 'YYYY-MM-DDTHH:MM' in clinic-local time into an aware datetime."""
    try:
        naive = datetime.strptime(value.strip(), LOCAL_FORMAT)
    except ValueError as exc:
        raise AppointmentError(
            f"Could not read time '{value}'. Use the format YYYY-MM-DDTHH:MM."
        ) from exc
    return naive.replace(tzinfo=tz)


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise AppointmentError(
            f"Could not read date '{value}'. Use the format YYYY-MM-DD."
        ) from exc


def day_slots(doctor: dict, day: date, tz: ZoneInfo) -> list[datetime]:
    """All slot start times for a doctor on a local date, ignoring bookings."""
    if day.isoweekday() not in doctor["working_days"]:
        return []
    step = timedelta(minutes=doctor["slot_minutes"])
    cursor = datetime.combine(day, time(doctor["start_hour"]), tzinfo=tz)
    end = datetime.combine(day, time(doctor["end_hour"]), tzinfo=tz)
    slots = []
    while cursor + step <= end:
        slots.append(cursor)
        cursor += step
    return slots


async def get_doctor(db: AsyncDatabase, doctor_id: str) -> dict:
    doctor = await db.doctors.find_one({"doctor_id": doctor_id})
    if not doctor:
        raise AppointmentError(
            f"No doctor with id '{doctor_id}'. Call list_doctors for valid ids."
        )
    return doctor


async def list_doctors(db: AsyncDatabase) -> list[dict]:
    cursor = db.doctors.find({}, {"_id": 0, "created_at": 0}).sort("name", 1)
    doctors = await cursor.to_list(length=None)
    return [
        {
            "doctor_id": d["doctor_id"],
            "name": d["name"],
            "specialty": d["specialty"],
            "works_on": [_WEEKDAYS[i - 1] for i in d["working_days"]],
            "hours": f"{d['start_hour']}:00 to {d['end_hour']}:00",
        }
        for d in doctors
    ]


async def _open_slots_on(
    db: AsyncDatabase, doctor: dict, day: date, tz: ZoneInfo, now: datetime
) -> list[datetime]:
    slots = [s for s in day_slots(doctor, day, tz) if s > now]
    if not slots:
        return []
    cursor = db.appointments.find(
        {
            "doctor_id": doctor["doctor_id"],
            "status": "booked",
            "start": {"$gte": slots[0], "$lte": slots[-1]},
        },
        {"start": 1},
    )
    # PyMongo returns naive UTC datetimes unless tz_aware=True on the client.
    taken = {
        (a["start"] if a["start"].tzinfo else a["start"].replace(tzinfo=timezone.utc))
        for a in await cursor.to_list(length=None)
    }
    return [s for s in slots if s not in taken]


async def check_availability(
    db: AsyncDatabase,
    doctor_id: str,
    day: str,
    tz: ZoneInfo,
    now: datetime | None = None,
) -> dict:
    """Free slots for a doctor on `day`, or on the next day that has any."""
    now = now or _utcnow()
    doctor = await get_doctor(db, doctor_id)
    requested = parse_date(day)
    start_day = max(requested, now.astimezone(tz).date())
    for offset in range(SEARCH_AHEAD_DAYS + 1):
        candidate = start_day + timedelta(days=offset)
        open_slots = await _open_slots_on(db, doctor, candidate, tz, now)
        if open_slots:
            return {
                "doctor": doctor["name"],
                "date": candidate.isoformat(),
                "requested_date_available": candidate == requested,
                "slots": [
                    {"start": s.strftime(LOCAL_FORMAT), "spoken": speakable(s, tz)}
                    for s in open_slots[:MAX_SLOTS_RETURNED]
                ],
            }
    return {
        "doctor": doctor["name"],
        "date": None,
        "requested_date_available": False,
        "slots": [],
    }


async def book_appointment(
    db: AsyncDatabase,
    user_id: str,
    doctor_id: str,
    start: str,
    reason: str,
    tz: ZoneInfo,
    now: datetime | None = None,
) -> dict:
    """Book a slot. Raises AppointmentError when it is invalid or taken."""
    now = now or _utcnow()
    user = await db.users.find_one({"user_id": user_id}) or {}
    if not user.get("name"):
        raise AppointmentError(
            "The patient's name is not on file. Ask for their full name and "
            "call update_profile with field='name' before booking."
        )
    doctor = await get_doctor(db, doctor_id)
    start_local = parse_local(start, tz)
    if start_local <= now:
        raise AppointmentError("That time is in the past. Offer a future slot.")
    if start_local not in day_slots(doctor, start_local.date(), tz):
        raise AppointmentError(
            f"{doctor['name']} does not have a slot starting at "
            f"{speakable(start_local, tz)}. Call check_availability first."
        )

    start_utc = start_local.astimezone(timezone.utc)
    doc = {
        "user_id": user_id,
        "patient_name": user["name"],
        "doctor_id": doctor_id,
        "doctor_name": doctor["name"],
        "start": start_utc,
        "end": start_utc + timedelta(minutes=doctor["slot_minutes"]),
        "reason": reason,
        "status": "booked",
        "created_at": now,
    }
    try:
        result = await db.appointments.insert_one(doc)
    except DuplicateKeyError as exc:
        raise AppointmentError(
            "That slot was just taken by someone else. Check availability "
            "again and offer another time."
        ) from exc
    return {
        "appointment_id": str(result.inserted_id),
        "doctor": doctor["name"],
        "when": speakable(start_local, tz),
        "reason": reason,
    }


async def list_appointments(
    db: AsyncDatabase, user_id: str, tz: ZoneInfo, now: datetime | None = None
) -> list[dict]:
    """Upcoming booked appointments for a patient, soonest first."""
    now = now or _utcnow()
    cursor = db.appointments.find(
        {"user_id": user_id, "status": "booked", "start": {"$gte": now}}
    ).sort("start", 1)
    return [
        {
            "appointment_id": str(a["_id"]),
            "doctor": a["doctor_name"],
            "when": speakable(a["start"].replace(tzinfo=timezone.utc), tz),
            "reason": a.get("reason", ""),
        }
        for a in await cursor.to_list(length=20)
    ]


async def cancel_appointment(
    db: AsyncDatabase, user_id: str, appointment_id: str, now: datetime | None = None
) -> str:
    """Cancel one of the patient's own booked appointments."""
    try:
        oid = ObjectId(appointment_id)
    except (InvalidId, TypeError) as exc:
        raise AppointmentError(
            "Unknown appointment id. Call my_appointments to get valid ids."
        ) from exc
    result = await db.appointments.update_one(
        {"_id": oid, "user_id": user_id, "status": "booked"},
        {"$set": {"status": "cancelled", "cancelled_at": now or _utcnow()}},
    )
    if result.matched_count == 0:
        raise AppointmentError(
            "No booked appointment with that id for this patient. Call "
            "my_appointments to get valid ids."
        )
    return "Appointment cancelled."
