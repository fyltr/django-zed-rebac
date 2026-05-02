.PHONY: install-dev test lint typecheck check ci

install-dev:
	uv pip install -e .[dev]

test:
	uv run pytest -q

lint:
	uv run ruff check .

typecheck:
	uv run mypy src

check: lint test

ci: install-dev check
