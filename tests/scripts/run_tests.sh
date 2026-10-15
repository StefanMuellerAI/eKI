#!/bin/bash
# Run tests for eKI API

set -e

echo "=== eKI API Test Runner ==="
echo ""

# Check if we're in Docker or local
if [ -f "/.dockerenv" ]; then
    echo "Running in Docker container"
    PYTHON_CMD="python"
else
    echo "Running locally"
    # Try to find Python in venv
    if [ -d "venv/bin" ]; then
        PYTHON_CMD="venv/bin/python"
    elif [ -d ".venv/bin" ]; then
        PYTHON_CMD=".venv/bin/python"
    else
        PYTHON_CMD="python"
    fi
fi

echo "Using: $PYTHON_CMD"
echo ""

# Install dev dependencies if needed
echo "Installing dev dependencies..."
$PYTHON_CMD -m pip install -e ".[dev]" -q

echo ""
echo "Running tests..."
echo ""

# Run tests with coverage. The covered packages come from pyproject.toml
# (addopts). Optional:
#   TEST_POSTGRES_URL=postgresql+asyncpg://eki_user:eki_password@localhost:5432/eki_test
#     -> whole suite + tests/test_pg_integration.py against real PostgreSQL/pgvector
#        (the database is wiped at session start -- use a throwaway DB!)
#   TEST_REDIS_URL=redis://localhost:6379/4
#     -> real Redis for tests/test_e2e_pipeline.py (that Redis DB is flushed)
#   -m "not temporal"  -> skip tests that need the Temporal test server download
$PYTHON_CMD -m pytest tests/ \
    -v \
    --cov-report=term-missing \
    --cov-report=html \
    "$@"

echo ""
echo "✅ Tests complete!"
echo "Coverage report: htmlcov/index.html"
