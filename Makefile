COMPOSE := docker compose
COMPOSE_FILE := srcs/docker-compose.yml
ENV_FILE ?= .env

COMPOSE_RUN := $(COMPOSE) --env-file $(ENV_FILE) -f $(COMPOSE_FILE)

.PHONY: up down build logs ps clean fclean test config

up:
	$(COMPOSE_RUN) up -d

down:
	$(COMPOSE_RUN) down --remove-orphans

build:
	$(COMPOSE_RUN) build

logs:
	$(COMPOSE_RUN) logs -f

ps:
	$(COMPOSE_RUN) ps

clean: down

fclean:
	$(COMPOSE_RUN) down -v --rmi local --remove-orphans

config:
	$(COMPOSE_RUN) config

test:
	python3 tests/validate_stack.py
	@if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then \
		$(COMPOSE) --env-file .env.example -f $(COMPOSE_FILE) config >/dev/null; \
		echo "docker compose config passed"; \
	else \
		echo "docker compose not available; skipped compose config"; \
	fi
