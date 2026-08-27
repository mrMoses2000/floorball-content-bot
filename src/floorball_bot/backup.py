from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlsplit

LIBPQ_QUERY_ENV = {
    "sslmode": "PGSSLMODE",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "application_name": "PGAPPNAME",
}


def postgres_environment(dsn: str, base: dict[str, str] | None = None) -> dict[str, str]:
    parts = urlsplit(dsn)
    if parts.scheme not in {"postgres", "postgresql"}:
        raise ValueError("POSTGRES_DSN must use postgres/postgresql")
    database = unquote(parts.path.lstrip("/"))
    if not database:
        raise ValueError("POSTGRES_DSN must include a database")
    env = dict(base or {})
    env["PGDATABASE"] = database
    if parts.hostname:
        env["PGHOST"] = parts.hostname
    if parts.port:
        env["PGPORT"] = str(parts.port)
    if parts.username:
        env["PGUSER"] = unquote(parts.username)
    if parts.password:
        env["PGPASSWORD"] = unquote(parts.password)
    query = parse_qs(parts.query, keep_blank_values=False)
    for parameter, variable in LIBPQ_QUERY_ENV.items():
        if query.get(parameter):
            env[variable] = query[parameter][-1]
    return env
