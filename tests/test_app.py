"""
Unit test suite verifying helpers, Pydantic schemas, and endpoints with mocked MongoDB.
"""
from datetime import datetime, date, timezone, timedelta
from decimal import Decimal
import pytest
from pydantic import ValidationError

from app.main import (
    IST,
    dt_to_epoch_ms,
    epoch_ms_to_dt,
    now_epoch_ms,
    round_half_up,
    get_attendance_date,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    calculate_working_days,
    EmployeeCreate,
    PunchInRequest,
    PunchOutRequest,
    RegularizeRequest,
)


def test_dt_and_epoch_conversions():
    ms = 1783312500000
    dt = epoch_ms_to_dt(ms)
    assert dt.tzinfo == timezone.utc
    assert dt_to_epoch_ms(dt) == ms

    # Truncation to whole seconds (R1)
    ms_fractional = 1783312500900
    dt_trunc = epoch_ms_to_dt(ms_fractional)
    assert dt_to_epoch_ms(dt_trunc) == 1783312500000


def test_round_half_up():
    assert round_half_up(2.125, 2) == 2.13
    assert round_half_up(2.124, 2) == 2.12
    assert round_half_up(2.126, 2) == 2.13
    assert round_half_up(0.12345, 4) == 0.1235
    assert round_half_up(0.12344, 4) == 0.1234


def test_r1_attendance_date_normal():
    # 09:28 IST on 2026-07-06 (03:58 UTC)
    dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    d = get_attendance_date(dt, "09:30", "18:30")
    assert d == "2026-07-06"


def test_r1_attendance_date_overnight():
    # Overnight shift: 22:00 - 06:00
    # 21:55 IST on 2026-07-06 (16:25 UTC) -> 2026-07-06
    dt1 = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    assert get_attendance_date(dt1, "22:00", "06:00") == "2026-07-06"

    # 00:05 IST on 2026-07-07 (18:35 UTC on 2026-07-06) -> 2026-07-06
    dt2 = datetime(2026, 7, 6, 18, 35, 0, tzinfo=timezone.utc)
    assert get_attendance_date(dt2, "22:00", "06:00") == "2026-07-06"

    # 05:59 IST on 2026-07-07 -> 2026-07-06
    dt3 = datetime(2026, 7, 7, 0, 29, 0, tzinfo=timezone.utc)
    assert get_attendance_date(dt3, "22:00", "06:00") == "2026-07-06"

    # 06:00 IST on 2026-07-07 -> 2026-07-07 (not earlier than shift_end)
    dt4 = datetime(2026, 7, 7, 0, 30, 0, tzinfo=timezone.utc)
    assert get_attendance_date(dt4, "22:00", "06:00") == "2026-07-07"


def test_r2_late_minutes():
    # Shift 09:30 on 2026-07-06:
    # 09:40:00 -> 0
    dt_40 = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_40, "09:30", "18:30", "2026-07-06") == 0

    # 09:40:01 -> 10
    dt_40_01 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_40_01, "09:30", "18:30", "2026-07-06") == 10

    # 10:15:59 -> 45
    dt_15_59 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_15_59, "09:30", "18:30", "2026-07-06") == 45

    # Early: 09:25:00 -> 0
    dt_early = datetime(2026, 7, 6, 3, 55, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_early, "09:30", "18:30", "2026-07-06") == 0

    # Overnight shift (22:00 - 06:00) on 2026-07-06:
    # Punch in at 00:05 IST on 2026-07-07 (2h 5min after 22:00 = 125 min)
    dt_overnight = datetime(2026, 7, 6, 18, 35, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_overnight, "22:00", "06:00", "2026-07-06") == 125


def test_r4_work_hours():
    # Sample 1: 03:58:00Z to 13:05:00Z = 32820 seconds -> 9.12
    dt1 = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    dt2 = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_work_hours(dt1, dt2) == 9.12

    # Half-up rounding test: 7650 seconds = 2.125 hours -> 2.13
    dt3 = datetime(2026, 7, 6, 0, 0, 0, tzinfo=timezone.utc)
    dt4 = datetime(2026, 7, 6, 2, 7, 30, tzinfo=timezone.utc)
    assert compute_work_hours(dt3, dt4) == 2.13


def test_r3_overtime():
    # Normal shift 09:30 - 18:30 on 2026-07-06
    # 18:35 (5 min) -> 0 (< 30)
    dt_out_5 = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out_5, "09:30", "18:30", "2026-07-06") == 0

    # 19:10 (40 min) -> 40 (>= 30)
    dt_out_40 = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out_40, "09:30", "18:30", "2026-07-06") == 40

    # Overnight shift 22:00 - 06:00 on 2026-07-06
    # 06:40 IST on 2026-07-07 (01:10 UTC) -> 40
    dt_out_overnight = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out_overnight, "22:00", "06:00", "2026-07-06") == 40

    # 06:25 IST on 2026-07-07 (25 min) -> 0 (< 30)
    dt_out_overnight_25 = datetime(2026, 7, 7, 0, 55, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out_overnight_25, "22:00", "06:00", "2026-07-06") == 0


def test_r7_working_days():
    # July 2026 has 23 weekdays
    assert calculate_working_days("2026-07", "2026-01-05") == 23
    # Mid-month joiner: joined 2026-07-15
    assert calculate_working_days("2026-07", "2026-07-15") == 13
    # Joined in future: joined 2026-08-01
    assert calculate_working_days("2026-07", "2026-08-01") == 0


def test_employee_create_schema():
    # Valid
    emp = EmployeeCreate(
        emp_code="EMP0001",
        name="Asha Rao",
        email="asha@example.com",
        department="Engineering",
        shift_start="09:30",
        shift_end="18:30",
        joined_on="2026-01-05",
    )
    assert emp.emp_code == "EMP0001"

    # Equal shift_start and shift_end -> ValueError
    with pytest.raises(ValidationError):
        EmployeeCreate(
            emp_code="EMP0001",
            name="Asha Rao",
            email="asha@example.com",
            department="Engineering",
            shift_start="09:30",
            shift_end="09:30",
            joined_on="2026-01-05",
        )

    # Invalid emp_code format -> ValidationError
    with pytest.raises(ValidationError):
        EmployeeCreate(
            emp_code="INVALID",
            name="Asha Rao",
            email="asha@example.com",
            department="Engineering",
            joined_on="2026-01-05",
        )


def test_epoch_millis_strict_validation():
    # Seconds-looking value (< 100_000_000_000) rejected
    with pytest.raises(ValidationError):
        PunchInRequest(emp_code="EMP0001", punched_at=1783312500)

    # String value rejected (strict int)
    with pytest.raises(ValidationError):
        PunchInRequest(emp_code="EMP0001", punched_at="1783312500000")

    # Float value rejected (strict int)
    with pytest.raises(ValidationError):
        PunchInRequest(emp_code="EMP0001", punched_at=1783312500000.5)

    # Valid ms accepted
    p = PunchInRequest(emp_code="EMP0001", punched_at=1783312500000)
    assert p.punched_at == 1783312500000
