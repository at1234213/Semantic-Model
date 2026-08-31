# semantic-model

A FastAPI service backed by Postgres (pgvector) for storing and searching documents
by semantic similarity.

## Layout

```
semantic-model/
├── .gitignore
├── .env.example
├── README.md
├── requirements.txt
├── docker-compose.yml
├── Dockerfile
├── alembic.ini
├── app/              # application code (api, core, models, services)
├── migrations/       # alembic migrations
└── tests/            # pytest suite
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Run

With Docker (starts Postgres + the API):

```bash
docker compose up --build
```

Locally, against a database you already have running:

```bash
uvicorn app.main:app --reload
```

The API is then at http://localhost:8000 — docs at `/docs`, health at `/api/health`.

## Migrations

```bash
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
```

## Tests

```bash
pytest
```
