# 읽는 순서 가이드 (inception / container-stack)

> 이 문서는 코드 주석이 아니라, 이미 곳곳에 박아둔 `[INTV:ARCH]` 등 주석들을 "어디서부터 읽어야
> 전체 그림이 잡히는가" 관점에서 엮은 내비게이션 문서다. 실제 설계 이유는 항상 코드 옆 `[INTV]`
> 주석에 있고, 여기는 그 주석들을 찾아가는 지도 역할만 한다.

## 30초 피치

nginx(TLS 종단) + WordPress/php-fpm + MariaDB를 각각 직접 만든 Dockerfile로 격리해 띄우는
Compose 스택. "그냥 docker-compose up이 되게 만드는" 수준을 넘어, 네트워크 세그멘테이션으로
DB를 외부와 완전히 차단하고, 컨테이너를 부트스트랩 도중 강제 종료해도 재기동 시 올바른
상태로 수렴하며, 비밀값 회전·백업/복원·리소스 제한까지 갖춘 "운영 가능한" 스택을 목표로 한다.
그 운영 시나리오들이 얼마나 진지하게 검증되는지는 CI(`.github/workflows/container-stack.yml`)
의 210분짜리 job 하나가 그대로 보여준다.

## 요청/데이터 흐름

```
[클라이언트] --HTTPS(443)--> [nginx] --FastCGI(9000)--> [wordpress(php-fpm)] --TCP(3306)--> [mariadb]
                                  ^                              |
                            frontend 네트워크                backend 네트워크
                          (nginx, wordpress만)          (wordpress, mariadb만 — internal: true)
```

- nginx는 `frontend` 네트워크에만 속해 mariadb에 직접 닿을 방법이 없다.
- wordpress(php-fpm)는 `frontend`+`backend` 양쪽에 걸치는 **유일한** 서비스 — DB로 가는 유일한
  경로가 이 컨테이너를 반드시 거치게 강제된다.
- `backend`는 `internal: true`라 호스트/인터넷으로 나가는 길 자체가 없다 — "DB는 애초에 외부와
  통신할 방법이 없다"는 걸 애플리케이션 코드가 아니라 네트워크 레벨에서 강제.

## 읽는 순서

### 1. 전체 그림 먼저 — `srcs/docker-compose.yml`
서비스 3개, 네트워크 2개, 볼륨 3개가 어떻게 연결되는지 한눈에 보이는 파일. `[INTV:ARCH]` 주석이
붙은 지점 위주로: `x-` 확장 필드 규약(상단), `depends_on`의 `service_healthy` 조건(단순 기동이
아니라 헬스체크 통과까지 기다림), wp-config.php를 웹 루트와 분리된 별도 볼륨에 두는 이유,
`backend: internal: true`.

### 2. 각 서비스의 이미지가 어떻게 만들어지는가 — Dockerfile 3개
`srcs/requirements/{mariadb,wordpress,nginx}/Dockerfile`을 훑어보면 세 파일 모두 같은 원칙을
반복한다(재현 가능한 빌드): 베이스 이미지를 태그가 아니라 `@sha256` 다이제스트로 고정, apt
저장소를 `snapshot.debian.org`의 특정 시각으로 고정, ENTRYPOINT/CMD 역할 분리. 이 반복
패턴을 한 번 이해하면 세 Dockerfile을 거의 비슷한 속도로 읽을 수 있다.
- nginx만 있는 특이점: `[INTV:ARCH] [INTV:EDGE]` 443만 노출하고 80(평문 HTTP)은 아예 안 연다.

### 3. 실제 동작의 핵심 — entrypoint 스크립트 2개
- `srcs/requirements/mariadb/tools/docker-entrypoint.sh` — `set -e`/`set -u` 방어, `${VAR:-기본값}`
  셸 관용구, `case` 글롭 매칭으로 입력 검증. 그리고 **이 프로젝트에서 가장 중요한 설계**: 테스트
  하네스(`tests/runtime_stack.py`)가 부트스트랩의 특정 단계 직후 컨테이너를 멈춰 세우고 재기동
  시키기 위한 `pause_after` 단계 구조가 여기 정의된다 — bootstrap 시나리오를 읽기 전에 반드시
  먼저 이 파일의 단계 이름들을 봐야 그 테스트가 뭘 하는지 이해된다.
- `srcs/requirements/wordpress/tools/docker-entrypoint.sh` — mariadb와 같은 "bootstrap 인자로
  한 번 / 평시 실행 시 다시" 이중 역할 구조. `converge_wordpress_config()`의 3가지 상태 코드
  반환값, `WORDPRESS_URL`이 바뀌었을 때 이미 부트스트랩된 사이트의 설정을 갱신하는 로직이 이
  스크립트의 핵심.

### 4. 그 위에서 실제로 검증하는 코드 — `tests/`, `tools/`
CI가 정확히 무엇을 호출하는지, 그리고 각 시나리오가 무엇을 검증하는지는
`tests/runtime_stack.py`의 6개 진입점(`verify_e2e`, `verify_bootstrap_recovery`,
`verify_persistence`, `verify_backup_restore`, `verify_secret_rotation`, `verify_operations`)
바로 위 `[INTV:ARCH]` 주석에 각각 요약돼 있다. `tests/validate_stack.py`는 컨테이너를 띄우지 않는
더 저렴한 정적 검증(같은 파일 상단 `[INTV:ARCH]`/`[INTV:TRAP]` 참고 — `--functional` 플래그가
실은 아무 효과가 없다는 함정도 여기 적혀 있다).

### 5. 오케스트레이션 진입점 — `Makefile`, `.github/workflows/container-stack.yml`
로컬에서 `make <target>`으로 무엇을 실행할 수 있는지는 `make help`가 그대로 보여주고, CI가
같은 명령들을 어떤 순서·조건으로 자동 실행하는지는 워크플로 파일 자체에 GitHub Actions 문법
설명 없이(문법 자체는 `../miniRT/.github/workflows/ci.yml`에 모아둠) why/how 위주로 달려있다.

## 재구현 시 가장 먼저 마주칠 함정

- `srcs/requirements/*/Dockerfile`의 다이제스트 고정을 빼먹으면 "로컬에서는 되는데 CI에서는
  안 되는" 재현성 문제가 생긴다.
- entrypoint 스크립트의 `pause_after` 단계 개념 없이 부트스트랩을 구현하면, bootstrap 시나리오
  테스트(`verify_bootstrap_recovery`)를 통과시킬 방법이 없다 — 이 단계 구조는 테스트 하네스와
  1:1로 맞물려 설계된 것이라, entrypoint와 테스트 코드를 항상 같이 봐야 한다.
- `backend` 네트워크에 `internal: true`를 빼먹으면(과거 이 저장소에서 실제로 검증이 깨졌던
  사례가 `bug-fix-report.md`에 있다), DB 격리라는 이 프로젝트의 핵심 보안 속성이 조용히 사라진다.
