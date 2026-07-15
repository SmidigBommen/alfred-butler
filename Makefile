PYTHON ?= python3
PODMAN ?= podman
RUFF ?= ruff
UV ?= uv
CODEX_HOME ?= $(HOME)/.codex

SEARXNG_IMAGE := docker.io/searxng/searxng@sha256:1a196e52ef0aec52a462667e5c54030840f94865c13e1260004caa10cca6be49
SEARXNG_CONTAINER := alfred-searxng
SEARXNG_DIR := infra/searxng
SEARXNG_CONFIG := $(abspath $(SEARXNG_DIR)/settings.yml)
SEARXNG_LIMITER_CONFIG := $(abspath $(SEARXNG_DIR)/limiter.toml)
SEARXNG_ENV := $(abspath $(SEARXNG_DIR)/.env)
ALFRED_WEB_SKILL_SOURCE := $(abspath skills/alfred-search-web)
ALFRED_WEB_SKILL_DESTINATION := $(CODEX_HOME)/skills/alfred-search-web
ALFRED_MEMORY_SKILL_SOURCE := $(abspath skills/alfred-personal-memory)
ALFRED_MEMORY_SKILL_DESTINATION := $(CODEX_HOME)/skills/alfred-personal-memory

.PHONY: install-agent check lint format test search-up search-down search-restart search-status search-logs search-health

install-agent:
	$(UV) tool install --force --editable .
	PYTHONPATH=src $(PYTHON) -m alfred_tools.install_skill \
		--source $(ALFRED_WEB_SKILL_SOURCE) \
		--destination $(ALFRED_WEB_SKILL_DESTINATION)
	PYTHONPATH=src $(PYTHON) -m alfred_tools.install_skill \
		--source $(ALFRED_MEMORY_SKILL_SOURCE) \
		--destination $(ALFRED_MEMORY_SKILL_DESTINATION)

check: lint test

lint:
	$(RUFF) check src tests
	$(RUFF) format --check src tests

format:
	$(RUFF) check --fix src tests
	$(RUFF) format src tests

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -v

search-up:
	test -f $(SEARXNG_ENV)
	$(PODMAN) run --detach --replace \
		--name $(SEARXNG_CONTAINER) \
		--publish 127.0.0.1:8888:8080 \
		--env-file $(SEARXNG_ENV) \
		--volume $(SEARXNG_CONFIG):/etc/searxng/settings.yml:ro,Z \
		--volume $(SEARXNG_LIMITER_CONFIG):/etc/searxng/limiter.toml:ro,Z \
		--volume alfred-searxng-cache:/var/cache/searxng:Z \
		$(SEARXNG_IMAGE)

search-down:
	$(PODMAN) rm --force --ignore $(SEARXNG_CONTAINER)

search-restart: search-down search-up

search-status:
	$(PODMAN) ps --all --filter name=$(SEARXNG_CONTAINER)

search-logs:
	$(PODMAN) logs $(SEARXNG_CONTAINER)

search-health:
	curl --fail --silent --show-error --header "X-Real-IP: 127.0.0.1" http://127.0.0.1:8888/healthz
