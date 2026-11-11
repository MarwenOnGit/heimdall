.PHONY: help preflight tools up down reset test lint

help:
	@echo "make preflight  check the machine"
	@echo "make tools      download pinned CLI tools into .bin/"
	@echo "make up         create the cluster and install everything (idempotent)"
	@echo "make reset      return the running cluster to the baseline state"
	@echo "make down       delete the cluster and the local registry"
	@echo "make test       unit tests and policy tests"
	@echo "make test-integration  RBAC and admission checks against the cluster"
	@echo "make lint       ruff + mypy"

preflight:
	@infra/preflight.sh

tools:
	@infra/tools.sh

up:
	@infra/bootstrap.sh

reset:
	@infra/reset.sh

down:
	@infra/teardown.sh

test: tools
	uv run pytest
	.bin/opa test heimdall/policy/policies/

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	.bin/opa fmt --fail --list heimdall/policy/policies/

.PHONY: test-integration
test-integration:
	@infra/admission/tests/run.sh
