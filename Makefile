.PHONY: all build test test-unit test-integration test-e2e install clean allure allure-serve

all: build

# Install dependencies
install:
	pip install -e ".[dev]"

# Build standalone binary
build:
	python -m PyInstaller --onefile --name harbour entry_harbour.py

# Run all tests
test:
	pytest tests/

# Run only fast tests (no network)
test-unit:
	pytest tests/unit

# Run integration tests
test-integration:
	pytest tests/integration

# Run e2e tests (needs npx)
test-e2e:
	pytest tests/e2e

# Run the suite + the smoke scenario, emitting Allure results, then open report
# (needs Node for the Allure 3 CLI: `npm install -g allure`)
allure:
	rm -rf allure-results
	pytest tests/unit tests/integration tests/e2e --alluredir=allure-results || true
	python tests/smoke/scenario.py serve-check --alluredir allure-results --allure-name "smoke serve-check"
	npx allure generate allure-results -c allurerc.mjs

# Run the suite and serve a live Allure report locally
allure-serve:
	rm -rf allure-results
	pytest tests/unit tests/integration --alluredir=allure-results || true
	npx allure serve allure-results

# Clean build artifacts
clean:
	-rm -r build/
	-rm -r dist/
	-rm -r *.spec
	-rm -r allure-results/
	-rm -r allure-report/
	-rm -f history.jsonl
