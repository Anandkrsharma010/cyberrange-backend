#!/bin/sh
set -e

echo "Starting database initialization..."
python initialize_db.py

echo "Starting CyberRange API Server..."
exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}
