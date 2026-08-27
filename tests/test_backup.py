from floorball_bot.backup import postgres_environment
from floorball_bot.config import Settings


def test_backup_converts_dsn_to_libpq_environment_without_argv_secret():
    dsn = "postgresql://floorball:p%40ss@127.0.0.1:5433/content?sslmode=require"

    env = postgres_environment(dsn, {"PATH": "/usr/bin"})

    assert env == {
        "PATH": "/usr/bin",
        "PGDATABASE": "content",
        "PGHOST": "127.0.0.1",
        "PGPORT": "5433",
        "PGUSER": "floorball",
        "PGPASSWORD": "p@ss",
        "PGSSLMODE": "require",
    }
    assert "p@ss" not in str(["scripts/backup.sh"])


def test_runtime_paths_are_absolute():
    settings = Settings(_env_file=None)

    assert settings.media_root.is_absolute()
    assert settings.backup_root.is_absolute()
