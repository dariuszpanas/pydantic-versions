# pydantic-versions Makefile
# Core development commands for local work and CI parity.

.PHONY: all install format lint typecheck test test-cov check ci build clean help
.PHONY: docs-build docs-build-strict docs-serve

# Default target - run all checks
all: format lint typecheck test

# Install dependencies
install:
	uv sync

# Format code with Ruff
format:
	uv run ruff format .

# Lint code with Ruff (with auto-fix)
lint:
	uv run ruff check . --fix

# Type check with ty
typecheck:
	uv run ty check

# Run all tests
test:
	uv run pytest

# Run tests with coverage
test-cov:
	uv run pytest --cov=src --cov-report=html --cov-report=term

# Run lint and typecheck (no formatting)
check:
	uv run ruff check .
	uv run ty check

# CI check - all validations without modifications
ci:
	uv run ruff format --check .
	uv run ruff check .
	uv run ty check
	uv run pytest --cov=src --cov-report=xml --cov-report=term
	@echo "All CI checks passed!"

# Build the package
build:
	uv build

# Build docs
docs-build:
	uv run zensical build

# Build docs in strict mode (CI)
docs-build-strict:
	uv run zensical build --strict

# Serve docs locally at http://127.0.0.1:8000
docs-serve:
	uv run zensical serve --dev-addr 127.0.0.1:8000

# Clean up cache and build files
clean:
	python -c "import pathlib, shutil; [shutil.rmtree(path, ignore_errors=True) for path in map(pathlib.Path, ['.pytest_cache', '.ruff_cache', 'htmlcov', 'dist', 'build', 'site'])]; [path.unlink(missing_ok=True) for path in map(pathlib.Path, ['.coverage', 'coverage.xml'])]; [shutil.rmtree(path, ignore_errors=True) for path in pathlib.Path('.').rglob('__pycache__')]; [shutil.rmtree(path, ignore_errors=True) for path in pathlib.Path('src').glob('*.egg-info')]"

# Show help
help:
	@echo "pydantic-versions Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  install           - Install dependencies with uv"
	@echo ""
	@echo "Development:"
	@echo "  format            - Format code with Ruff"
	@echo "  lint              - Lint code with Ruff (auto-fix)"
	@echo "  typecheck         - Type check with ty"
	@echo "  check             - Run lint + typecheck (no formatting)"
	@echo ""
	@echo "Testing:"
	@echo "  test              - Run all tests"
	@echo "  test-cov          - Run tests with coverage"
	@echo "  docs-build        - Build Zensical site"
	@echo "  docs-build-strict - Build Zensical site (strict mode)"
	@echo "  docs-serve        - Serve docs locally at http://127.0.0.1:8000"
	@echo ""
	@echo "CI/CD:"
	@echo "  all               - Run format, lint, typecheck, test"
	@echo "  ci                - Run all checks (no modifications)"
	@echo "  build             - Build the package"
	@echo "  clean             - Clean cache and build files"
