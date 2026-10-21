.PHONY: install run test lint deploy

install:  ## Create .venv and install app + dev dependencies
	python3 -m venv .venv
	.venv/bin/pip install -q -r requirements-dev.txt

run:      ## Start the app at http://localhost:8000 (reads .env)
	.venv/bin/uvicorn app.main:app --reload --port 8000

test:     ## Run the test suite (no network or API keys needed)
	.venv/bin/python -m pytest -q

lint:
	.venv/bin/python -m pyflakes app calcom_mcp tests

deploy:   ## Deploy to AWS (CloudFront + S3 + Lambda); see README
	./infra/deploy.sh
