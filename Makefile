COMPOSE 		:= docker compose
COMPOSE_FILE 	:= srcs/docker-compose.yml
ENV_FILE 		?= .env
PROJECT_NAME 	?= container-stack
WAIT_TIMEOUT 	?= 300
CHECK_ENV_FILE 	?= .env.example
BACKUP_DIR 		?=
NEW_SECRETS_DIR	?=
DIAGNOSTICS_DIR	?= diagnostics/$(PROJECT_NAME)
DESTROY_CONFIRM	?=

COMPOSE_RUN 	:= $(COMPOSE) --project-name "$(PROJECT_NAME)" --env-file "$(ENV_FILE)" -f "$(COMPOSE_FILE)"

.DEFAULT_GOAL 	:= help

.PHONY: help check-functional up up-build start-database start-application
.PHONY: down build logs ps fclean re test config config-strict smoke
.PHONY: bootstrap-test e2e persistence backup restore backup-restore-test
.PHONY: rotate-secrets rotation-test diagnostics operations-test verify

help:
	@printf '%s\n' \
		'Usage: make <target> [VARIABLE=value]' \
		'' \
		'Stack:' \
		'  up                 Reconcile and start the existing images' \
		'  up-build           Build, reconcile, and start under one operation lock' \
		'  start-database     Reconcile and start only MariaDB' \
		'  start-application  Reconcile and start WordPress and nginx' \
		'  build              Build all local images' \
		'  down               Stop containers; preserve images and volumes' \
		'  ps / logs          Show container state / follow logs' \
		'  fclean             Remove volumes and local images (confirmation required)' \
		'  re                 fclean, then rebuild and start (confirmation required)' \
		'' \
		'Validation:' \
		'  check-functional   Run static checks and strict Compose parsing' \
		'  test               Run source-level validation' \
		'  config             Print the resolved Compose model' \
		'  config-strict      Validate a Compose model without printing it' \
		'  smoke              Probe the running HTTPS endpoint' \
		'  verify             Run every static and runtime scenario serially' \
		'' \
		'Operations: backup, restore, rotate-secrets, diagnostics' \
		'Test scenarios: bootstrap-test, e2e, persistence, backup-restore-test,' \
		'                rotation-test, operations-test' \
		'' \
		'Common variables: PROJECT_NAME, ENV_FILE, WAIT_TIMEOUT, CHECK_ENV_FILE'

check-functional:
	python3 tests/validate_stack.py --functional
	$(MAKE) config-strict ENV_FILE="$(CHECK_ENV_FILE)"

up:
	python3 tools/start_stack.py start --project "$(PROJECT_NAME)" --env-file "$(ENV_FILE)" --wait-timeout "$(WAIT_TIMEOUT)"

up-build:
	python3 tools/start_stack.py start --project "$(PROJECT_NAME)" --env-file "$(ENV_FILE)" --wait-timeout "$(WAIT_TIMEOUT)" --build

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

fclean:
	@test -n "$(PROJECT_NAME)" && test "$(DESTROY_CONFIRM)" = "$(PROJECT_NAME)" || { \
		echo "볼륨과 로컬 이미지를 삭제하려면 DESTROY_CONFIRM=$(PROJECT_NAME)을 지정하십시오." >&2; \
		exit 2; \
	}
	$(COMPOSE_RUN) down -v --rmi local --remove-orphans

re: fclean
	$(MAKE) up-build

config:
	$(COMPOSE_RUN) config

config-strict:
	@command -v docker >/dev/null 2>&1 || { echo "docker 명령을 찾을 수 없습니다." >&2; exit 2; }
	@docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2를 사용할 수 없습니다." >&2; exit 2; }
	$(COMPOSE_RUN) config --quiet

test:
	python3 tests/validate_stack.py
	@if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then \
		$(COMPOSE) --env-file .env.example -f "$(COMPOSE_FILE)" config >/dev/null; \
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

rotation-test:
	python3 tests/runtime_stack.py rotation

diagnostics:
	python3 tools/diagnose_stack.py --project "$(PROJECT_NAME)" --env-file "$(ENV_FILE)" --output "$(DIAGNOSTICS_DIR)"

operations-test:
	python3 tests/runtime_stack.py operations

verify:
	python3 tools/verify_stack.py
