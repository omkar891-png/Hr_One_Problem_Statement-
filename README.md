# Employee Attendance & Analytics API

[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg?style=flat&logo=fastapi)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB.svg?style=flat&logo=python)](https://www.python.org/)
[![MongoDB](https://img.shields.io/badge/MongoDB-6.0+-47A248.svg?style=flat&logo=mongodb)](https://www.mongodb.com/)
[![Tests](https://img.shields.io/badge/Tests-35%2F35%20Passing-brightgreen.svg)]()
[![Code Quality](https://img.shields.io/badge/Code%20Style-PEP%208%20%7C%20Ruff%20Clean-success.svg)]()

A high-performance, production-grade backend service built with **FastAPI** and **MongoDB** for employee attendance tracking, manual shift regularization with immutable audit history, and departmental analytics powered entirely by MongoDB aggregation pipelines.

---

## 1. Executive Summary

This repository represents a full, battle-tested implementation of the **HROne Engineering Assignment (Contract v2.0)** satisfying all contract requirements in `openapi.yaml`, `DATA_MODEL.md`, and business rules **R1 through R10**.

### Key Deliverables Completed:
- **Part A (Review & Fix):** Identified and resolved 23 defects in the original codebase, documented with root causes, detection vectors, and architectural fixes in [`REVIEW.md`](REVIEW.md).
- **Part B (Implementation):** Complete API covering employee management, race-safe punch operations, manual regularization with audit trails, and complex MongoDB aggregation pipelines for monthly, departmental, leaderboard, and trend analytics.
- **Part C (Explain & Prove):** Index coverage verified with zero collection scans (`COLLSCAN`) via the `/admin/explain/{endpoint}` query explain API, with architectural answers in [`DECISIONS.md`](DECISIONS.md).
- **Test Suite:** Comprehensive automated test suite with **35 unit and integration tests** passing (100% test pass rate).

---

## 2. System Architecture & Flow

```mermaid
flowchart TD
    Client([HTTP Client / Frontend]) -->|JSON / REST| FastAPI[FastAPI Service - app/main.py]
    
    subgraph "Core Business Logic"
        FastAPI --> Auth["Pydantic Schemas & Validators"]
        FastAPI --> BizRules["Business Rules (R1 - R10)"]
        BizRules --> R1["R1: IST & Truncated Epoch ms"]
        BizRules --> R2["R2: Strict Late Evaluation (>10m)"]
        BizRules --> R3R5["R3-R5: Overtime, Half-Up Hours, Half-Day"]
        BizRules --> R7R9["R7-R9: Working Days & Period Headcount"]
    end

    subgraph "MongoDB Database (attendance_db)"
        BizRules -->|Find / Atomic Update| Employees[(employees)]
        BizRules -->|Unique Natural Key (emp_code, date)| Attendance[(attendance_logs)]
        
        FastAPI -->|Aggregation Pipelines| Aggs["Pure MongoDB Aggregations"]
        Aggs --> Pipe1["Employee Monthly ($group)"]
        Aggs --> Pipe2["Department Summary ($lookup + $reduce)"]
        Aggs --> Pipe3["Leaderboard ($setWindowFields + $rank)"]
        Aggs --> Pipe4["Trend ($densify + 7d Moving Avg)"]
    end
```

---

## 3. Business Rules Implementation (R1 – R10)

| Rule | Specification | Implementation in `app/main.py` |
|---|---|---|
| **R1: Timezone & Attendance Date** | Dates and shifts operate in IST (`UTC+05:30`). Overnight shifts (`shift_end <= shift_start`) assign punch-ins before `shift_end` to previous day. Instants truncated to whole seconds. | Handled by `get_attendance_date()` and `epoch_ms_to_dt()`. Stored as BSON UTC datetime in DB; returned as integer epoch ms. |
| **R2: Late Computation** | Grace period 10 minutes. Late only if punch-in > 10:00 (strictly 600s) after shift start. `late_minutes` = elapsed minutes from shift start. | Computed strictly with second deltas (`diff_sec > 600`) in `compute_late_minutes()`. |
| **R3: Overtime** | Minutes from shift end to punch-out, counted only if >= 30 minutes; otherwise 0. | Handled by `compute_overtime()`, accounting for next-day shift end on overnight shifts. |
| **R4: Work Hours** | `(punch_out - punch_in)` in seconds / 3600, rounded to 2 decimals using exact **Half-Up** rounding. | Uses `decimal.Decimal` with `ROUND_HALF_UP` in `compute_work_hours()`. |
| **R5: Half Day** | Flagged `half_day = true` if rounded `work_hours < 4.50`. Counts as 0.5 present day. | Computed upon punch-out and regularization. |
| **R6: Presence** | `PRESENT`, `WFH`, and `ON_DUTY` count as present; `ABSENT` and `LEAVE` do not. | Filtered across all aggregation pipelines via `$in: ["$status", ["PRESENT", "WFH", "ON_DUTY"]]`. |
| **R7: Working Days** | Monday through Friday. Mid-month joiners counted from `joined_on`. | Calculated dynamically in `calculate_working_days()`. Weekend records contribute to late/overtime but not working presence days. |
| **R8: Decimal Rounding** | Reported numbers rounded to 2 decimals half-up; rates to 4 decimals half-up. | Strict decimal half-up rounding in Python and `$round` in MongoDB stages. |
| **R9: Headcount** | Employees with `joined_on <= period_end`, including personnel with zero attendance logs. | Rooted in `employees` collection filtered by `joined_on <= date` with outer `$lookup`. |
| **R10: Pagination** | 1-indexed pagination (`page >= 1`, max `page_size = 100`). `total` reflects filter match. | Implemented via database cursor `.skip((page - 1) * page_size).limit(page_size)` and `count_documents(q)`. |

---

## 4. Concurrency & Race-Condition Safeguards

1. **Concurrent Punch-Ins:**
   - Enforced by a compound unique index on `attendance_logs(emp_code, date)`.
   - Simultaneous requests pass validation, but MongoDB permits exactly one insert; the other throws `pymongo.errors.DuplicateKeyError` and returns `409 Conflict`.
2. **Concurrent Punch-Outs:**
   - Atomic conditional update via `find_one_and_update({"_id": rec["_id"], "punch_out": None}, ...)` ensures that parallel punch-outs never produce double calculations or lost updates.
3. **Concurrent Regularizations:**
   - Optimistic concurrency control via `find_one_and_update({"_id": rec["_id"], "history": {"$size": len(current_history)}}, ...)` prevents concurrent editors from overwriting or losing audit trail history.

---

## 5. Indexing & Performance Strategy

Indexes are created idempotently at application startup via FastAPI's `lifespan` handler:

| Collection | Index Keys | Type | Purpose |
|---|---|---|---|
| `employees` | `{ emp_code: 1 }` | Unique | Identity uniqueness, primary lookups |
| `employees` | `{ department: 1, emp_code: 1 }` | Compound | Fast department filtering |
| `employees` | `{ joined_on: 1, department: 1 }` | Compound | Headcount range filtering (R9) |
| `attendance_logs` | `{ emp_code: 1, date: 1 }` | Unique | Natural key addressability, prevents duplicate punch-in |
| `attendance_logs` | `{ date: -1, emp_code: 1 }` | Compound | Paginated listing without in-memory sort |
| `attendance_logs` | `{ emp_code: 1, punch_in: -1 }` | Compound | Reverse lookup for punch-out |
| `attendance_logs` | `{ date: 1, late_minutes: 1, emp_code: 1 }` | Compound | IXSCAN for late leaderboard queries |
| `attendance_logs` | `{ status: 1, date: -1, emp_code: 1 }` | Compound | Status-filtered attendance queries |

*Zero Collection Scans:* Verified via `/admin/explain/{endpoint}` running `db.command("explain", ..., verbosity="executionStats")`.

---

## 6. How to Run & Verify

### Prerequisites
- Python 3.11+
- MongoDB 6.0+ (running locally or via MongoDB Atlas)

### Setup Instructions
```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure MongoDB Environment Variables (optional; defaults to localhost:27017)
# Linux/macOS:
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
# Windows PowerShell:
$env:MONGO_URI="mongodb://localhost:27017"; $env:MONGO_DB="attendance_db"

# 3. Seed sample data
python sample_seed.py

# 4. Run automated test suite
python -m pytest

# 5. Start the API service
python -m uvicorn app.main:app --port 8000
```

---

## 7. API Documentation & Interactive Swagger

Once running, interactive documentation is immediately accessible:
- **Interactive Swagger UI:** [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc Documentation:** [http://localhost:8000/redoc](http://localhost:8000/redoc)
- **Health Check Endpoint:** `GET http://localhost:8000/health` &rarr; `{"status": "ok"}`

### Core Endpoint Summary

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | MongoDB readiness probe (200 OK / 503 Service Unavailable) |
| `POST` | `/employees` | Create employee with unique `emp_code` (201 / 409 / 422) |
| `GET` | `/employees` | Paginated employee list sorted by `emp_code` asc |
| `POST` | `/attendance/punch-in` | Record punch-in, compute date & late minutes (201 / 404 / 409 / 422) |
| `POST` | `/attendance/punch-out` | Close open shift, compute hours & overtime (200 / 404 / 409 / 422) |
| `GET` | `/attendance` | Paginated logs sorted by `date` desc, `emp_code` asc |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularize shift, recompute metrics, append immutable history |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee summary with presence & late metrics |
| `GET` | `/analytics/departments/summary` | Department summary with headcount, avg hours, presence |
| `GET` | `/analytics/leaderboard/late` | Competition ranking leaderboard using `$rank` window function |
| `GET` | `/analytics/departments/{dept}/trend` | Full daily series with `$densify` gap filling and 7d moving average |
| `GET` | `/admin/explain/{endpoint}` | MongoDB query execution stats proving `IXSCAN` index usage |

---

## 8. Repository Structure

```
├── app/
│   ├── __init__.py
│   └── main.py              # Full application code: schemas, business rules, endpoints, and aggregations
├── tests/
│   ├── test_aggregations.py # Tests for the 4 analytics endpoints & explain endpoint
│   ├── test_app.py          # Unit tests for business rules (R1-R10), rounding, and helpers
│   └── test_endpoints.py    # Integration tests for all HTTP endpoints, concurrency, and validation
├── sample_data/
│   ├── employees.json       # Extended JSON employee sample records
│   └── attendance_logs.json # Extended JSON attendance sample logs
├── sample_seed.py           # Database seeder script
├── requirements.txt         # Production dependencies
├── REVIEW.md                # 23 defects identified and fixed with detailed analysis
├── DECISIONS.md             # Technical design and architecture decisions
├── DATA_MODEL.md            # MongoDB collection specifications and schemas
├── openapi.yaml             # OpenAPI 3.0.3 API Contract
└── README.md                # Project documentation & recruiter guide
```
