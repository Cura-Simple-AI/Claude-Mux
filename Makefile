.PHONY: install install-dev test test-e2e coverage-e2e lint clean build publish help

PYTHON ?= python3

help:
	@echo "Heimsense — AI provider subscription manager"
	@echo ""
	@echo "  make install      Install from PyPI"
	@echo "  make install-dev  Editable install for development"
	@echo "  make test         Run test suite"
	@echo "  make test-e2e     Run end-to-end suite (CLI/proxy processes, TUI via Pilot)"
	@echo "  make coverage-e2e End-to-end suite with coverage (incl. subprocesses)"
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

# The E2E suite isolates HOME before claude_mux is imported, so it must run
# in its own pytest process (not together with tests/).
test-e2e:
	$(PYTHON) -m pytest e2e -q

coverage-e2e:
	rm -f .coverage-e2e*
	COVERAGE_FILE=$(CURDIR)/.coverage-e2e $(PYTHON) -m coverage run --rcfile=e2e/coveragerc -m pytest e2e -q
	COVERAGE_FILE=$(CURDIR)/.coverage-e2e $(PYTHON) -m coverage combine --rcfile=e2e/coveragerc -q
	COVERAGE_FILE=$(CURDIR)/.coverage-e2e $(PYTHON) -m coverage report --rcfile=e2e/coveragerc

test-fast:
	$(PYTHON) -m pytest tests/ -q

lint:
	$(PYTHON) -m ruff check heimsense/ tests/ || true

clean:
	rm -rf dist/ build/ *.egg-info __pycache__ .pytest_cache .coverage-e2e*
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete

build: clean
	$(PYTHON) -m build

publish: build
	$(PYTHON) -m twine upload dist/*
