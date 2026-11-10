.PHONY: help preflight tools up down test lint

help:
	@echo "make preflight  check the machine"
	@echo "make tools      download pinned CLI tools into .bin/"
	@echo "make up         create the cluster and install everything (idempotent)"
	@echo "make down       delete the cluster and the local registry"
	@echo "make test       unit tests"
	@echo "make test-integration  RBAC and admission checks against the cluster"
	@echo "make lint       ruff + mypy"

preflight:
	@infra/preflight.sh

tools:
	@infra/tools.sh

up:
	@infra/bootstrap.sh

down:
	@infra/teardown.sh

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

.PHONY: test-integration
test-integration:
	@infra/admission/tests/run.sh
