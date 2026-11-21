"""Tests for the appointment booking tools (tools/appointments.py).

The pure slot/format tests run without any services. The database tests use
the `seeded_db` fixture from conftest.py, which is skipped when MONGODB_URI is
not set. All tests pin `now` so they do not depend on the real clock.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from agent import receptionist_instructions
from tools.appointments import (
    AppointmentError,
    book_appointment,
    cancel_appointment,
    check_availability,
    day_slots,
    list_appointments,
    list_doctors,
    speakable,
)

TZ = ZoneInfo("America/New_York")
# Monday, January 7, 2030 at 8:00 AM clinic time.
NOW = datetime(2030, 1, 7, 8, 0, tzinfo=TZ)
MONDAY = "2030-01-07"

KHAN = {
    "doctor_id": "dr_khan",
    "working_days": [1, 2, 3, 4, 5],
    "start_hour": 9,
    "end_hour": 17,
    "slot_minutes": 30,
}


# --- Pure logic ----------------------------------------------------------


def test_day_slots_cover_working_hours() -> None:
    slots = day_slots(KHAN, date(2030, 1, 7), TZ)
    assert len(slots) == 16
    assert slots[0] == datetime(2030, 1, 7, 9, 0, tzinfo=TZ)
    # Last slot must end by closing time, so it starts at 16:30.
    assert slots[-1] == datetime(2030, 1, 7, 16, 30, tzinfo=TZ)


def test_day_slots_empty_on_day_off() -> None:
    assert day_slots(KHAN, date(2030, 1, 6), TZ) == []  # Sunday


def test_speakable_reads_naturally() -> None:
    assert speakable(datetime(2030, 1, 7, 9, 0, tzinfo=TZ), TZ) == (
        "Monday, January 7 at 9:00 AM"
    )
    assert speakable(datetime(2030, 1, 7, 13, 30, tzinfo=TZ), TZ) == (
        "Monday, January 7 at 1:30 PM"
    )


def test_instructions_include_today_and_safety_rules() -> None:
    text = receptionist_instructions(NOW)
    assert "Monday, January 7, 2030" in text
    assert "911" in text
    assert "never diagnose" in text


# --- Database-backed -----------------------------------------------------


async def test_list_doctors(seeded_db: AsyncDatabase) -> None:
    doctors = await list_doctors(seeded_db)
    assert {d["doctor_id"] for d in doctors} == {"dr_khan", "dr_patel"}


async def test_availability_skips_past_and_booked_slots(
    seeded_db: AsyncDatabase,
) -> None:
    now = datetime(2030, 1, 7, 10, 10, tzinfo=TZ)
    await book_appointment(
        seeded_db, "user_1", "dr_khan", "2030-01-07T10:30", "checkup", TZ, now=now
    )
    result = await check_availability(seeded_db, "dr_khan", MONDAY, TZ, now=now)
    starts = [s["start"] for s in result["slots"]]
    assert result["requested_date_available"] is True
    assert "2030-01-07T10:00" not in starts  # already past
    assert "2030-01-07T10:30" not in starts  # booked
    assert starts[0] == "2030-01-07T11:00"


async def test_availability_rolls_forward_when_doctor_is_off(
    seeded_db: AsyncDatabase,
) -> None:
    # Dr. Patel does not work Mondays; the next working day is Tuesday.
    result = await check_availability(seeded_db, "dr_patel", MONDAY, TZ, now=NOW)
    assert result["requested_date_available"] is False
    assert result["date"] == "2030-01-08"
    assert result["slots"][0]["spoken"] == "Tuesday, January 8 at 9:00 AM"


async def test_unknown_doctor_raises(seeded_db: AsyncDatabase) -> None:
    with pytest.raises(AppointmentError, match="No doctor"):
        await check_availability(seeded_db, "dr_nobody", MONDAY, TZ, now=NOW)


async def test_double_booking_is_rejected(seeded_db: AsyncDatabase) -> None:
    booked = await book_appointment(
        seeded_db, "user_1", "dr_khan", "2030-01-07T09:00", "flu shot", TZ, now=NOW
    )
    assert booked["when"] == "Monday, January 7 at 9:00 AM"

    await seeded_db.users.insert_one({"user_id": "user_2", "name": "Casey"})
    with pytest.raises(AppointmentError, match="just taken"):
        await book_appointment(
            seeded_db, "user_2", "dr_khan", "2030-01-07T09:00", "rash", TZ, now=NOW
        )


async def test_booking_requires_patient_name(seeded_db: AsyncDatabase) -> None:
    with pytest.raises(AppointmentError, match="name is not on file"):
        await book_appointment(
            seeded_db, "anon", "dr_khan", "2030-01-07T09:00", "checkup", TZ, now=NOW
        )


@pytest.mark.parametrize(
    "start",
    [
        "2030-01-07T09:15",  # not on the 30-minute grid
        "2030-01-07T17:00",  # after closing
        "2030-01-13T10:00",  # Sunday, doctor is off
        "2030-01-07T07:00",  # before `now`
        "tomorrow at nine",  # unparseable
    ],
)
async def test_booking_rejects_invalid_slots(
    seeded_db: AsyncDatabase, start: str
) -> None:
    with pytest.raises(AppointmentError):
        await book_appointment(
            seeded_db, "user_1", "dr_khan", start, "checkup", TZ, now=NOW
        )


async def test_cancel_frees_slot_and_is_scoped_to_patient(
    seeded_db: AsyncDatabase,
) -> None:
    booked = await book_appointment(
        seeded_db, "user_1", "dr_khan", "2030-01-07T09:00", "checkup", TZ, now=NOW
    )
    appt_id = booked["appointment_id"]

    upcoming = await list_appointments(seeded_db, "user_1", TZ, now=NOW)
    assert [a["appointment_id"] for a in upcoming] == [appt_id]

    # Another caller cannot cancel someone else's appointment.
    with pytest.raises(AppointmentError):
        await cancel_appointment(seeded_db, "user_2", appt_id, now=NOW)
    with pytest.raises(AppointmentError, match="Unknown appointment id"):
        await cancel_appointment(seeded_db, "user_1", "not-an-id", now=NOW)

    assert await cancel_appointment(seeded_db, "user_1", appt_id, now=NOW) == (
        "Appointment cancelled."
    )
    assert await list_appointments(seeded_db, "user_1", TZ, now=NOW) == []

    # The partial unique index lets the cancelled slot be booked again.
    rebooked = await book_appointment(
        seeded_db, "user_1", "dr_khan", "2030-01-07T09:00", "checkup", TZ, now=NOW
    )
    assert rebooked["appointment_id"] != appt_id
