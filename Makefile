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

.PHONY: sql-plans
sql-plans: ## Before/after EXPLAIN (ANALYZE, BUFFERS) into docs/evidence/sql/
	$(UV) run python -m tests.tools.sql_plans --rows 2000000 --vehicles 5000

# Trivy runs in a container rather than as a local binary, so this works on a machine with
# only Docker installed. Three flags are load-bearing, and two of them cost a failed run to
# discover (see docs/evidence/security/README.md):
#   --skip-dirs         without it the secret scanner walks .venv and times out mid-file
#   --include-dev-deps  without it npm dev dependencies are skipped - here 318 of 364
#                       packages, and the build chain is exactly what STRIDE E6 is about
# Both scopes are written, because the default-scope report alone reads as covering far
# more than it does.
TRIVY_IMAGE ?= aquasec/trivy:latest
TRIVY_SKIP  ?= .venv,web/node_modules,.git,web/dist,**/__pycache__
TRIVY_RUN    = docker run --rm -v "$(CURDIR):/src" -v trivy-cache:/root/.cache $(TRIVY_IMAGE)

.PHONY: security
security: ## Dependency, secret and config scans into docs/evidence/security/
	@mkdir -p docs/evidence/security
	$(TRIVY_RUN) fs --quiet --scanners vuln,secret,misconfig \
		--severity CRITICAL,HIGH,MEDIUM --skip-dirs "$(TRIVY_SKIP)" \
		--format json --output /src/docs/evidence/security/trivy-fs.json /src
	$(TRIVY_RUN) fs --quiet --scanners vuln,secret,misconfig --include-dev-deps \
		--severity CRITICAL,HIGH,MEDIUM,LOW --skip-dirs "$(TRIVY_SKIP)" \
		--format json --output /src/docs/evidence/security/trivy-fs-with-dev.json /src
	$(UV) run python -m tests.tools.scan_summary

.PHONY: chaos
chaos: ## Kill a broker and a store, prove recovery AND that nothing was lost
	$(UV) run python -m tests.chaos.run --scenario all

# Terraform and Helm run in containers, so neither needs a local install - and neither needs
# cloud credentials. validate and fmt are credential-free; plan and apply are not and are out
# of scope (there is no AWS account). See infra/terraform/aws/README.md.
TF_IMAGE   ?= hashicorp/terraform:1.9
HELM_IMAGE ?= alpine/helm:3.16.2
TF_RUN      = docker run --rm -v "$(CURDIR)/infra/terraform/aws:/tf" -w /tf $(TF_IMAGE)
HELM_RUN    = docker run --rm -v "$(CURDIR)/infra/helm:/charts" -w /charts $(HELM_IMAGE)

.PHONY: tf-validate
tf-validate: ## Format-check, init and validate the Terraform; then scan it. No credentials.
	$(TF_RUN) fmt -check -recursive
	$(TF_RUN) init -backend=false -input=false
	$(TF_RUN) validate
	docker run --rm -v "$(CURDIR):/src" -v trivy-cache:/root/.cache $(TRIVY_IMAGE) 		config --quiet --severity CRITICAL,HIGH,MEDIUM /src/infra/terraform

.PHONY: helm-validate
helm-validate: ## Lint and render the charts, then schema-check the output
	$(HELM_RUN) lint fleet-platform
	$(HELM_RUN) lint fleet-platform -f fleet-platform/values-aws.yaml
	$(HELM_RUN) template fleet fleet-platform > /dev/null
	$(HELM_RUN) template fleet fleet-platform -f fleet-platform/values-aws.yaml > /dev/null

.PHONY: helm-deploy-local
helm-deploy-local: ## Install the charts on the local cluster (Docker Desktop Kubernetes)
	kubectl config current-context
	$(HELM_RUN) template fleet fleet-platform --set api.hpa.enabled=false > /tmp/fleet.yaml
	kubectl apply --dry-run=server -f /tmp/fleet.yaml

.PHONY: pin-digests
pin-digests: ## Resolve image tags in docker-compose.yml to pinned digests (CLAUDE.md S11)
	$(UV) run python -m tests.tools.pin_digests docker-compose.yml

.PHONY: check-digests
check-digests: ## Fail if any compose image is pinned by tag only. Runs in CI.
	$(UV) run python -m tests.tools.pin_digests docker-compose.yml --check

# --- Cloud ----------------------------------------------------------------

.PHONY: cloud-up
cloud-up: ## Provision the AWS environment (time-boxed, budget-capped)
	cd infra/terraform/aws && terraform init && terraform apply

.PHONY: cloud-down
cloud-down: ## Tear down the AWS environment. ALWAYS run this after a benchmark.
	cd infra/terraform/aws && terraform destroy -auto-approve
