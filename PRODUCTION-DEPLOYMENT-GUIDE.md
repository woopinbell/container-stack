# Container Stack 첫 Production 배포 가이드

기준일: 2026-09-01(KST)

이 문서는 배포를 처음 해보는 소유자가 Container Stack을 실제 인터넷에 공개하기 전에 무엇을
준비하고, 어떤 화면에서 무엇을 확인하며, 언제 멈춰야 하는지 안내한다. 로컬 실행 계약은
[README](README.md), 지원 명령은 [Makefile](Makefile), 실제 서비스 구성은
[Compose 모델](srcs/docker-compose.yml)을 기준으로 한다.

이 문서를 위에서 아래로 읽는 것만으로 배포가 실행되지는 않는다. `STOP` 항목을 코드와 별도
환경에서 검증한 뒤, 각 단계의 체크박스를 실제 작업 기록으로 복사해 사용한다.

## 1. 먼저 알아둘 결론

### 권장 기본 구조

| 역할 | 선택 | 하는 일 |
|---|---|---|
| source repository | 비공개 GitHub repository | 승인된 `main`과 exact release SHA 보관 |
| image release | GitHub Actions와 GHCR | 검증한 세 이미지를 immutable digest로 보관 |
| application host | AWS Lightsail 서울 리전 | Docker Engine과 운영 도구를 실행하는 단일 VPS |
| host image | Ubuntu 24.04 LTS OS-only | 사전 설치 WordPress가 아닌 깨끗한 운영체제 |
| host size | Linux 4GB, 2 vCPU, 80GB부터 시작 | image build·세 runtime·백업 staging 여유 확보 |
| stable address | Lightsail Static IPv4 | 인스턴스 재시작 뒤에도 origin 주소 유지 |
| origin firewall | Lightsail IPv4·IPv6 firewall | SSH와 Cloudflare발 HTTPS만 허용 |
| domain·edge | Cloudflare authoritative DNS와 proxy | 공개 DNS, Universal SSL, DDoS 방어 |
| origin TLS | Let’s Encrypt + Certbot DNS-01 | Cloudflare와 nginx 사이 인증서 검증 |
| host recovery | Lightsail automatic·manual snapshot | 운영체제와 Docker host 전체 복구 보조 |
| application recovery | 기존 `make backup` + 검토된 암호화 원격 복제 | MariaDB·WordPress data/config 일관성 백업 |
| monitoring | Lightsail metric alarm + 외부 HTTPS monitor | CPU·메모리·disk·인증서·실제 페이지 장애 통지 |

```text
사용자
  └─ HTTPS
      Cloudflare
        ├─ authoritative DNS·proxied A record
        ├─ Universal SSL·Always Use HTTPS
        └─ Full (strict)
             └─ HTTPS
                 Lightsail 서울 / Static IPv4 / upstream firewall
                   └─ Docker published 443
                       nginx
                         └─ FastCGI / frontend network
                             WordPress PHP-FPM
                               └─ backend internal network
                                   MariaDB
```

Lightsail의 WordPress 또는 nginx 사전 구성 이미지는 사용하지 않는다. 이 repository가 직접 만드는
세 image, Compose network·volume, secret bootstrap과 백업·복구 도구가 제품의 일부이기 때문이다.

현재 공식 사양에서 4GB Linux bundle은 월 USD 24이지만 가격·세금·전송량·snapshot 비용은 바뀔 수
있다. 결제 직전 Lightsail 생성 화면에서 다시 확인한다. 이 구조는 한 대의 서버가 멈추면 사이트도
멈추는 단일 호스트 구조다. 유료 거래·회원 핵심 데이터·무중단 서비스가 목적이면 이 가이드로
진행하지 말고 managed database와 다중 host 구조를 먼저 설계한다.

## 2. 이 가이드의 표시

- **직접 수행**: 계정 소유자가 dashboard나 terminal에서 하는 단계
- **Codex와 수행**: repository 구현·검증이 필요한 단계
- **STOP**: 통과 전에는 production resource 생성·자료 이전·DNS 전환을 하지 않는 관문
- **기록**: secret 값이 아니라 resource ID·release SHA·image digest·receipt 위치만 남기는 단계

AWS key, GitHub token, Cloudflare token, WordPress·database password, backup encryption key,
private TLS key를 Git·대화·screenshot·ticket·shell history에 붙여 넣지 않는다. 승인된 password
manager와 해당 서비스의 secret 입력 경계에서만 다룬다.

## 3. 현재 구현이 이미 제공하는 것

- nginx·WordPress PHP-FPM·MariaDB를 직접 만드는 세 개의 pinned image
- nginx만 host port를 공개하고 MariaDB는 internal backend network에만 두는 구조
- `mariadb_data`, `wordpress_data`, `wordpress_config` named volume
- runtime container에 password environment·argument·secret mount를 남기지 않는 bootstrap
- 현재 사용자 소유 `0700` directory와 `0600` 단일-link regular file을 요구하는 secret 검사
- 한 project·한 host에서 동시에 하나의 운영 명령만 허용하는 operation lock
- 재실행으로 목표 상태에 수렴하는 database·WordPress 초기화
- container별 healthcheck, resource limit, log rotation, graceful stop과 `unless-stopped`
- MariaDB dump와 WordPress data/config를 함께 만드는 application-consistent backup
- 빈 Compose project에만 복원하는 fail-closed restore
- database root·application·WordPress admin·author password rotation과 best-effort rollback
- secret redaction에 실패하면 전체를 폐기하는 제한된 diagnostics
- bootstrap·e2e·persistence·backup/restore·rotation·operations 회귀 시나리오

현재 기본 resource 상한 합계는 nginx 0.5 CPU/128MiB, WordPress 1 CPU/512MiB, MariaDB
1 CPU/512MiB다. 운영체제·Docker daemon·page cache·image build·backup staging 여유까지 고려해
4GB host부터 시작하고 metric을 본 뒤 조정한다.

## 4. Production 전에 끝내야 하는 것 — STOP

현재 repository는 단일 host 운영 실습의 완성도는 높지만 그대로 공개하는 배포 후보는 아니다.
다음 항목이 하나라도 남으면 Lightsail을 만들 수는 있어도 사용자 traffic을 열지 않는다.

### Source·CI·release

- [ ] 이 repository의 비공개 GitHub origin을 만들고 `main` exact SHA를 보존
- [ ] 현재 legacy branch만 감시하는 workflow를 `main` pull request·push 기준으로 변경
- [ ] repository 문구 정책과 CI 검사가 서로 요구하는 값이 충돌하는 기존 모순 해소
- [x] README가 가리키지만 현재 없는 세 `architecture/` 문서를 복구하거나 잘못된 link 제거 — link 제거로 해소
- [ ] required check와 branch protection 구성, force-push·branch 삭제 차단
- [ ] 세 image를 exact source SHA로 빌드하고 GHCR digest로 고정하는 release workflow 추가
- [ ] production host가 임의 build나 `latest`가 아니라 승인 digest만 실행하도록 고정
- [ ] image SBOM·취약점 결과와 base image 갱신 기준 추가
- [ ] 이전 release digest와 현재 volume schema의 rollback 호환성 검증

### Dependency·WordPress

- [ ] pinned WordPress `6.7.7` 유지 또는 현재 지원 버전으로 갱신할지 사람 승인
- [ ] 2026-09-01 현재 최신 major `7.1`과의 차이를 staging restore에서 검토
- [ ] 선택한 WordPress tarball·WP-CLI·Debian base·package snapshot checksum 갱신
- [ ] 갱신 뒤 전체 runtime 회귀와 실제 admin·upload·database upgrade 확인
- [ ] core·theme·plugin 보안 update를 image pin으로 반영하는 정기 절차 추가
- [ ] plugin·theme 허용 목록, 출처, update·rollback 담당자 고정

현재 `wp-config.php`는 core automatic update를 명시적으로 끈다. Ubuntu의 unattended upgrade는
host package만 갱신하며 immutable container 안의 WordPress·PHP·nginx·MariaDB를 갱신하지 않는다.

### Public ingress·TLS

- [ ] production nginx가 trusted certificate와 private key를 read-only로 mount하도록 구현
- [ ] certificate가 없거나 hostname이 맞지 않으면 self-signed fallback 없이 fail closed
- [ ] nginx `server_name`을 승인 domain으로 제한하고 예상하지 않은 Host 거부
- [ ] Certbot renewal symlink를 안전하게 따라가는 certificate path 고정
- [ ] 인증서 갱신 성공 후에만 nginx config 검증·reload하는 지원 명령 추가
- [ ] `certbot renew --dry-run`과 certificate rollback rehearsal 통과
- [ ] production에서 `curl -k` 없이 certificate chain·hostname·expiry 검증
- [ ] `HTTPS_BIND_ADDRESS=0.0.0.0` 공개가 upstream firewall 뒤에서만 가능함을 검사
- [ ] DOMAIN·WordPress URL·공개 port의 실제 교차 검증을 startup preflight에 추가

현재 nginx는 365일 self-signed certificate를 writable container layer에 만든다. container가
재생성되면 certificate도 바뀌고, `make smoke`는 `curl -k`라 browser trust를 증명하지 않는다.
host reverse proxy를 즉흥적으로 앞에 붙이면 현재 port·URL 일치 계약과 충돌할 수 있다. 검토된
certificate mount 구현이 들어오기 전에는 아래 TLS 명령을 실행하지 않는다.

### Backup·recovery

- [ ] `make backup`을 저트래픽 시간에 예약하는 single-host scheduler 추가
- [ ] output을 검토된 도구로 client-side encryption한 뒤 off-host storage에 복제
- [ ] 최소 daily 7개·weekly 4개·monthly 6개 retention 승인
- [ ] 원격 checksum·object 존재를 확인한 뒤 local staging을 제거하는 receipt 추가
- [ ] `.env`, host secret, TLS 복구 자료, exact SHA와 backup key의 별도 recovery kit 구성
- [ ] production과 분리된 새 instance·새 project에서 정기 restore rehearsal
- [ ] 목표 RPO·RTO와 허용 backup downtime 승인
- [ ] backup·upload·retention·restore 실패 alert 구성

기존 backup은 nginx와 WordPress를 잠시 멈추고 일관된 archive를 만들지만 암호화·예약·원격 복제는
하지 않는다. Lightsail snapshot은 host 복구 보조 수단이며 application backup을 대체하지 않는다.
archive에는 `wp-config.php`의 database credential과 WordPress account hash가 포함된다. 복원할 때는
동일한 `.env` identity와 recovery secret set으로 먼저 기동한 뒤 지원되는 rotation을 수행한다.
임의의 새 password를 넣어 복원이 될 것이라고 가정하지 않는다.

### Security·operations

- [ ] `/healthz` 외에 실제 WordPress와 MariaDB까지 확인하는 readiness·synthetic read 추가
- [ ] 외부 uptime·TLS expiry·CPU·memory·disk·backup failure alarm 연결
- [ ] Docker published port와 UFW 우회 특성을 반영한 upstream firewall·DOCKER-USER 정책 검증
- [ ] host reboot 후 exact container·volume·health를 검사하는 reconciliation unit 추가
- [ ] access/error log의 IP·cookie·query·개인정보 retention과 접근 권한 승인
- [ ] WordPress login rate limit·2FA·brute-force 방어 결정
- [ ] SMTP 또는 password recovery·운영 알림 경로 결정
- [ ] auth salt·session 강제 폐기 절차 추가
- [ ] HSTS·CSP·Permissions-Policy 적용 범위 검토
- [ ] comment·upload·개인정보·cookie·privacy·moderation 정책 검토
- [ ] 장애 담당자, 비용 담당자와 계정 복구 담당자 지정

`/healthz`는 nginx가 무조건 반환하는 liveness다. WordPress나 MariaDB가 실패해도 `200`일 수 있으므로
이 URL 하나만으로 production traffic을 유지하거나 복구 완료를 선언하지 않는다.

## 5. 결제 전에 결정할 것

다음 항목의 현재 가격을 AWS·domain registrar·backup provider 화면에서 확인한다.

- [ ] Lightsail 서울 4GB Linux instance 1개
- [ ] instance automatic snapshot과 deploy 전 manual snapshot storage
- [ ] encrypted off-host backup storage와 API request·egress 비용
- [ ] domain 등록·연장 비용
- [ ] Cloudflare에서 선택한 plan과 필요한 security 기능
- [ ] 외부 uptime·certificate monitor
- [ ] transactional email을 쓸 경우 SMTP provider

Lightsail bundle에는 compute·system disk·전송량이 함께 포함되지만 snapshot과 외부 서비스는 별도다.
비용 경보가 없으면 resource를 만들지 않는다. AWS Budget은 실제 비용과 예측 비용 알림을 모두 켠다.

## 6. 계정과 사람 checkpoint

### GitHub

1. **직접 수행**: 개인 계정 2FA와 recovery code 보관을 확인한다.
2. **직접 수행**: 비공개 repository를 만들되 `.env`, `secrets/`, backup을 업로드하지 않는다.
3. **Codex와 수행**: 현재 clean `main`을 origin에 연결하고 full SHA를 확인한다.
4. **Codex와 수행**: 위 CI·release STOP을 해결한다.
5. **직접 수행**: required check·branch protection을 켠다.

### AWS

1. **직접 수행**: root account에 phishing-resistant MFA를 등록한다.
2. **직접 수행**: root는 일상 운영에 사용하지 않고 별도 관리자 identity를 만든다.
3. **직접 수행**: recovery email·전화와 결제 수단을 확인한다.
4. **직접 수행**: monthly cost budget과 actual·forecast notification을 만든다.
5. **기록**: account ID·budget ID만 남기고 access key는 만들지 않거나 최소화한다.

### Cloudflare

1. **직접 수행**: email을 검증한다.
2. **직접 수행**: 서로 다른 2FA 수단 두 개와 recovery code를 준비한다.
3. **직접 수행**: domain을 추가하고 registrar nameserver를 Cloudflare 값으로 변경한다.
4. **직접 수행**: DNSSEC 상태와 기존 mail·verification record가 보존됐는지 확인한다.
5. **STOP**: origin 검증 전에는 production hostname의 proxied A record를 만들지 않는다.

## 7. Lightsail instance 만들기

AWS Console에서 Lightsail로 이동해 `Create instance`를 선택한다.

| 화면 항목 | 선택 |
|---|---|
| Region | `Seoul` |
| Availability Zone | 기본값 또는 운영 기록에 남긴 zone |
| Platform | `Linux/Unix` |
| Blueprint | `OS Only` → `Ubuntu 24.04 LTS` |
| Plan | 4GB RAM·2 vCPU·80GB부터 시작 |
| SSH key | 새 전용 key 또는 승인된 기존 public key |
| Automatic snapshots | 활성화, UTC·KST window 기록 |
| Resource name | production임을 분명히 하는 이름 |

WordPress blueprint를 고르지 않는다. 생성 직후 다음을 수행한다.

1. **직접 수행**: Networking → `Create static IP`에서 같은 region의 IPv4를 붙인다.
2. **기록**: instance name·region·static IP resource ID를 남긴다. IP 자체 공개 범위를 검토한다.
3. **직접 수행**: snapshot이 실제 활성 상태이고 보존 정책이 보이는지 확인한다.
4. **직접 수행**: CPU·status check alarm과 알림 수신자를 만든다.
5. **STOP**: firewall을 제한하기 전 application을 실행하거나 DNS에 IP를 넣지 않는다.

## 8. Firewall 고정 — STOP

Lightsail instance의 Networking 화면에서 IPv4와 IPv6 firewall을 각각 설정한다.

### IPv4 inbound

| protocol·port | source | 이유 |
|---|---|---|
| TCP 22 | 현재 관리자 public IP `/32` | SSH |
| TCP 443 | 배포 시점의 공식 Cloudflare IPv4 ranges | proxied HTTPS |
| TCP 443 | bootstrap 동안만 관리자 IP `/32` | origin 직접 검증 |

### IPv6 inbound

- IPv6 origin을 운영하지 않으면 inbound를 모두 닫고 DNS에 AAAA record를 만들지 않는다.
- 운영한다면 TCP 22는 관리자 `/128`, TCP 443은 공식 Cloudflare IPv6 ranges만 허용한다.

### 항상 닫을 것

- TCP 80: DNS-01과 Cloudflare edge redirect를 사용하므로 origin에는 불필요
- TCP 3306: MariaDB
- TCP 9000: PHP-FPM
- TCP 2375·2376: Docker daemon
- 그 밖의 모든 inbound

Cloudflare IP range를 이 문서에 복사해 영구 고정하지 않는다. 공식 목록을 작업 당일 다시 확인하고
변경 알림 절차를 둔다. Cloudflare IP 외 443 traffic을 차단하지 않으면 origin IP를 알아낸 요청이
edge 방어를 우회한다.

Docker port publish는 UFW보다 먼저 처리되어 UFW 규칙을 우회할 수 있다. Lightsail firewall을 1차
경계로 사용하고 host 방어가 더 필요하면 검증된 `DOCKER-USER` policy를 추가한다. Docker의
`iptables`·`ip6tables` 기능을 끄지 않으며 Docker daemon을 TCP로 노출하지 않는다. `docker` group은
root와 같은 권한이므로 배포 관리자 한 명만 포함한다.

## 9. Host 준비

아래는 승인된 Ubuntu 24.04 OS-only instance에서만 수행한다. 명령은 실제 배포일의 Docker 공식
문서와 package version을 다시 확인한 뒤 실행한다.

1. **직접 수행**: SSH host fingerprint를 별도 경로로 확인하고 SSH key로 접속한다.
2. **직접 수행**: sudo 가능한 전용 deploy user를 만들고 새 session에서 key login을 확인한다.
3. **직접 수행**: 확인 후 root·password SSH를 비활성화한다.
4. **직접 수행**: timezone은 UTC로 유지하고 NTP 상태를 확인한다. 운영 기록에 KST 환산을 병기한다.
5. **직접 수행**: host security update와 unattended upgrade 정책을 적용한다.
6. **직접 수행**: Docker 공식 apt repository로 Engine·Buildx·Compose plugin을 설치한다.
7. **직접 수행**: Docker daemon을 network socket에 bind하지 않았는지 확인한다.
8. **직접 수행**: `/srv/container-stack` 아래 release·config·secret·backup staging 소유권을 고정한다.
9. **STOP**: reboot 뒤 SSH·Docker·firewall이 예상대로 돌아오는지 빈 host 상태에서 확인한다.

Docker convenience script는 개발·시험용이므로 사용하지 않는다. production host에 source compiler와
GitHub write token을 오래 남기지 않는다. 최종 목표는 GHCR의 read-only credential로 승인 digest만
pull하는 것이다.

## 10. Let’s Encrypt DNS-01 준비 — STOP

이 단계는 repository에 production certificate mount와 reload 명령이 구현된 뒤에만 진행한다.

1. **직접 수행**: Cloudflare에서 해당 zone의 `Zone:DNS:Edit`만 가진 전용 API token을 만든다.
2. **직접 수행**: token은 password manager와 root 소유 `0600` credentials file에만 저장한다.
3. **직접 수행**: Certbot 공식 snap과 `certbot-dns-cloudflare` plugin을 설치한다.
4. **직접 수행**: apex 또는 승인 subdomain에 필요한 certificate를 DNS-01로 발급한다.
5. **Codex와 수행**: `/etc/letsencrypt` renewal path를 nginx가 read-only로 읽게 한다.
6. **Codex와 수행**: nginx config test 후 reload하는 deploy hook을 고정한다.
7. **직접 수행**: `certbot renew --dry-run`을 실행해 발급·hook·reload를 함께 확인한다.
8. **직접 수행**: certificate SAN·issuer·expiry·key permission을 확인한다.
9. **STOP**: renewal 실패 alert와 만료 전 대응 담당자가 없으면 traffic을 열지 않는다.

private key를 image, named volume backup, Git 또는 diagnostics에 넣지 않는다. certificate renewal
directory와 Cloudflare token은 서로 다른 권한으로 보관한다.

## 11. Production config와 secret 준비

production release directory에서 `.env.example`을 그대로 쓰지 않고 owner-only `.env`를 만든다.

### 공개 설정

| 이름 | production 원칙 |
|---|---|
| `DOMAIN_NAME` | 승인한 canonical hostname 하나 |
| `WORDPRESS_URL` | 정확히 `https://` + canonical hostname |
| `HTTPS_BIND_ADDRESS` | firewall 검증 뒤 `0.0.0.0` |
| `HTTPS_PORT` | `443` |
| `STACK_IMAGE_PREFIX` | 승인 GHCR namespace |
| `STACK_IMAGE_TAG` | full release SHA 또는 immutable release ID |
| `MYSQL_DATABASE` | 영문·숫자·underscore의 production 전용 이름 |
| `MYSQL_USER` | production 전용 application user |
| `WORDPRESS_TITLE` | 실제 공개 site title |
| admin·author 이름·email | 실제 운영자 계정, 서로 다른 login |
| 네 `*_PASSWORD_FILE` | `/srv/container-stack/secrets/` 아래 각 file의 absolute path |

`admin`, `author`, `example.com`, `local`, `latest` 같은 예시값을 남기지 않는다. `.env`는 `0600`으로
두고 secret value를 넣지 않는다.

### 네 secret file

- `db_root_password.txt`
- `db_password.txt`
- `wp_admin_password.txt`
- `wp_user_password.txt`

각 값은 서로 달라야 하며 허용 문자로 된 24~128자 한 줄이어야 한다. secret directory는 deploy
user 소유 `0700`, 각 file은 같은 사용자 소유 `0600` regular file·단일 hard link·non-symlink여야
한다. 생성값을 command argument에 넣지 않는다.

```sh
umask 077
install -d -m 0700 /srv/container-stack/secrets
openssl rand -hex 32 > /srv/container-stack/secrets/db_root_password.txt
openssl rand -hex 32 > /srv/container-stack/secrets/db_password.txt
openssl rand -hex 32 > /srv/container-stack/secrets/wp_admin_password.txt
openssl rand -hex 32 > /srv/container-stack/secrets/wp_user_password.txt
chmod 600 /srv/container-stack/secrets/*.txt
```

이 예시는 값을 화면에 출력하지 않지만 생성 후 password manager의 recovery record와 연결하는
승인 절차가 별도로 필요하다. `cat`, shell debug, `docker inspect`, 완료 보고서에 값을 출력하지 않는다.

## 12. Release 만들기 — STOP

현재 지원 명령을 production에 사용하기 전에 CI·GHCR release 구현을 완료한다. 목표 순서는 다음이다.

1. clean `main`에서 full 40-character `RELEASE_SHA`를 고정한다.
2. required CI가 정적·runtime·backup/restore·rotation·operations를 모두 통과한다.
3. workflow가 세 image를 exact SHA로 한 번 빌드한다.
4. image source label·SBOM·scan 결과를 검증한다.
5. GHCR에 immutable tag와 digest로 publish한다.
6. deployment record에 nginx·WordPress·MariaDB 각 digest를 남긴다.
7. production host는 read-only package credential로 그 digest만 pull한다.
8. `docker compose config`에서 예상 image·volume·network·port·resource limit를 확인한다.
9. `make up` 경로가 local rebuild 없이 승인 image를 쓰는지 검증한다.

`make up-build`와 production host의 즉흥 build를 release 절차로 사용하지 않는다. migration과
WordPress update를 container start에 임의로 추가하지 않는다.

## 13. 첫 bootstrap

Cloudflare production DNS는 아직 열지 않고, Lightsail firewall에 관리자 IP의 443만 임시 허용한
상태에서 진행한다.

1. **기록**: `RELEASE_SHA`, 세 image digest, `PREVIOUS_RELEASE_SHA`, snapshot ID를 남긴다.
2. **직접 수행**: deploy release directory와 `.env`·secret permission을 다시 확인한다.
3. **직접 수행**: production certificate mount와 hostname이 예상대로 resolve되는지 확인한다.
4. **직접 수행**: 승인 image를 pull한다.
5. **직접 수행**: repository의 지원 경로로 `make up`을 실행한다.
6. **직접 수행**: `make ps`에서 nginx·WordPress·MariaDB가 모두 healthy인지 확인한다.
7. **직접 수행**: 새 private directory에 `make diagnostics DIAGNOSTICS_DIR=...`를 실행하고 startup
   error만 사람 검토한다. 자료를 외부로 보내지 않는다.
8. **직접 수행**: local liveness와 실제 WordPress page·admin login·upload를 각각 확인한다.
9. **직접 수행**: DNS 전환 전에는 canonical hostname을 Static IPv4로 임시 해석하는 `curl --resolve`
   또는 operator host mapping을 사용한다. WordPress URL을 IP 주소로 바꾸지 않는다.
10. **직접 수행**: certificate 검사는 `-k` 없이 hostname과 chain을 확인한다.
11. **직접 수행**: MariaDB가 host port에 없고 PHP-FPM 9000도 외부에서 닫혔는지 확인한다.

임시 host mapping은 acceptance 뒤 즉시 제거한다. IP 주소로 직접 접속하면 TLS hostname과 WordPress
canonical redirect를 함께 검증할 수 없다.

`make up`은 실패 전체를 자동 rollback하지 않는다. 일부 service만 남으면 `make ps`와 제한된 log를
확인한 뒤 원인을 고치고 같은 지원 명령으로 다시 수렴시킨다. 직접 `docker compose up`으로 bootstrap
lock과 secret 전달 경계를 우회하지 않는다.

## 14. 첫 backup·restore rehearsal — STOP

아직 공개 content가 거의 없을 때 실제 복구를 먼저 증명한다.

1. 세 runtime service가 healthy인지 확인한다.
2. 저트래픽 maintenance window를 선언한다.
3. 새 private staging path에 `make backup BACKUP_DIR=...`을 실행한다.
4. manifest·file mode·종료 후 runtime 재시작을 확인한다.
5. output을 검토된 도구로 client-side encryption한다.
6. off-host storage로 복제하고 remote checksum·object existence를 확인한다.
7. 원본과 다른 새 project name·빈 volume으로 restore한다.
8. 복원 site를 외부에 노출하지 않고 database·admin login·upload·page를 확인한다.
9. measured RPO·RTO와 receipt를 기록한다.
10. 검증한 뒤에만 local plaintext staging을 안전하게 제거한다.

restore는 기존 live volume 위에 덮어쓰는 기능이 아니다. 빈 project만 지원한다. 실패한 live stack을
`fclean`한 뒤 그 자리에 복원하지 말고 새 project에 복원·검증한 뒤 traffic target을 바꾼다.

## 15. Cloudflare 연결

origin bootstrap과 restore rehearsal이 모두 끝난 뒤 진행한다.

1. Cloudflare DNS → Records에서 canonical hostname의 `A` record를 Static IPv4로 만든다.
2. Proxy status를 `Proxied`로 켠다.
3. SSL/TLS → Overview에서 `Full (strict)`를 선택한다.
4. Edge Certificates에서 Universal SSL이 active인지 확인한다.
5. `Always Use HTTPS`를 켠다.
6. Minimum TLS Version을 최소 `1.2`로 둔다.
7. HTML 전체를 cache하는 `Cache Everything` rule은 만들지 않는다.
8. `/wp-admin/`, `/wp-login.php`, preview·authenticated response가 cache되지 않는지 확인한다.
9. Query String Sort 같은 기능을 임의로 켜지 않는다.
10. HSTS는 renewal·rollback·모든 subdomain HTTPS가 검증된 뒤 별도 승인한다.

Cloudflare 기본 cache는 정적 확장자를 대상으로 하고 HTML은 기본적으로 cache하지 않는다. 로그인
cookie가 있는 동적 WordPress 응답을 강제로 cache하면 다른 사용자에게 잘못된 내용이 노출될 수 있다.

## 16. 실제 traffic을 열기 전 acceptance

### Network·TLS

- [ ] DNS가 Cloudflare proxy IP를 반환
- [ ] origin 443은 Cloudflare IP와 임시 관리자 IP 외에는 차단
- [ ] 관리자 임시 443 rule 제거 후 Cloudflare 경유만 성공
- [ ] origin port 80·3306·9000·2375·2376 차단
- [ ] public certificate hostname·chain·expiry 정상
- [ ] HTTP가 HTTPS로 이동하고 redirect loop 없음
- [ ] Cloudflare SSL mode `Full (strict)`

### Application

- [ ] homepage와 대표 post가 익명 browser에서 정상
- [ ] `/wp-admin/` login·logout·session 정상
- [ ] admin과 author 권한이 서로 다름
- [ ] image upload·조회·삭제가 volume에 반영
- [ ] restart와 `make down`/`make up` 뒤 content·upload·account 유지
- [ ] 잘못된 Host와 예상하지 않은 domain 거부
- [ ] upload size·comment·registration 설정이 제품 정책과 일치
- [ ] password recovery 또는 운영자 복구 경로 확인

### Operations

- [ ] `/healthz` liveness와 실제 WordPress synthetic read를 분리 확인
- [ ] 세 container health와 restart count 정상
- [ ] CPU·memory·disk·status·uptime·TLS expiry alarm 수신 시험
- [ ] encrypted off-host backup 성공과 fresh-project restore 성공
- [ ] diagnostics에 secret이 없고 공유 전 사람 검토 절차 존재
- [ ] release·image digest·snapshot·backup receipt·rollback target 기록

이 항목이 모두 끝난 시점이 첫 배포 완료다. GitHub push, instance 생성, container `running`,
`/healthz 200` 중 하나만으로 배포 완료를 선언하지 않는다.

## 17. 배포·update 절차

1. staging restore에서 새 WordPress·image·config를 먼저 검증한다.
2. `RELEASE_SHA`와 `PREVIOUS_RELEASE_SHA`, image digest를 고정한다.
3. live application backup과 Lightsail manual snapshot을 만든다.
4. backup upload·checksum을 확인한다.
5. maintenance window를 시작한다.
6. 승인 image digest를 pull한다.
7. repository가 제공하는 release command로만 service를 교체한다.
8. Compose health→WordPress synthetic read→admin→upload→TLS 순서로 확인한다.
9. error rate·resource·log·backup scheduler를 관찰한다.
10. 이상이 없으면 release receipt를 `DEPLOYED`로 기록한다.

WordPress major update나 database schema 변화가 있으면 이전 image로 되돌리는 것만으로 안전하다고
가정하지 않는다. 이전 code가 새 schema를 읽는지 staging restore에서 먼저 증명한다.

## 18. Rollback과 복구

### 새 image만 문제이고 data schema가 호환될 때

1. 새 DNS·volume·database를 건드리지 않는다.
2. 승인된 이전 image digest로 release를 되돌린다.
3. 세 container health와 실제 WordPress read를 다시 확인한다.
4. 원인을 기록하고 새 release는 별도 SHA로 수정한다.

### content·database·volume이 손상됐을 때

1. live project를 삭제하거나 in-place restore하지 않는다.
2. incident 시점 이후의 쓰기 중단과 forensic 보존 범위를 승인한다.
3. 마지막 정상 encrypted backup을 새 project·새 volume에 복원한다.
4. exact `.env` identity와 recovery secret을 승인 경계에서 제공한다.
5. database·account·post·upload·plugin·theme·TLS를 검증한다.
6. 사람 승인 뒤 Static IP 또는 Cloudflare origin target을 새 instance로 전환한다.
7. 검증 전 기존 instance·volume·snapshot을 삭제하지 않는다.

### Host 전체가 문제일 때

1. Lightsail snapshot으로 새 instance를 만든다. 기존 instance에 덮어쓰지 않는다.
2. 또는 깨끗한 Ubuntu host에서 exact SHA와 encrypted application backup으로 재구성한다.
3. firewall·Docker·certificate·secret permission을 처음부터 다시 확인한다.
4. 새 origin을 Cloudflare에 연결하기 전 acceptance 전체를 반복한다.

`make fclean DESTROY_CONFIRM=...`은 WordPress와 MariaDB volume을 삭제한다. 복구·rollback 명령이
아니며, production data 폐기 승인과 verified backup이 없으면 실행하지 않는다.

## 19. 반복 운영

### 매일

- [ ] public HTTPS와 실제 WordPress page monitor 확인
- [ ] 세 container health·restart·disk·memory alarm 확인
- [ ] encrypted off-host application backup receipt 확인
- [ ] WordPress security notice와 비정상 login 징후 확인

### 매주

- [ ] host security update와 reboot 필요 여부 확인
- [ ] WordPress core·theme·plugin advisory와 pinned version 차이 검토
- [ ] failed backup·renewal·deploy·login 기록 검토
- [ ] comment·upload·account 권한 검토

### 매월

- [ ] fresh project application restore rehearsal
- [ ] Lightsail invoice·snapshot·storage·transfer 확인
- [ ] container image base·Debian snapshot·package pin 갱신 후보 검토
- [ ] certificate renewal timer와 다음 expiry 확인
- [ ] off-host retention과 오래된 object 삭제 검증

### 분기마다

- [ ] 새 instance 전체 복구 rehearsal과 RPO·RTO 측정
- [ ] 네 password rotation rehearsal
- [ ] WordPress auth salt·session invalidation rehearsal
- [ ] GitHub·AWS·Cloudflare·backup provider role·2FA·recovery code 검토
- [ ] origin firewall의 Cloudflare IP range 갱신 여부 확인
- [ ] application·data·host rollback rehearsal

## 20. 즉시 멈추고 도움을 요청할 경우

- CI가 current `main` exact SHA에서 완전 통과하지 않음
- image digest와 deployed container image ID가 다름
- production nginx가 self-signed certificate로 시작함
- Certbot renewal 또는 nginx reload 검증 실패
- Cloudflare `Full (strict)`에서 526·redirect loop·mixed content 발생
- origin 443이 Cloudflare 외 주소에서도 열림
- MariaDB 3306 또는 PHP-FPM 9000이 public에서 접근 가능
- `/healthz`는 성공하지만 WordPress page·database가 실패
- backup이 암호화·원격 복제·checksum 확인 중 하나라도 실패
- fresh-project restore가 account·post·upload를 복원하지 못함
- log·diagnostics·terminal에 password·token·private key가 노출됨
- WordPress update가 예상하지 않은 schema·plugin·theme 변경을 요구함
- disk 부족, 반복 restart, OOM 또는 database corruption 징후가 있음
- 이전 release가 현재 data schema를 읽는다는 evidence가 없음

이때 값을 추측해 바꾸거나 live volume·database를 직접 편집하지 않는다. secret을 제외한 error code,
resource ID, exact SHA, image digest와 발생 시각만 기록하고 다음 승인을 요청한다.

## 21. 배포 기록 양식

```text
상태: BLOCKED / READY / DEPLOYED / ROLLED_BACK
작업일(KST/UTC):
담당자:
Git RELEASE_SHA:
Git PREVIOUS_RELEASE_SHA:
nginx image digest:
WordPress image digest:
MariaDB image digest:
Lightsail instance ID:
Static IP resource ID:
pre-deploy snapshot ID:
application backup receipt:
restore rehearsal receipt:
TLS certificate expiry:
Cloudflare zone/record locator:
monitor/alert locator:
rollback target:
비고(비밀값 금지):
```

## 22. 공식 참고 문서

### AWS Lightsail

- [Region과 Availability Zone](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-regions-and-availability-zones-in-amazon-lightsail.html)
- [instance image 선택](https://docs.aws.amazon.com/lightsail/latest/userguide/compare-options-choose-lightsail-instance-image.html)
- [instance bundle 사양·가격](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html)
- [Static IP](https://docs.aws.amazon.com/lightsail/latest/userguide/lightsail-create-static-ip.html)
- [IPv4·IPv6 firewall](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-firewall-and-port-mappings-in-amazon-lightsail.html)
- [Automatic snapshot](https://docs.aws.amazon.com/en_en/lightsail/latest/userguide/amazon-lightsail-configuring-automatic-snapshots.html)
- [AWS root account 보안](https://docs.aws.amazon.com/IAM/latest/UserGuide/root-user-best-practices.html)
- [AWS Budget](https://docs.aws.amazon.com/cost-management/latest/userguide/create-cost-budget.html)

### Docker·GitHub

- [Ubuntu에 Docker Engine 설치](https://docs.docker.com/engine/install/ubuntu/)
- [Docker packet filtering과 firewall](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
- [Docker group 보안 경계](https://docs.docker.com/engine/install/linux-postinstall/)
- [Container image build best practices](https://docs.docker.com/build/building/best-practices/)
- [GitHub Container Registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)

### Cloudflare·TLS

- [Cloudflare에 domain 추가](https://developers.cloudflare.com/fundamentals/manage-domains/add-site/)
- [DNS record 생성](https://developers.cloudflare.com/dns/manage-dns-records/how-to/create-dns-records/)
- [Full strict](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/)
- [Cloudflare IP allowlist와 origin 보호](https://developers.cloudflare.com/fundamentals/concepts/cloudflare-ip-addresses/)
- [Always Use HTTPS](https://developers.cloudflare.com/ssl/edge-certificates/additional-options/always-use-https/)
- [Minimum TLS Version](https://developers.cloudflare.com/ssl/edge-certificates/additional-options/minimum-tls/)
- [HSTS 주의사항](https://developers.cloudflare.com/ssl/edge-certificates/additional-options/http-strict-transport-security/)
- [Cloudflare 2FA](https://developers.cloudflare.com/fundamentals/user-profiles/2fa/)
- [Let’s Encrypt challenge 종류](https://letsencrypt.org/docs/challenge-types/)
- [Certbot DNS plugin 설치](https://certbot.eff.org/instructions?os=snap&tab=wildcard&ws=other)
- [Certbot Cloudflare plugin](https://certbot-dns-cloudflare.readthedocs.io/en/stable/)
- [Certbot renewal hook](https://eff-certbot.readthedocs.io/en/stable/using.html)

### WordPress·backup

- [WordPress hardening](https://developer.wordpress.org/advanced-administration/security/hardening/)
- [WordPress backup](https://developer.wordpress.org/advanced-administration/security/backup/)
- [WordPress update](https://wordpress.org/documentation/article/updating-wordpress/)
- [WordPress release archive](https://wordpress.org/download/releases/)
- [restic 공식 문서](https://restic.readthedocs.io/en/stable/)
