"""
Tests verifying the 4 analytics endpoints and explain endpoints.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app, build_employee_monthly_pipeline, build_department_summary_pipeline, build_late_leaderboard_pipeline, build_department_trend_pipeline


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


def test_employee_monthly_summary(client, mocks):
    emp = {"emp_code": "EMP0001", "joined_on": "2026-01-05"}
    mocks["employees"].find_one.return_value = emp

    agg_output = [
        {
            "_id": None,
            "present_days": 19.5,
            "leave_days": 2,
            "late_count": 3,
            "total_late_minutes": 75,
            "total_overtime_minutes": 120,
        }
    ]
    mocks["attendance_logs"].aggregate.return_value = iter(agg_output)

    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 19.5
    assert data["leave_days"] == 2
    assert data["late_count"] == 3
    assert data["total_late_minutes"] == 75
    assert data["total_overtime_minutes"] == 120
    # 19.5 / 23 * 100 = 84.7826... -> 84.78
    assert data["attendance_pct"] == 84.78


def test_department_summary(client, mocks):
    agg_output = [
        {
            "department": "Engineering",
            "headcount": 5,
            "present_days": 80.5,
            "avg_work_hours": 8.75,
            "late_count": 6,
            "total_late_minutes": 140,
            "leave_count": 3,
            "on_duty_count": 2,
        }
    ]
    mocks["employees"].aggregate.return_value = iter(agg_output)

    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["department"] == "Engineering"
    assert item["headcount"] == 5
    assert item["present_days"] == 80.5
    assert item["avg_work_hours"] == 8.75


def test_late_leaderboard(client, mocks):
    agg_output = [
        {
            "rank": 1,
            "emp_code": "EMP0003",
            "name": "David",
            "department": "Sales",
            "total_late_minutes": 120,
            "late_count": 4,
        },
        {
            "rank": 2,
            "emp_code": "EMP0001",
            "name": "Asha",
            "department": "Engineering",
            "total_late_minutes": 80,
            "late_count": 2,
        },
        {
            "rank": 2,
            "emp_code": "EMP0002",
            "name": "Bob",
            "department": "Sales",
            "total_late_minutes": 80,
            "late_count": 2,
        },
    ]
    mocks["attendance_logs"].aggregate.return_value = iter(agg_output)

    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) == 3
    assert data["items"][0]["rank"] == 1
    assert data["items"][1]["rank"] == 2
    assert data["items"][2]["rank"] == 2


def test_department_trend(client, mocks):
    mocks["employees"].count_documents.return_value = 5
    agg_output = [
        {
            "date": "2026-07-06",
            "is_working_day": True,
            "headcount": 5,
            "present_count": 4.5,
            "late_count": 1,
            "attendance_rate": 0.9000,
            "moving_avg_7d": 0.9000,
        },
        {
            "date": "2026-07-07",
            "is_working_day": True,
            "headcount": 5,
            "present_count": 5.0,
            "late_count": 0,
            "attendance_rate": 1.0000,
            "moving_avg_7d": 0.9500,
        },
    ]
    mocks["attendance_logs"].aggregate.return_value = iter(agg_output)

    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-07")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 2
    assert data["items"][0]["date"] == "2026-07-06"
    assert data["items"][0]["attendance_rate"] == 0.9000
    assert data["items"][1]["moving_avg_7d"] == 0.9500


def test_all_explain_endpoints(client, mocks):
    mocks["db"].command.return_value = {
        "stages": [{"$cursor": {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}}],
        "executionStats": {"nReturned": 10},
    }

    # 1. attendance_list
    res1 = client.get("/admin/explain/attendance_list?emp_code=EMP0001")
    assert res1.status_code == 200
    assert res1.json()["endpoint"] == "attendance_list"
    assert res1.json()["collection"] == "attendance_logs"

    # 2. employee_monthly
    res2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert res2.status_code == 200
    assert res2.json()["endpoint"] == "employee_monthly"
    assert res2.json()["collection"] == "attendance_logs"

    # 3. department_summary
    res3 = client.get("/admin/explain/department_summary?month=2026-07")
    assert res3.status_code == 200
    assert res3.json()["endpoint"] == "department_summary"
    assert res3.json()["collection"] == "employees"

    # 4. late_leaderboard
    res4 = client.get("/admin/explain/late_leaderboard?month=2026-07")
    assert res4.status_code == 200
    assert res4.json()["endpoint"] == "late_leaderboard"
    assert res4.json()["collection"] == "attendance_logs"

    # 5. department_trend
    res5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07")
    assert res5.status_code == 200
    assert res5.json()["endpoint"] == "department_trend"
    assert res5.json()["collection"] == "attendance_logs"
