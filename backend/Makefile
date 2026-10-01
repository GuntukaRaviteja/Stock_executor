VENV       := .venv
PYTHON     := $(VENV)/bin/python
PIP        := $(VENV)/bin/pip
UVICORN    := $(VENV)/bin/uvicorn
PYTEST     := $(VENV)/bin/pytest
ALEMBIC    := $(VENV)/bin/alembic

.PHONY: help venv install run dev test seed ingest migrate db-check clean

# ── Default ──────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "Stock Intelligence API — Phase 2 Commands"
	@echo "==========================================="
	@echo "  make install      Install all dependencies into .venv"
	@echo "  make run          Start server (production mode)"
	@echo "  make dev          Start server (auto-reload, development mode)"
	@echo "  make test         Run all pytest tests"
	@echo "  make migrate      Apply pending Alembic migrations"
	@echo "  make seed         Seed stocks + company profiles"
	@echo "  make ingest       Run market data ingestion pipeline"
	@echo "  make db-check     Quick database connectivity check"
	@echo "  make clean        Remove __pycache__ and .pyc files"
	@echo ""

# ── Environment ──────────────────────────────────────────────────────────────
venv:
	@echo "Creating virtual environment with Python 3.12..."
	/opt/homebrew/bin/python3.12 -m venv $(VENV)
	@echo "Virtual environment created at $(VENV)/"

install: venv
	@echo "Installing dependencies..."
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@echo "Dependencies installed."

# ── Server ───────────────────────────────────────────────────────────────────
run:
	$(UVICORN) app.main:app --host 0.0.0.0 --port 8000

dev:
	$(UVICORN) app.main:app --host 0.0.0.0 --port 8000 --reload

# ── Tests ─────────────────────────────────────────────────────────────────────
test:
	$(PYTEST) tests/ -v

# ── Database ─────────────────────────────────────────────────────────────────
migrate:
	$(ALEMBIC) upgrade head

db-check:
	$(PYTHON) -c "from app.core.database import check_database_health; ok, msg = check_database_health(); print(f'Database: {msg}')"

# ── Data Scripts ──────────────────────────────────────────────────────────────
seed:
	$(PYTHON) scripts/seed_stocks.py

ingest:
	$(PYTHON) scripts/ingest_market_data.py

# ── Cleanup ───────────────────────────────────────────────────────────────────
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "Cleaned up."
