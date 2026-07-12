"""Verify a container role, then replace this process with its service."""

from __future__ import annotations

import os
import subprocess
import sys


COMMANDS = {
    "api": [
        sys.executable,
        "-m",
        "uvicorn",
        "api:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--workers",
        "1",
        "--no-access-log",
    ],
    "collector": [sys.executable, "scheduled_collector.py"],
}


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        roles = ", ".join(COMMANDS)
        raise SystemExit(f"usage: container_entrypoint.py <{roles}>")
    role = sys.argv[1]
    subprocess.run(
        [sys.executable, "deploy/predeploy_check.py", "--mode", role, "--skip-tests"],
        check=True,
    )
    command = COMMANDS[role]
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
