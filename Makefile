# 변수 대입 연산자: `:=`는 즉시 값을 고정하는 대입, `?=`는 이미 값이 있으면(환경변수나 `make VAR=값`으로 넘긴 경우) 덮어쓰지 않는 조건부 대입 — Make 고유 문법으로, 아래 값들을 커맨드라인/환경변수로 오버라이드 가능하게 하기 위한 관용구
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

# 모든 타겟이 반복해서 쓸 compose 호출부를 변수 하나로 묶어둔 것 — 프로젝트명/env 파일/compose 파일 경로를 한 곳에서만 관리해 타겟마다 따로 지정하지 않게 하려는 설계
COMPOSE_RUN 	:= $(COMPOSE) --project-name "$(PROJECT_NAME)" --env-file "$(ENV_FILE)" -f "$(COMPOSE_FILE)"

# .DEFAULT_GOAL: 인자 없이 `make`만 입력했을 때 실행할 타겟을 지정하는 Make 전용 특수 변수 (여기서는 사용법을 출력하는 help)
.DEFAULT_GOAL 	:= help

# .PHONY: 아래 이름들이 실제 파일이 아니라 "명령"임을 선언하는 Make 전용 지시자 — 우연히 같은 이름의 파일이 생겨도 Make가 "이미 최신"이라 판단해 실행을 건너뛰는 걸 막는다
.PHONY: help check-functional up up-build start-database start-application
.PHONY: down build logs ps fclean re test config config-strict smoke
.PHONY: bootstrap-test e2e persistence backup restore backup-restore-test
.PHONY: rotate-secrets rotation-test diagnostics operations-test verify

help:
	# `@`는 이 명령 자체가 콘솔에 에코되지 않게 숨기는 Make 문법. 뒤의 백슬래시(`\`)는 셸 라인 연속으로, 여러 문자열 인자를 printf 한 번에 넘기기 위한 것
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

# [INTV:ARCH] CI(container-stack.yml)의 "정적 구성 검사" 스텝이 바로 이 타겟을 호출한다 — 실제로
# 컨테이너를 하나도 띄우지 않고 끝나는, 이 프로젝트에서 가장 저렴한 검증 단계. 두 스텝으로 구성:
# (1) validate_stack.py --functional로 소스/설정 파일 자체의 정적 규칙(아래 tests/validate_stack.py
# 참고)을, (2) config-strict로 실제 Compose 스키마 파싱을 검증한다 — 둘 다 컨테이너를 안 띄우므로
# CI의 6개 런타임 시나리오보다 훨씬 빠르게 "가장 흔한 실수"부터 걸러낸다.
check-functional:
	python3 tests/validate_stack.py --functional
	# [INTV:TRAP] $(MAKE)로 자기 자신을 재귀 호출하면서 ENV_FILE만 예제 파일로 바꿔치기 — 실제
	# 비밀값 없이도(.env.example로) compose 설정 자체의 구문/스키마 오류를 잡기 위한 설계. 이
	# 타겟을 실제 .env로만 테스트하면, 비밀값을 가진 사람의 로컬 환경에서는 통과하지만 그 비밀값이
	# 없는 CI/신규 기여자 환경에서는 애초에 config 파싱조차 안 되는 문제를 놓친다.
	$(MAKE) config-strict ENV_FILE="$(CHECK_ENV_FILE)"

# [INTV:ARCH] 이 Makefile은 오케스트레이션 로직을 직접 담지 않고 python 스크립트에 위임하는 얇은
# CLI 래퍼 — 실제 대기/헬스체크/롤백 판단은 tools/start_stack.py 쪽 책임.
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
	# 볼륨과 이미지를 지우는 파괴적 명령이라, PROJECT_NAME과 정확히 일치하는 DESTROY_CONFIRM을 요구해 오조작으로 인한 데이터 삭제를 막는 안전장치
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
	# docker/compose 자체가 없는 환경에서 뒤 명령이 알 수 없는 에러로 실패하는 대신, 원인을 명확히 알려주고 조기 종료하는 사전 점검(preflight check) 패턴
	@command -v docker >/dev/null 2>&1 || { echo "docker 명령을 찾을 수 없습니다." >&2; exit 2; }
	@docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2를 사용할 수 없습니다." >&2; exit 2; }
	$(COMPOSE_RUN) config --quiet

test:
	python3 tests/validate_stack.py
	# docker가 없는 환경(예: 최소 CI 러너)에서도 나머지 테스트는 통과시키되, compose 파일 자체의 문법 검증만 가능하면 추가로 수행 — 하드 의존성 없이 점진적으로 검증 범위를 넓히는 설계
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
	# 출력 경로를 항상 명시적으로 요구 — 기본 경로에 조용히 저장/덮어쓰기 되는 것을 막기 위함 (아래 restore의 --input도 동일한 이유)
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
