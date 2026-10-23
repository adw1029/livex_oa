.PHONY: install run test lint format deploy

install:  ## Create .venv and install app + dev dependencies
	python3 -m venv .venv
	.venv/bin/pip install -q --upgrade pip
	.venv/bin/pip install -q --group dev

run:      ## Start the app at http://localhost:8000 (reads .env)
	.venv/bin/uvicorn app.main:app --reload --port 8000

test:     ## Run the test suite (no network or API keys needed)
	.venv/bin/python -m pytest -q

lint:     ## Check style and common mistakes
	.venv/bin/ruff check . && .venv/bin/ruff format --check .

format:   ## Auto-format the code
	.venv/bin/ruff check --fix . && .venv/bin/ruff format .

deploy:   ## Deploy to AWS (CloudFront + S3 + Lambda); see README
	./infra/deploy.sh
