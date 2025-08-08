COMPOSE := docker compose
COMPOSE_FILE := srcs/docker-compose.yml
ENV_FILE ?= .env
PROJECT_NAME ?= container-stack
WAIT_TIMEOUT ?= 300
BACKUP_DIR ?=
NEW_SECRETS_DIR ?=

COMPOSE_RUN := $(COMPOSE) --project-name $(PROJECT_NAME) --env-file $(ENV_FILE) -f $(COMPOSE_FILE)

.PHONY: up start-database start-application down build logs ps clean fclean test config smoke bootstrap-test e2e persistence backup restore backup-restore-test rotate-secrets

up:
	python3 tools/start_stack.py start --project "$(PROJECT_NAME)" --env-file "$(ENV_FILE)" --wait-timeout "$(WAIT_TIMEOUT)"

start-database:
	python3 tools/start_stack.py database --project "$(PROJECT_NAME)" --env-file "$(ENV_FILE)" --wait-timeout "$(WAIT_TIMEOUT)"

start-application:
	python3 tools/start_stack.py application --project "$(PROJECT_NAME)" --env-file "$(ENV_FILE)" --wait-timeout "$(WAIT_TIMEOUT)"

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

smoke:
	tools/smoke_https.sh

bootstrap-test:
	python3 tests/runtime_stack.py bootstrap

e2e:
	python3 tests/runtime_stack.py e2e

persistence:
	python3 tests/runtime_stack.py persistence

backup:
	@test -n "$(BACKUP_DIR)" || { echo "BACKUP_DIR is required" >&2; exit 2; }
	python3 tools/stack_backup.py backup --project "$(PROJECT_NAME)" --env-file "$(ENV_FILE)" --output "$(BACKUP_DIR)"

restore:
	@test -n "$(BACKUP_DIR)" || { echo "BACKUP_DIR is required" >&2; exit 2; }
	python3 tools/stack_backup.py restore --project "$(PROJECT_NAME)" --env-file "$(ENV_FILE)" --input "$(BACKUP_DIR)"

backup-restore-test:
	python3 tests/runtime_stack.py backup-restore

rotate-secrets:
	@test -n "$(NEW_SECRETS_DIR)" || { echo "NEW_SECRETS_DIR is required" >&2; exit 2; }
	python3 tools/rotate_secrets.py --project "$(PROJECT_NAME)" --env-file "$(ENV_FILE)" --new-secrets-dir "$(NEW_SECRETS_DIR)"
