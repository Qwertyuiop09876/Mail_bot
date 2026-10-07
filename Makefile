PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: install lint format typecheck test check migrate-new clean

install:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -U pip
	$(BIN)/pip install -e ".[dev]"
	$(BIN)/pre-commit install

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck:
	$(BIN)/mypy

test:
	$(BIN)/pytest --cov

# Всё то же, что гоняет CI
check: lint typecheck test

# make migrate-new MSG="add something"
migrate-new:
	MAILBOT_DATABASE_URL=sqlite:///data/_migrate.db $(BIN)/alembic upgrade head
	MAILBOT_DATABASE_URL=sqlite:///data/_migrate.db $(BIN)/alembic revision --autogenerate -m "$(MSG)"
	rm -f data/_migrate.db*

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist build
