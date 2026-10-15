# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # Copy .env.example to .env and configure MONGO_URI / MONGO_DB as needed
python sample_seed.py
uvicorn app.main:app --port 8000
```
Copy `.env.example` to `.env` and configure `MONGO_URI` and `MONGO_DB` (defaults to `mongodb://localhost:27017` and `attendance_db`).
MongoDB **6.0 or newer** is required (the grader uses 7.0; MongoDB 8.0/Atlas also supported).


## Startup Command
```bash
uvicorn app.main:app --port 8000
```

## Implemented Endpoints
- `GET /health` (C01): MongoDB ping health check (returns 200 on success, 503 on database failure).
- `POST /employees` (C02): Create employee record with field validations and unique constraint on `emp_code`.
- `GET /employees` (C03): Paginated employee listing sorted by `emp_code`, with optional department filter.
- `POST /attendance/punch-in` (C04): Punch-in recording with IST date derivation, overnight shift support, late calculation (grace period 10m), and race condition prevention.
- `POST /attendance/punch-out` (C05): Punch-out recording with work hours (ROUND_HALF_UP to 2 decimals), overtime calculation, half-day flagging, and atomic update.
- `GET /attendance` (C06): Filtered and paginated attendance record listing backed by compound index `(date DESC, emp_code ASC)`.
- `GET /analytics/employees/{emp_code}/monthly` (C07): Aggregation-based monthly attendance metrics for an employee (working days, present days, late count, attendance percentage).
- `GET /analytics/departments/summary` (C08): Department-level monthly summary via cross-collection aggregation with headcount including employees without attendance logs.
- `GET /analytics/leaderboard/late` (C09): Monthly late-minutes leaderboard using `$setWindowFields` with standard competition ranking (`$rank`), cutoff ties, and employee code tie-breaker.
- `GET /analytics/departments/{department}/trend` (C10): Daily attendance trend series using `$densify` gap filling, cumulative headcount, and 7-day moving averages.
- `PATCH /attendance/{emp_code}/{date}` (C11): Manual regularization with delta-only audit history, derived field recalculation, and optimistic CAS concurrency protection (409 Conflict).
- `GET /admin/explain/{endpoint}` (C12): MongoDB explain execution stats for main queries and aggregations across all 5 analytic endpoints, verifying index scan (`IXSCAN`) utilization.

## Limitations & Assumptions
- **Timezone**: All shift boundaries, attendance dates, and punch calculations operate under Indian Standard Time (IST, UTC+05:30) as specified by Rule R1.
- **MongoDB Version**: Requires MongoDB >= 6.0 to support `$setWindowFields` and `$densify` aggregation operators.
- **Single-Service Scope**: All code resides in `app/main.py` per assignment specification.
- **Authentication**: Authentication and authorization are out of scope per assignment specification.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, `.env.example`, and `README.md`.
No `.env`, no secrets, no Dockerfile.

