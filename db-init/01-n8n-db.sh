#!/bin/sh
# create a second database for n8n inside the pipeline postgres instance
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE DATABASE n8n;
  GRANT ALL PRIVILEGES ON DATABASE n8n TO pipeline;
EOSQL
