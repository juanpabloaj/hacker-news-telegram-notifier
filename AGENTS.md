# Repository Guidelines

## Project Structure & Module Organization
- `hn_notifier.py`: main service entrypoint and core logic (HN polling, SQLite state, Telegram delivery).
- `state.db`: runtime SQLite state (generated locally/in server runtime, not source code).
- `requirements.txt` and `pyproject.toml`: Python dependency definitions (`pip` and `uv` workflows).
- `systemd/hn-notifier.service`: generic systemd unit template for production deployments.
- `Dockerfile` and `docker-compose.yml`: containerized runtime.
- `README.md`: setup, runtime, and deployment instructions.

## Build, Test, and Development Commands
- `python3 -m py_compile hn_notifier.py`: fast syntax validation.
- `uv run python hn_notifier.py`: run service with `uv`-managed environment.
- `python3 -m venv .venv && source .venv/bin/activate && python -m pip install -r requirements.txt`: alternative local setup with `pip`.
- `docker compose up -d --build`: build and run containerized service.
- `docker compose logs -f`: follow container logs.

## Coding Style & Naming Conventions
- Language: Python 3.10+.
- Follow PEP 8: 4-space indentation, clear function names, and small focused functions.
- Use `snake_case` for variables/functions, `PascalCase` for classes, and UPPER_CASE for constants.
- Keep comments minimal and purposeful; explain non-obvious logic only.
- Keep code and comments in English.

## Testing Guidelines
- Current project has no formal test suite yet.
- Minimum validation before commit:
  - `python3 -m py_compile hn_notifier.py`
  - manual smoke test with valid `.env` and log inspection.
- When adding tests, prefer `pytest` with files named `test_*.py` in a `tests/` directory.

## Commit & Pull Request Guidelines
- Use short, imperative commit messages (seen in history), e.g.:
  - `Fix FK compatibility for legacy SQLite state`
  - `Improve notifier state handling across restarts`
- PRs should include:
  - clear summary of behavior changes,
  - any config/migration impact (`state.db`, `.env`, service files),
  - verification steps and key log output.

## Security & Configuration Tips
- Never commit secrets (`.env`, bot tokens, chat IDs).
- Keep `.env.example` updated when adding new environment variables.
- For production, run via systemd and verify restart/OnFailure behavior with `journalctl`.
