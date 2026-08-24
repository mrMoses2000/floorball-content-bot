#!/usr/bin/env bash
set -euo pipefail

: "${TEST_RESTORE_DATABASE:?TEST_RESTORE_DATABASE is required}"
if [[ ! "$TEST_RESTORE_DATABASE" =~ _restore_test$ ]]; then
    printf 'Refusing to touch a database not ending in _restore_test.\n' >&2
    exit 64
fi
: "${1:?Usage: restore-test.sh /path/to/database.dump}"

dropdb --if-exists "$TEST_RESTORE_DATABASE"
createdb "$TEST_RESTORE_DATABASE"
pg_restore --exit-on-error --no-owner --dbname="$TEST_RESTORE_DATABASE" "$1"
psql --dbname="$TEST_RESTORE_DATABASE" --set=ON_ERROR_STOP=1 \
    --command='SELECT count(*) AS migrations FROM schema_migrations;'

