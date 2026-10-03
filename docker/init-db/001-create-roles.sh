#!/bin/sh
set -eu

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=migration_role="$DB_MIGRATION_USER" \
  --set=migration_password="$DB_MIGRATION_PASSWORD" \
  --set=app_role="$DB_USER" \
  --set=app_password="$DB_PASSWORD" \
  --set=database_name="$POSTGRES_DB" <<'SQL'
CREATE ROLE :"migration_role" LOGIN PASSWORD :'migration_password';
CREATE ROLE :"app_role" LOGIN PASSWORD :'app_password';
GRANT CONNECT ON DATABASE :"database_name" TO :"migration_role";
GRANT CONNECT ON DATABASE :"database_name" TO :"app_role";
ALTER SCHEMA public OWNER TO :"migration_role";
GRANT USAGE ON SCHEMA public TO :"app_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_role";
SQL
