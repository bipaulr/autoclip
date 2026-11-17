# On Windows without make, run the same commands from scripts/autoclip.ps1 (e.g. .\scripts\autoclip.ps1 test)
PY ?= .venv/Scripts/python

up:
	docker compose up -d --build

down:
	docker compose down

migrate:
	docker compose run --rm api alembic upgrade head

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m compileall -q autoclip tests
	cd web && npm run typecheck

logs:
	docker compose logs -f --tail 100 worker beat api

.PHONY: up down migrate test lint logs
