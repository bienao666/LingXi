#!/bin/bash
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=password="$HINDSIGHT_DB_PASSWORD" <<'SQL'
CREATE USER hindsight WITH PASSWORD :'password';
CREATE DATABASE hindsight OWNER hindsight;
REVOKE CONNECT ON DATABASE lingxi FROM PUBLIC;
REVOKE CONNECT ON DATABASE hindsight FROM PUBLIC;
GRANT CONNECT ON DATABASE hindsight TO hindsight;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname hindsight <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgroonga;
SQL
