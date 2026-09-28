# AI Agent 1

A chat assistant that answers questions from the Goal Post Pro MySQL database. A React chat UI talks to a FastAPI backend, which runs a LangChain agent (OpenAI `gpt-4.1-mini`) with database tools.

## Architecture

```
frontend (React + Vite, :5173)
    │  POST /chat  { message }
    ▼
backend (FastAPI, :8000)  ── main.py
    │  agent.invoke({ messages })
    ▼
LangChain agent (gpt-4.1-mini)
    │  tool calls
    ▼
MySQL (goalpostpro)  ── see DATABASE_SCHEMA.md
```

## Project layout

| Path | Purpose |
|---|---|
| [main.py](main.py) | FastAPI app and agent definition |
| [db_tools.py](db_tools.py) | Database tools the agent can call |
| [frontend/](frontend/) | React 19 + Vite chat UI (`src/App.jsx` is the whole UI) |
| [requirements.txt](requirements.txt) | Python dependencies (direct ones, pinned) |
| [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) | Schema reference for the `goalpostpro` database |
| `.env` | Secrets (git-ignored) |
| `.venv/` | Python 3.12 virtual environment (git-ignored) |

## Backend

**Stack:** Python 3.12, FastAPI, Uvicorn, LangChain 1.x, LangGraph, `langchain-openai`, `mysql-connector-python`, `python-dotenv`.

### Configuration

Create a `.env` in the project root with:

```
OPENAI_API_KEY=
MYSQL_HOST=
MYSQL_USER=
MYSQL_PASSWORD=
MYSQL_DATABASE=
```

### Run

```powershell
python -m venv .venv          # first time only
.venv\Scripts\Activate.ps1
pip install -r requirements.txt   # first time only
uvicorn main:app --reload --port 8000
```

### API

`POST /chat`

- Request: `{ "message": "string" }`
- Response: `{ "reply": "string" }`

CORS allows `http://localhost:5173` and `http://127.0.0.1:5173`.

### Agent tools

Defined in [db_tools.py](db_tools.py). Each call opens its own MySQL connection, and errors are returned to the agent as `{ "error": ... }` so it can correct itself. Table and column names are validated against the live schema; values are always parameterised.

| Tool | Description |
|---|---|
| `describeDatabase` | All tables, columns and logical relationships |
| `queryRecords` | Read rows from one table with filters, column selection, ordering and paging (max 200 rows) |
| `countRecords` | Count rows, optionally filtered |
| `runSelectQuery` | Single read-only `SELECT`/`WITH` for joins and aggregates (runs in a read-only transaction; max 200 rows) |
| `getPersonnelProfile` | One person's record, drill results, events and appraisals |
| `createRecord` | Insert a row (`id` and `created_at` are automatic) |
| `updateRecord` | Update columns of one row by id |
| `deleteRecord` | Delete one row by id; refused while other rows still reference it |

Safeguards:

- `users.password` is hidden from every tool, and `runSelectQuery` rejects any query mentioning `users` or `password`.
- The schema declares no foreign keys, so the relationships are listed in `REFERENCES` in `db_tools.py`. Create and update check that referenced rows exist, and delete checks that nothing references the row.
- The system prompt tells the agent to describe a change and get the user's confirmation before creating, updating or deleting. This is an instruction to the model, not an enforced control.

## Frontend

**Stack:** React 19, Vite 6, ESLint 9. The API URL is hardcoded as `API_URL` in `frontend/src/App.jsx` (`http://localhost:8000/chat`).

```powershell
cd frontend
npm install
npm run dev      # http://localhost:5173
npm run build
npm run lint
```

## Current limitations

- **Shared conversation:** history is one global list in `main.py`, so every client and browser tab shares the same conversation. It resets when the server restarts.
- **No enforced write confirmation:** the agent can create, update and delete rows, and asking for confirmation first relies on the system prompt. The DB user's grants are the only hard limit.
- **Schema doc drift:** the live `users` table has a `username` column where `DATABASE_SCHEMA.md` says `name`. The tools read the live schema, so they are unaffected.
- **Frontend `README.md`:** `frontend/README.md` is still the default Vite template.
