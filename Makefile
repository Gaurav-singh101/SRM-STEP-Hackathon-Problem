# ---------------------------------------------------------------------------
# Single entry point for the project (CLAUDE.md S13 / S19).
#
# Windows note: `make` is not present on a stock Windows install. Use the
# equivalent shim from PowerShell - `./make.ps1 <target>` - which runs the same
# commands. CI runs on Linux and uses this Makefile directly.
# ---------------------------------------------------------------------------

.DEFAULT_GOAL := help
SHELL := /bin/bash

COMPOSE := docker compose
UV      := uv
# Coverage gate applies to libs + domain + application only (ruling G7);
# adapters are measured but report-only, so the gate cannot be gamed.
COV_MIN := 80

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Local stack ----------------------------------------------------------

.PHONY: up
up: .env ## Start the full local stack and seed reference data
	$(COMPOSE) --profile core up -d --wait
	$(MAKE) topics
	$(MAKE) migrate
	@echo "Stack up. API http://localhost:8000/docs  Web http://localhost:5173"

.PHONY: down
down: ## Stop the stack and remove volumes
	$(COMPOSE) --profile core --profile obs --profile load down -v --remove-orphans

.PHONY: logs
logs: ## Tail logs from every service
	$(COMPOSE) logs -f --tail=100

.PHONY: ps
ps: ## Show container status
	$(COMPOSE) ps

.env: .env.example
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example - review it before running.")

# --- Schema and data ------------------------------------------------------

.PHONY: topics
topics: ## Create Kafka topics with the partition counts from ADR-0004
	$(UV) run python -m processor.admin create-topics

.PHONY: migrate
migrate: ## Apply Postgres migrations
	$(UV) run python -m batch.migrate up

.PHONY: seed
seed: ## Seed the reference dataset (tenants, fleets, vehicles, tariffs)
	$(UV) run python -m batch.seed --vehicles $${VEHICLES:-100000}

.PHONY: sim
sim: ## Run the simulator. Override with: make sim N=100000
	$(UV) run python -m simulator --vehicles $${N:-2000}

# --- Quality gates --------------------------------------------------------

.PHONY: install
install: ## Sync the pinned Python workspace and web dependencies
	$(UV) sync --all-packages
	cd web && npm ci

.PHONY: lint
lint: ## ruff, mypy, eslint, tsc
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy
	cd web && npm run lint && npm run typecheck

.PHONY: fmt
fmt: ## Auto-format Python and web sources
	$(UV) run ruff check --fix .
	$(UV) run ruff format .
	cd web && npm run format

.PHONY: test
test: ## Unit tests with the coverage gate, then web tests
	$(UV) run pytest -m "not integration" --cov --cov-report=term-missing \
		--cov-report=xml:docs/evidence/coverage/coverage.xml --cov-fail-under=$(COV_MIN)
	cd web && npm run test -- --run

.PHONY: test-unit
test-unit: ## Unit tests only, no infrastructure needed
	$(UV) run pytest -m "not integration" --cov --cov-fail-under=$(COV_MIN)

.PHONY: test-integration
test-integration: ## Integration tests against real infrastructure (Testcontainers)
	$(UV) run pytest -m integration

.PHONY: test-acceptance
test-acceptance: ## BDD acceptance scenarios
	$(UV) run behave

.PHONY: verify
verify: lint test test-acceptance ## Everything CI enforces, in one command

# --- Evidence -------------------------------------------------------------

.PHONY: load
load: ## Run the load scenario and write results to docs/evidence/load/
	$(UV) run python -m tests.load.run_scenario

.PHONY: security
security: ## SAST, dependency and image scans into docs/evidence/security/
	$(UV) run semgrep --config=auto --sarif --output=docs/evidence/security/semgrep.sarif || true
	trivy fs --scanners vuln,secret --format json \
		--output docs/evidence/security/trivy-fs.json . || true

.PHONY: chaos
chaos: ## Kill a broker and a consumer, prove recovery
	$(UV) run python -m tests.chaos.run

.PHONY: pin-digests
pin-digests: ## Resolve image tags in docker-compose.yml to pinned digests (CLAUDE.md S11)
	$(UV) run python -m tests.tools.pin_digests docker-compose.yml

# --- Cloud ----------------------------------------------------------------

.PHONY: cloud-up
cloud-up: ## Provision the AWS environment (time-boxed, budget-capped)
	cd infra/terraform/aws && terraform init && terraform apply

.PHONY: cloud-down
cloud-down: ## Tear down the AWS environment. ALWAYS run this after a benchmark.
	cd infra/terraform/aws && terraform destroy -auto-approve
