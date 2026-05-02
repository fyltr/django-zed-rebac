# Contributing

## Local setup

- Python 3.11+
- `uv` installed

```bash
uv pip install -e .[dev]
```

## Canonical commands

```bash
make lint
make test
make check
```

## Test and lint policy

- Run lint + tests before opening a PR.
- Migrations under `tests/testapp/migrations/` are generated fixtures. We do not require import sorting or mutable-class-attribute lint rules there.
- Prefer regenerating test migrations when model shape changes, rather than hand-editing generated structures.

## CI matrix

CI validates:
- Python 3.11, 3.12, 3.13
- Django 4.2 and 5.2

## Commit hygiene

- Keep changes focused and atomic.
- Add/update tests for behavior changes.
