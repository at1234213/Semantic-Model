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
docker compose exec api pytest -q
```

## Authentication

Every route requires an API key. Mint the first one directly against the
database, because the tenant routes it unlocks are the ones that would
otherwise have to create it:

```bash
docker compose exec api python -m scripts.bootstrap_admin_key "ops"
```

The secret is shown once; only a SHA-256 digest is stored. Send it as
`Authorization: Bearer <secret>`. Admin keys reach `/api/tenants`; ordinary
keys are scoped to their own tenant and reach everything else.

## Asking a question

```bash
curl -X POST http://localhost:8000/api/ask \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"question": "revenue by country last quarter",
       "semantic_model_version_id": "...", "execute": true}'
```

`execute: false` returns the compiled SQL without running it.

## Before deploying beyond localhost

- `ENV` must not be `development`, or the data-source host policy stays
  permissive and a tenant can point a "warehouse" at internal addresses.
- Set a real `CREDENTIAL_ENCRYPTION_KEY` and `SECRET_KEY`; the placeholders
  are refused where it matters and dangerous where they are not.
- The app connects as `app_user`. Check `/api/ready` reports
  `row_level_security_effective: true` — connecting as a superuser would
  silently disable every isolation policy.
- Run migrations with `ALEMBIC_DATABASE_URL` (superuser); the app itself
  cannot alter its own schema, deliberately.
- `/api/ask` runs a model call per question when `INTENT_PROVIDER=claude`.
  There is no rate limiting yet.
