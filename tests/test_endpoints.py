"""
Comprehensive test suite testing all endpoints, error codes, and responses using FastAPI TestClient and mocked MongoDB.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

import app.main as main_module
from app.main import app


@pytest.fixture
def mocks(monkeypatch):
    mock_client = MagicMock()
    mock_database = MagicMock()
    mock_emp = MagicMock()
    mock_att = MagicMock()

    mock_database.client = mock_client
    mock_database.employees = mock_emp
    mock_database.attendance_logs = mock_att
    mock_database.__getitem__.side_effect = lambda name: mock_emp if name == "employees" else mock_att

    monkeypatch.setattr(main_module, "client", mock_client)
    monkeypatch.setattr(main_module, "db", mock_database)

    return {
        "client": mock_client,
        "db": mock_database,
        "employees": mock_emp,
        "attendance_logs": mock_att,
    }


@pytest.fixture
def client():
    return TestClient(app)


def test_health_success(client, mocks):
    mocks["client"].admin.command.return_value = {"ok": 1}
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_health_failure(client, mocks):
    mocks["client"].admin.command.side_effect = Exception("Connection refused")
    res = client.get("/health")
    assert res.status_code == 503
    assert res.json() == {"detail": "Database unavailable"}


def test_create_employee_success(client, mocks):
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    mocks["employees"].insert_one.return_value = MagicMock()
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert isinstance(data["created_at"], int)
    assert "_id" not in data


def test_create_employee_duplicate_409(client, mocks):
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    }
    mocks["employees"].insert_one.side_effect = DuplicateKeyError("duplicate")
    res = client.post("/employees", json=payload)
    assert res.status_code == 409
    assert res.json()["detail"] == "emp_code already exists"


def test_create_employee_validation_422(client, mocks):
    # shift_start == shift_end
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-05",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 422


def test_list_employees(client, mocks):
    sample = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
        "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=timezone.utc),
    }
    cursor_mock = MagicMock()
    cursor_mock.sort.return_value = cursor_mock
    cursor_mock.skip.return_value = cursor_mock
    cursor_mock.limit.return_value = [sample]

    mocks["employees"].count_documents.return_value = 1
    mocks["employees"].find.return_value = cursor_mock

    res = client.get("/employees?page=1&page_size=20")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["emp_code"] == "EMP0001"
    assert isinstance(data["items"][0]["created_at"], int)


def test_punch_in_success(client, mocks):
    emp = {
        "emp_code": "EMP0001",
        "shift_start": "09:30",
        "shift_end": "18:30",
    }
    mocks["employees"].find_one.return_value = emp
    mocks["attendance_logs"].insert_one.return_value = MagicMock()

    payload = {
        "emp_code": "EMP0001",
        "punched_at": 1783310280000,  # 09:28 IST on 2026-07-06
        "status": "PRESENT",
    }
    res = client.post("/attendance/punch-in", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["date"] == "2026-07-06"
    assert data["late_minutes"] == 0
    assert data["punch_in"] == 1783310280000
    assert data["punch_out"] is None
    assert "id" not in data
    assert "_id" not in data


def test_punch_in_unknown_emp_404(client, mocks):
    mocks["employees"].find_one.return_value = None
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert res.status_code == 404


def test_punch_in_duplicate_409(client, mocks):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    mocks["employees"].find_one.return_value = emp
    mocks["attendance_logs"].insert_one.side_effect = DuplicateKeyError("dup")

    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0001"})
    assert res.status_code == 409


def test_punch_out_success(client, mocks):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    rec = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc),  # 09:28 IST
        "punch_out": None,
    }
    updated_rec = dict(rec)
    updated_rec["punch_out"] = datetime(2026, 7, 6, 13, 5, tzinfo=timezone.utc)  # 18:35 IST
    updated_rec["work_hours"] = 9.12
    updated_rec["overtime_minutes"] = 0
    updated_rec["half_day"] = False
    updated_rec["late_minutes"] = 0

    mocks["employees"].find_one.return_value = emp
    mocks["attendance_logs"].find_one.return_value = rec
    mocks["attendance_logs"].find_one_and_update.return_value = updated_rec

    payload = {
        "emp_code": "EMP0001",
        "punched_at": 1783343100000,  # 18:35 IST
    }
    res = client.post("/attendance/punch-out", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] == 9.12
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False


def test_punch_out_no_punchin_404(client, mocks):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    mocks["employees"].find_one.return_value = emp
    mocks["attendance_logs"].find_one.return_value = None

    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0001"})
    assert res.status_code == 404


def test_punch_out_already_punched_out_409(client, mocks):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    rec = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "punch_in": datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 6, 13, 5, tzinfo=timezone.utc),
    }
    mocks["employees"].find_one.return_value = emp
    mocks["attendance_logs"].find_one.return_value = rec

    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0001"})
    assert res.status_code == 409


def test_punch_out_invalid_timing_422(client, mocks):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    # punched_at earlier than punch_in
    rec = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc),
        "punch_out": None,
    }
    mocks["employees"].find_one.return_value = emp
    mocks["attendance_logs"].find_one.return_value = rec

    payload = {
        "emp_code": "EMP0001",
        "punched_at": 1783300000000,  # earlier than punch_in
    }
    res = client.post("/attendance/punch-out", json=payload)
    assert res.status_code == 422


def test_list_attendance_date_validation_422(client):
    res = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert res.status_code == 422
    assert "date_from must be <= date_to" in res.json()["detail"]


def test_regularize_unknown_emp_404(client, mocks):
    mocks["employees"].find_one.return_value = None
    payload = {"reason": "test correction", "regularized_by": "admin"}
    res = client.patch("/attendance/EMP9999/2026-07-06", json=payload)
    assert res.status_code == 404


def test_regularize_changes_nothing_422(client, mocks):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    rec = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 6, 13, 5, tzinfo=timezone.utc),
        "work_hours": 9.12,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    mocks["employees"].find_one.return_value = emp
    mocks["attendance_logs"].find_one.return_value = rec

    payload = {
        "reason": "no changes requested",
        "regularized_by": "admin",
        "status": "PRESENT",  # same status
    }
    res = client.patch("/attendance/EMP0001/2026-07-06", json=payload)
    assert res.status_code == 422
    assert "changes nothing" in res.json()["detail"]


def test_regularize_success(client, mocks):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    rec = {
        "_id": "dummy_id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 4, 35, tzinfo=timezone.utc),  # 10:05 IST (late 35 min)
        "punch_out": datetime(2026, 7, 6, 13, 40, tzinfo=timezone.utc),
        "work_hours": 9.08,
        "late_minutes": 35,
        "overtime_minutes": 40,
        "half_day": False,
        "history": [],
    }
    updated_rec = dict(rec)
    updated_rec["punch_in"] = datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc)  # corrected to 09:28 IST
    updated_rec["late_minutes"] = 0
    updated_rec["work_hours"] = 9.70
    updated_rec["history"] = [
        {
            "at": datetime(2026, 7, 7, 4, 0, tzinfo=timezone.utc),
            "by": "admin",
            "reason": "biometric glitch",
            "changes": {
                "punch_in": {"from": rec["punch_in"], "to": updated_rec["punch_in"]},
                "late_minutes": {"from": 35, "to": 0},
                "work_hours": {"from": 9.08, "to": 9.70},
            },
        }
    ]

    mocks["employees"].find_one.return_value = emp
    mocks["attendance_logs"].find_one.return_value = rec
    mocks["attendance_logs"].find_one_and_update.return_value = updated_rec

    payload = {
        "reason": "biometric glitch",
        "regularized_by": "admin",
        "punch_in": 1783310280000,
    }
    res = client.patch("/attendance/EMP0001/2026-07-06", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["late_minutes"] == 0
    assert len(data["history"]) == 1
    assert isinstance(data["history"][0]["at"], int)
    assert data["history"][0]["changes"]["late_minutes"]["to"] == 0


def test_department_trend_range_validation_422(client, mocks):
    mocks["employees"].count_documents.return_value = 5
    # to < from
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
    assert res.status_code == 422

    # > 92 days
    res2 = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01")
    assert res2.status_code == 422


def test_explain_endpoint_attendance_list(client, mocks):
    mocks["db"].command.return_value = {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}

    res = client.get("/admin/explain/attendance_list")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "attendance_list"
    assert data["collection"] == "attendance_logs"
    assert "explain" in data


def test_explain_endpoint_missing_param_422(client):
    # employee_monthly requires emp_code and month
    res = client.get("/admin/explain/employee_monthly")
    assert res.status_code == 422

    # department_summary requires month
    res2 = client.get("/admin/explain/department_summary")
    assert res2.status_code == 422
