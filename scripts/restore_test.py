#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from floorball_bot.backup import postgres_environment
from floorball_bot.config import Settings


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: restore_test.py /path/to/database.dump")
    dump = Path(sys.argv[1]).resolve(strict=True)
    settings = Settings()
    env = postgres_environment(settings.postgres_dsn.get_secret_value(), dict(os.environ))
    source_database = env["PGDATABASE"]
    env["TEST_RESTORE_DATABASE"] = f"{source_database}_restore_test"
    env["RESTORE_REUSE_DATABASE"] = "true"
    script = Path(__file__).with_name("restore-test.sh")
    subprocess.run([str(script), str(dump)], check=True, env=env)  # noqa: S603


if __name__ == "__main__":
    main()
