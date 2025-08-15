# Container Stack

![Language](https://img.shields.io/badge/language-Python-blue?logo=python&logoColor=white)
![Build](https://img.shields.io/badge/build-Docker%20Compose-lightgrey)

`container-stack`는 42 `inception` 과제를 변형한 Docker Compose 운영 실습 프로젝트입니다. nginx, WordPress PHP-FPM과 MariaDB를 한 Docker 호스트에서 실행하며, 세 이미지를 직접 빌드하고 최초 상태 생성, 재시작, 영속성 확인, 백업·복원, 자격증명 교체와 장애 자료 수집을 같은 저장소의 명령으로 관리합니다.

단순히 컨테이너 세 개를 실행하는 예제가 아닙니다. 호스트 secret 파일을 검증하고, 초기화 작업과 장기 실행 컨테이너를 분리하며, 실패 뒤 남은 상태를 다시 판별해 목표 상태로 수렴시키는 운영 절차를 구현합니다.

## 한눈에 보기

| 항목 | 내용 |
| --- | --- |
| 구성 요소 | nginx, WordPress PHP-FPM, MariaDB |
| 실행 관리 | Docker Compose v2, Makefile, Python 운영 도구 |
| 공개 진입점 | nginx의 HTTPS port |
| 내부 통신 | nginx → FastCGI → WordPress, WordPress → MariaDB |
| 영속 상태 | MariaDB data, WordPress data, `wp-config.php`를 named volume에 저장 |
| 운영 기능 | bootstrap, 상태 확인, 백업·복원, 자격증명 교체, 진단 수집 |
| 주요 검증 | 정적 계약, e2e, persistence, backup/restore, rotation, operations |

## 실행 구조

```text
브라우저 또는 HTTPS client
        │  host 127.0.0.1:${HTTPS_PORT}
        ▼
Docker daemon의 published port
        │  container 443 / TLS
        ▼
      nginx
        │  FastCGI / frontend network
        ▼
 WordPress PHP-FPM
        │  MariaDB protocol / backend network
        ▼
      MariaDB
```

- nginx만 호스트 port를 게시합니다.
- 기본 bind는 `127.0.0.1:443`이므로 같은 호스트에서만 직접 접근할 수 있습니다.
- nginx가 TLS를 종료하고 정적 파일을 제공하거나 WordPress에 FastCGI record를 보냅니다.
- WordPress는 backend network의 MariaDB에 연결합니다.
- backend internal network는 외부 노출을 줄이지만 애플리케이션 인증과 Docker daemon 접근 통제를 대신하지 않습니다.

## 요구 환경

- Docker Engine
- Docker Compose v2
- Python 3.10 이상
- GNU Make 또는 호환 `make`
- `curl`

관리 도구는 Compose의 `up --wait`, `--wait-timeout`, `config --format json`, `config --no-interpolate`를 사용합니다. 첫 image build에는 Debian snapshot과 WordPress·WP-CLI 배포 서버에 접근할 수 있어야 합니다.

## 최초 준비

환경 파일과 secret은 Git에 넣지 않습니다.

```sh
cp .env.example .env
umask 077
install -d -m 0700 secrets
printf 'replace-root-password-01\n' > secrets/db_root_password.txt
printf 'replace-database-password-01\n' > secrets/db_password.txt
printf 'replace-admin-password-01\n' > secrets/wp_admin_password.txt
printf 'replace-author-password-01\n' > secrets/wp_user_password.txt
chmod 600 secrets/*.txt
```

네 값은 서로 다르게 준비해야 합니다. 운영 도구가 받는 secret은 허용 문자로 된 24~128자의 한 줄입니다.

시작 과정은 다음 조건을 검사합니다.

- secret 상위 디렉터리가 현재 사용자 소유인지
- group과 other 접근 권한이 없는지
- secret 파일 자체가 symlink가 아닌지
- 같은 canonical path를 여러 secret 항목이 공유하지 않는지

`DOMAIN_NAME`, `WORDPRESS_URL`, `HTTPS_PORT`는 같은 공개 주소를 가리켜야 하지만, 이 값들이 서로 일치하는지는 자동으로 검사하지 않으므로 `.env`를 채울 때 직접 확인해야 합니다.

## 빌드와 시작

`make up`은 image를 자동으로 빌드하지 않습니다. 처음에는 다음 순서로 실행합니다.

```sh
make build
make up
make ps
```

`make up`은 같은 project의 operation lock을 잡고 다음 순서로 진행합니다.

```text
Compose model에서 secret 경로 확인
  -> 호스트 secret 검증
  -> one-off MariaDB bootstrap
  -> MariaDB runtime 시작과 health 대기
  -> 기존 nginx·WordPress 중지
  -> one-off WordPress bootstrap/reconciliation
  -> WordPress runtime 시작과 health 대기
  -> nginx runtime 시작과 health 대기
```

두 단계를 별도로 실행할 수도 있습니다.

```sh
make start-database
make start-application
```

중간 실패에 대한 전역 롤백은 없습니다. MariaDB만 실행되거나 일부 컨테이너가 멈추거나 unhealthy 상태가 남을 수 있습니다. 현재 상태를 확인한 뒤 같은 명령을 다시 실행합니다.

```sh
make ps
make logs
```

재실행은 이전 작업을 역순으로 되돌리는 절차가 아니라 현재 volume과 컨테이너 상태를 다시 판별해 목표 상태로 수렴시키는 새 시도입니다.

직접 `docker compose up`을 실행하면 operation lock, secret 검증과 초기 상태 조정을 우회하므로 지원되는 관리 경로로 보지 않습니다.

## 상태 확인과 종료

기본 상태 URL은 `https://localhost/healthz`입니다. 개발용 인증서는 자체 서명이므로 smoke 검사는 인증서 신뢰 확인을 생략합니다.

```sh
make smoke
SMOKE_URL=https://example.test/healthz make smoke
```

컨테이너와 network만 내리고 named volume과 image를 보존합니다.

```sh
make down
```

volume까지 삭제하려면 project 이름을 다시 전달해야 합니다.

```sh
make fclean DESTROY_CONFIRM=container-stack
```

이 명령은 WordPress와 MariaDB의 영속 데이터를 삭제합니다. `PROJECT_NAME`을 바꿨다면 `DESTROY_CONFIRM`에도 같은 값을 사용해야 합니다.

volume과 image를 지운 뒤 처음부터 다시 빌드하고 기동하려면 같은 확인 값과 함께 `re`를 사용합니다.

```sh
make re DESTROY_CONFIRM=container-stack
```

`re`는 `fclean` 후 `build`와 `up`을 순서대로 실행합니다.

## 영속 상태

| 상태 | 저장 위치 | 컨테이너 재생성 뒤 |
| --- | --- | --- |
| MariaDB data와 완료 marker | `mariadb_data` named volume | 유지 |
| WordPress core, content와 upload | `wordpress_data` named volume | 유지 |
| `wp-config.php` | `wordpress_config` named volume | 유지 |
| nginx 자체 서명 인증서 | nginx writable layer | 새 컨테이너에서 재생성 |
| 호스트 secret 원본 | `.env`가 가리키는 host file | Compose가 관리하지 않음 |
| bootstrap 임시 파일 | one-off 컨테이너의 `/run` | 컨테이너 종료와 함께 제거 |

장기 실행 컨테이너에는 호스트 secret 파일 mount, 비밀번호 환경 변수와 비밀번호 명령행 인자를 두지 않습니다. 다만 WordPress DB 비밀번호는 `wp-config.php`에 평문으로 남고 MariaDB·WordPress database에도 인증 상태가 저장됩니다. 백업에도 이 상태가 포함됩니다.

## 백업

세 runtime service가 모두 실행 중일 때만 백업을 시작합니다.

```sh
make backup BACKUP_DIR=/secure/backups/container-stack-20250104
```

백업 과정은 다음과 같습니다.

```text
operation lock 획득
  -> nginx·WordPress 중지
  -> MariaDB single-transaction dump
  -> WordPress data와 config archive 생성
  -> 0600 임시 산출물 완성
  -> SHA-256 manifest 생성
  -> 예약한 최종 디렉터리로 게시
  -> runtime service 재시작
```

외부 database writer가 없다는 조건에서 database와 filesystem의 애플리케이션 일관성을 맞춥니다. 백업은 암호화, 예약 실행, 보존 기간, 원격 복제, nginx 인증서, image, 호스트 `.env`와 secret 원본을 포함하지 않습니다.

## 복원

복원 대상은 컨테이너, volume과 network가 없는 새 project여야 합니다.

```sh
make down PROJECT_NAME=container-stack ENV_FILE=.env
make restore \
  PROJECT_NAME=container-stack-restore \
  ENV_FILE=.env \
  BACKUP_DIR=/secure/backups/container-stack-20250104
```

복원 도구는 manifest와 checksum을 확인하고 archive의 절대 경로, `..`, 중복 항목과 특수 파일을 거부합니다.

checksum은 파일이 manifest와 같다는 사실만 확인하며 백업 출처의 신뢰성을 증명하지 않습니다. `SIGKILL`, 호스트 종료와 Docker daemon 손실 뒤 자동 정리도 보장하지 않습니다.

## 자격증명 교체

새 secret 네 개를 현재 파일과 다른 private 디렉터리에 준비합니다.

```sh
make rotate-secrets NEW_SECRETS_DIR=/secure/container-stack-next-secrets
```

도구는 현재 값이 성공하고 새 값이 아직 거부되는지 먼저 확인한 뒤 다음 순서로 교체합니다.

```text
nginx 중지
  -> WordPress admin·author
  -> wp-config.php
  -> MariaDB application·root
  -> host secret file
  -> runtime 컨테이너 강제 재생성
  -> 이전 값 거부·새 값 성공 확인
```

전체가 하나의 원자적 transaction은 아닙니다. 처리 가능한 실패와 SIGINT·SIGTERM에서는 실제로 동작하는 root credential을 찾아 이전 상태로 보상하려고 시도합니다. `SIGKILL`, 호스트·daemon 손실에 대비한 durable journal은 없습니다.

## 장애 자료 수집

```sh
make diagnostics DIAGNOSTICS_DIR=diagnostics/incident-001
```

새 `0700` 디렉터리와 `0600` 파일에 Compose 상태, 최근 log와 resource 설정을 수집합니다. 가려야 할 secret을 하나라도 읽지 못하면 일부 자료를 남기지 않고 수집 전체를 중단합니다.

redaction은 project 밖의 코드가 log에 기록한 모든 민감 정보를 탐지한다고 보장하지 않습니다. 외부 공유 전 사람이 다시 확인해야 합니다.

## 검증

| 명령 | 확인하는 내용 |
| --- | --- |
| `make test` | source 정적 조건과 가능한 경우 `.env.example` Compose model parse |
| `make config-strict ENV_FILE=.env.example` | 설치된 Compose가 model을 해석하는지 |
| `make smoke` | 실행 중인 nginx의 HTTPS 상태 |
| `make bootstrap-test` | bootstrap 중 `SIGKILL` 뒤 재시도 수렴과 runtime secret source 부재 |
| `make e2e` | HTTPS WordPress 쓰기·읽기와 MariaDB 저장값 |
| `make persistence` | restart와 `down/up` 뒤 database·upload·volume identity |
| `make backup-restore-test` | 백업·복원 정상·실패·signal·경로 방어와 새 project 복원 |
| `make rotation-test` | 이전·새 자격증명 검증, 실패 보상과 재시도 |
| `make operations-test` | network, resource, log, stop, 파괴 명령과 diagnostics |
| `make verify` | 정적·runtime 시나리오 전체 실행과 잔여 자원 회수 |

## 문서

1. [첫 Production 배포 가이드](PRODUCTION-DEPLOYMENT-GUIDE.md)

## 제한 사항

지원 범위는 한 사용자와 한 Docker daemon이 관리하는 단일 호스트 project입니다.

다음 기능은 제공하지 않습니다.

- public CA 인증서 발급·갱신·영속 보관
- 외부 secret manager와 무중단 이중 자격증명 교체
- 암호화·예약·보존·원격 복제를 갖춘 백업 체계
- MariaDB replication, point-in-time recovery와 고가용성
- 여러 호스트의 분산 lock과 scheduler
- Kubernetes와 production ingress
- 임의의 WordPress schema·data migration framework

## 프로젝트 배경

이 저장소는 42 `inception` 과제에서 출발했습니다. 기본 nginx·WordPress·MariaDB 구성을 넘어 호스트 secret 검증, 단계별 bootstrap, 상태 수렴, 영속성 회귀, 백업·복원, 자격증명 교체, 진단 자료와 강제 중단 시나리오를 추가했습니다.
