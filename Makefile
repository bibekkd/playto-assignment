.PHONY: help install migrate seed smoke dev worker beat test test-fail-roll shell clean \
        frontend-install frontend-dev frontend-build up

# Settings used everywhere. Override on the command line, e.g.:
#   make dev SETTLEMENT=0.8     -> force every payout to fail
#   make dev SETTLEMENT=0.95    -> force every payout to hang
SETTLEMENT ?= 0.0
PORT       ?= 8000
DJANGO     := uv run --project . python backend/manage.py

help:
	@echo "Playto Payout — common tasks"
	@echo ""
	@echo "Backend:"
	@echo "  make install          Install Python dependencies into .venv via uv"
	@echo "  make migrate          Apply Django migrations"
	@echo "  make seed             Populate 3 demo merchants with credit history"
	@echo "  make smoke            Run Day 1 smoke checks (balance + CHECK constraints)"
	@echo "  make dev              Run Django dev server with eager Celery (one process)"
	@echo "  make worker           Run a real Celery worker (needs Redis)"
	@echo "  make beat             Run celery-beat (scheduled retry sweep)"
	@echo "  make test             Run the two graded tests"
	@echo "  make test-fail-roll   Run tests forcing all payouts to fail (sanity)"
	@echo ""
	@echo "Frontend:"
	@echo "  make frontend-install Install JS dependencies"
	@echo "  make frontend-dev     Run the Vite dev server (proxies /api to Django)"
	@echo "  make frontend-build   Production-build the dashboard into frontend/dist"
	@echo ""
	@echo "Knobs:"
	@echo "  SETTLEMENT=$(SETTLEMENT)   < 0.7 success, < 0.9 fail, >= 0.9 hang"
	@echo "  PORT=$(PORT)"

install:
	uv sync

migrate:
	$(DJANGO) migrate

seed:
	uv run --project . python backend/seed.py

smoke:
	uv run --project . python backend/smoke_test.py

# One-process demo: dev server with Celery in eager mode. No worker/Redis needed.
dev:
	CELERY_EAGER=1 PAYOUT_SETTLEMENT_FORCE=$(SETTLEMENT) \
		$(DJANGO) runserver 127.0.0.1:$(PORT)

# Production-shaped runners (for live deploy + when you want to see the worker).
worker:
	cd backend && PAYOUT_SETTLEMENT_FORCE=$(SETTLEMENT) \
		uv run --project .. celery -A playto worker --loglevel=info

beat:
	cd backend && uv run --project .. celery -A playto beat --loglevel=info

test:
	cd backend && CELERY_EAGER=1 PAYOUT_SETTLEMENT_FORCE=0.0 \
		uv run --project .. python manage.py test tests --verbosity 2

test-fail-roll:
	cd backend && CELERY_EAGER=1 PAYOUT_SETTLEMENT_FORCE=0.8 \
		uv run --project .. python manage.py test tests --verbosity 2

shell:
	$(DJANGO) shell

frontend-install:
	cd frontend && npm install

frontend-dev:
	cd frontend && npm run dev

frontend-build:
	cd frontend && npm run build

clean:
	rm -rf .venv uv.lock frontend/node_modules frontend/dist
