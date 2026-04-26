.PHONY: install install-dev test lint clean build publish help

PYTHON ?= python3

help:
	@echo "Heimsense — AI provider subscription manager"
	@echo ""
	@echo "  make install      Install from PyPI"
	@echo "  make install-dev  Editable install for development"
	@echo "  make test         Run test suite"
	@echo "  make lint         Run ruff linter"
	@echo "  make clean        Remove build artifacts"
	@echo "  make build        Build distribution packages"
	@echo "  make publish      Publish to PyPI (requires PYPI_TOKEN)"

install:
	$(PYTHON) -m pip install heimsense

install-dev:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest tests/ -v

test-fast:
	$(PYTHON) -m pytest tests/ -q

lint:
	$(PYTHON) -m ruff check heimsense/ tests/ || true

clean:
	rm -rf dist/ build/ *.egg-info __pycache__ .pytest_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete

build: clean
	$(PYTHON) -m build

publish: build
	$(PYTHON) -m twine upload dist/*
