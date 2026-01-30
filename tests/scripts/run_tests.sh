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

# Run tests with coverage
$PYTHON_CMD -m pytest tests/ \
    -v \
    --cov=api \
    --cov=core \
    --cov=services \
    --cov=workflows \
    --cov-report=term-missing \
    --cov-report=html \
    "$@"

echo ""
echo "✅ Tests complete!"
echo "Coverage report: htmlcov/index.html"
