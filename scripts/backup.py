#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from floorball_bot.backup import postgres_environment
from floorball_bot.config import Settings


def main() -> None:
    settings = Settings()
    env = postgres_environment(settings.postgres_dsn.get_secret_value(), dict(os.environ))
    env["BACKUP_ROOT"] = str(settings.backup_root)
    env["MEDIA_ROOT"] = str(settings.media_root)
    script = Path(__file__).with_name("backup.sh")
    subprocess.run([str(script)], check=True, env=env)  # noqa: S603


if __name__ == "__main__":
    main()
