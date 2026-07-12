"""Pre-deployment and service-start verification for Thai 2D."""

from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_ENVIRONMENT = (
    "DATABASE_PATH",
    "STALE_AFTER_SECONDS",
    "ALLOWED_ORIGINS",
    "MORNING_TARGET",
    "EVENING_TARGET",
    "FETCH_INTERVAL_SECONDS",
    "MORNING_WINDOW_START",
    "MORNING_WINDOW_END",
    "EVENING_WINDOW_START",
    "EVENING_WINDOW_END",
    "HEADLESS",
)


class CheckFailure(RuntimeError):
    pass


def load_environment() -> dict[str, str]:
    file_values = dotenv_values(PROJECT_ROOT / ".env")
    values = {key: value for key, value in file_values.items() if value is not None}
    values.update(os.environ)
    return values


def check_python() -> None:
    if sys.version_info < (3, 12):
        raise CheckFailure("Python 3.12 or newer is required")


def check_environment(environment: dict[str, str]) -> None:
    missing = [name for name in REQUIRED_ENVIRONMENT if name not in environment]
    if missing:
        raise CheckFailure(f"Missing environment variables: {', '.join(missing)}")
    if not environment["DATABASE_PATH"].strip():
        raise CheckFailure("DATABASE_PATH must not be empty")
    database_path = Path(environment["DATABASE_PATH"])
    if not database_path.is_absolute():
        raise CheckFailure("DATABASE_PATH must be absolute")


def check_database(environment: dict[str, str], mode: str) -> None:
    database_path = Path(environment["DATABASE_PATH"])
    directory = database_path.parent
    if not directory.exists() or not directory.is_dir():
        raise CheckFailure(f"Database directory does not exist: {directory}")
    if mode in {"collector", "all"} and not os.access(directory, os.W_OK | os.X_OK):
        raise CheckFailure(f"Collector cannot write database directory: {directory}")
    if mode in {"api", "all"}:
        if not database_path.is_file():
            raise CheckFailure(f"API database file does not exist: {database_path}")
        if not os.access(database_path, os.R_OK):
            raise CheckFailure(f"API cannot read database file: {database_path}")


def check_chromium() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        executable = Path(playwright.chromium.executable_path)
    if not executable.is_file():
        raise CheckFailure(f"Playwright Chromium executable is missing: {executable}")


def check_imports() -> None:
    importlib.import_module("api")
    importlib.import_module("scheduled_collector")


def run_tests() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise CheckFailure(f"Test suite failed with exit code {result.returncode}")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("api", "collector", "all"), default="all")
    parser.add_argument("--skip-tests", action="store_true")
    return parser.parse_args()


def main() -> None:
    options = arguments()
    try:
        environment = load_environment()
        for name, value in environment.items():
            os.environ.setdefault(name, value)
        check_python()
        check_environment(environment)
        check_database(environment, options.mode)
        if options.mode in {"collector", "all"}:
            check_chromium()
        check_imports()
        if not options.skip_tests:
            run_tests()
    except CheckFailure as exc:
        print(f"predeploy_check: FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"predeploy_check: OK (mode={options.mode})")


if __name__ == "__main__":
    main()
