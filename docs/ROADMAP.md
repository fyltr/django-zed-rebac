# ROADMAP (Code Review Follow-ups)

This document captures **technical-debt and best-practice follow-ups** identified during a repository review.

## Priority 0 — Quality gates and CI hygiene

- [x] Add a default local/CI bootstrap (`make test` or `just test`) that installs required test deps before running checks.
  - Why: current test run fails in a fresh environment because `django` is not installed.
  - Outcome: contributors get deterministic one-command validation.

- [x] Tighten lint scope so tooling rules are intentional for generated Django migrations.
  - Why: Ruff currently flags test migrations for import sorting and mutable class attributes; those files are framework-generated and often should be excluded from stylistic rewrites.
  - Outcome: less noise, fewer false-positive failures, clearer signal in CI.

- [x] Add a CI matrix for Python/Django versions already declared in metadata.
  - Why: compatibility is documented broadly, but repository checks should continuously verify claims.
  - Outcome: early detection of version-specific regressions.

## Priority 1 — Packaging and repository cleanliness

- [x] Remove committed `src/django_zed_rebac.egg-info/*` from source control and add ignore rules.
  - Why: egg-info artifacts are build outputs and can drift from source of truth (`pyproject.toml`).
  - Outcome: cleaner diffs and fewer accidental release metadata mismatches.

- [x] Add `MANIFEST.in` (or explicit setuptools config) review to ensure package data is deliberate.
  - Why: the project depends on schema/runtime files and typed marker (`py.typed`); packaging should be explicit and tested.
  - Outcome: predictable wheels/sdists.

## Priority 2 — Django best-practice hardening

- [x] Add system checks validating required middleware ordering when `ActorMiddleware` is enabled.
  - Why: docs say it must be after `AuthenticationMiddleware`; an automated check prevents subtle runtime behavior bugs.
  - Outcome: safer integration by default.

- [x] Add explicit tests for settings cache invalidation (`setting_changed`) behavior.
  - Why: `app_settings` caches values; cache invalidation is critical to predictable tests and runtime overrides.
  - Outcome: protects against regressions in config behavior.

- [x] Replace internal parser dependency (`_Parser`) usage with a stable public parser API abstraction.
  - Why: relying on a private symbol creates refactor risk and hidden coupling.
  - Outcome: easier maintenance and safer parser evolution.

## Priority 3 — Documentation debt reduction

- [x] Add a dedicated `CONTRIBUTING.md` with required toolchain, setup steps, and canonical commands.
  - Why: repo has strong architecture docs but contributor workflow is implicit.
  - Outcome: faster onboarding and fewer environment-specific failures.

- [x] Document lint/test policy for migrations and generated files.
  - Why: teams need explicit guidance on when to regenerate vs manually edit migration files in tests.
  - Outcome: consistent review expectations.
