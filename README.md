# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All code is in `app/main.py`.

## Run

python -m venv .venv
.venv\Scripts\activate          # Windows  
pip install -r requirements.txt
# set MONGO_URI and MONGO_DB as environment variables, or in a local .env file
uvicorn app.main:app --port 8000
```
Needs MongoDB 5.1 or newer (the trend endpoint uses `$densify`). Indexes are created automatically at startup.

## What is implemented
Everything in the contract: health, employees (create, list), punch-in, punch-out, attendance list, manual correction (PATCH) with an audit trail, the four analytics endpoints (employee monthly, department summary, late leaderboard, department trend) and `/admin/explain/{endpoint}`.

## Choices where the contract left room
- **Punch-out** closes the newest record whose `punch_in` is at or before `punched_at`. A `punched_at` earlier than every punch-in therefore returns 404, and one equal to the punch-in returns 422.
- **Corrections** compare all seven stored fields (status, punch times and the four derived values). A request that changes none of them returns 422. Concurrent corrections are guarded: the loser gets 409.
- **Trend** `moving_avg_7d` averages the rounded `attendance_rate` values of the last seven calendar rows.
- **Analytics** ignore attendance logs whose employee does not exist.

## Testing and limits
I tested the endpoints by hand through the `/docs` page against MongoDB Atlas (free tier), checking the analytics against numbers worked out by hand from the sample data. I did not load-test with 100,000 records.

See `REVIEW.md` for the starter code review and `DECISIONS.md` for the design answers.