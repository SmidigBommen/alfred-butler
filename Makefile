PYTHON ?= python3
PODMAN ?= podman
RUFF ?= ruff
UV ?= uv
CODEX_HOME ?= $(HOME)/.codex
ALFRED_CONFIG_DIR ?= $(HOME)/.config/alfred
ALFRED_VOICE_MARKER := $(ALFRED_CONFIG_DIR)/voice.enabled

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
ALFRED_WEATHER_SKILL_SOURCE := $(abspath skills/alfred-weather)
ALFRED_WEATHER_SKILL_DESTINATION := $(CODEX_HOME)/skills/alfred-weather

.PHONY: install-agent install-skills install-voice serve check lint format test search-up search-down search-restart search-status search-logs search-health

install-agent: install-skills
	@if test -f $(ALFRED_VOICE_MARKER); then \
		$(UV) tool install --force --editable '.[voice]'; \
	else \
		$(UV) tool install --force --editable .; \
	fi

install-skills:
	PYTHONPATH=src $(PYTHON) -m alfred_tools.install_skill \
		--source $(ALFRED_WEB_SKILL_SOURCE) \
		--destination $(ALFRED_WEB_SKILL_DESTINATION)
	PYTHONPATH=src $(PYTHON) -m alfred_tools.install_skill \
		--source $(ALFRED_MEMORY_SKILL_SOURCE) \
		--destination $(ALFRED_MEMORY_SKILL_DESTINATION)
	PYTHONPATH=src $(PYTHON) -m alfred_tools.install_skill \
		--source $(ALFRED_WEATHER_SKILL_SOURCE) \
		--destination $(ALFRED_WEATHER_SKILL_DESTINATION)

install-voice: install-skills
	$(UV) tool install --force --editable '.[voice]'
	install -D -m 600 /dev/null $(ALFRED_VOICE_MARKER)

serve:
	alfred-serve

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
