# Job Pilot

AI-powered job scraper that fetches LinkedIn listings, scores them against your resume, and surfaces the best matches.

---

## Project structure

```
job_pilot/
├── backend/
│   ├── main.py              ← FastAPI server
│   └── jobs.db              ← SQLite (auto-created)
├── worker/
│   ├── job_navigator.py     ← Playwright LinkedIn scraper
│   ├── matcher.py           ← AI job scoring 
│   └── agent.py             ← Resume → structured profile 
├── frontend/
│   └── index.html           ← SPA dashboard
├── utils/
│   ├── init_db.py           ← Schema creation
│   └── cleanup_db.py        ← Maintenance
├── data/
│   ├── dynamic_profile.txt  ← Extracted profile (auto-generated)
│   └── raw_job.txt          ← Raw scraped descriptions
├── requirements.txt
└── .env.example
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and add your GEMINI_API_KEY
```

### 3. Initialise the database

```bash
python -m utils.init_db
```

---

## Running

### Start the API server

```bash
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/app` for the UI, or `http://localhost:8000/docs` for the API explorer.

---

## Using the pipeline

### Option A — via the UI

1. Go to **Profile** → upload your resume PDF/TXT
2. Go to **Run Pipeline** → enter job query + location → click **Scrape + Match**
3. Go to **Jobs** → browse scored listings sorted by match %

### Option B — via CLI

```bash
# 1. Extract your profile from resume
python -m worker.agent --resume data/resume.pdf

# 2. Scrape LinkedIn
python -m worker.job_navigator --query "Python Developer" --location "Bengaluru" --limit 25

# 3. Score jobs against profile
python -m worker.matcher --limit 50

# Cleanup old jobs
python -m utils.cleanup_db --days 30
```

---

## API reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET    | `/api/jobs` | List jobs (filterable, sortable, paginated) |
| GET    | `/api/jobs/{id}` | Job detail + AI score |
| POST   | `/api/jobs/{id}/status` | Update status (applied/rejected/saved) |
| POST   | `/api/run/scrape` | Trigger LinkedIn scrape |
| POST   | `/api/run/match` | Trigger AI matching |
| POST   | `/api/run/full` | Scrape + match pipeline |
| GET    | `/api/profile` | Get active profile |
| POST   | `/api/profile/upload` | Upload resume (PDF/TXT) |
| GET    | `/api/stats` | Dashboard stats |

---

## Notes on LinkedIn scraping

- Uses the **public job search** page — no login required
- Respects random delays between requests to avoid rate-limiting
- Images and fonts are blocked to reduce bandwidth and speed up scraping
- `url` is a deduplication key — re-runs won't create duplicate rows
- If LinkedIn changes its HTML structure, update the selectors in `job_navigator.py`

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | required | Your Google Gemini API key |
| `DB_PATH` | `backend/jobs.db` | SQLite database path |
| `RAW_JOB_PATH` | `data/raw_job.txt` | Raw job text output |
| `PROFILE_PATH` | `data/dynamic_profile.txt` | Profile text output |
| `HEADLESS` | `true` | Set to `false` to watch the browser |
