#!/usr/bin/env python3
"""Compose 관리 도구가 공유하는 프로젝트·비밀 파일 경계를 제공합니다."""

# 파일 최상단의 이런 문자열 리터럴은 모듈 docstring — import한 쪽에서 help(module)로 볼 수 있는 모듈 설명
from __future__ import annotations
# 파일 안에서 타입 힌트(dict[str, object] 같은)를 실행 시점에 평가하지 않고 문자열처럼 지연 평가하게 만드는
# 선언 — 옛 파이썬 버전 문법 제약을 우회하기 위한 관용구. 아래 나머지 코드의 동작에는 영향이 없다

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import BinaryIO, Iterator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE_FILE = ROOT / "srcs" / "docker-compose.yml"
PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,62}$")
PASSWORD_PATTERN = re.compile(r"^[A-Za-z0-9_.~!@#%^+=,-]{24,128}$")
SECRET_FILENAMES = {
    "db_root_password": "db_root_password.txt",
    "db_password": "db_password.txt",
    "wp_admin_password": "wp_admin_password.txt",
    "wp_user_password": "wp_user_password.txt",
}
# 이 세 O_* 플래그는 플랫폼에 따라 아예 존재하지 않을 수 있다(예: 일부 O_NOFOLLOW 미지원 환경) —
# getattr(모듈, 이름, 기본값)으로 없으면 "아무 효과 없는 값(0)"으로 대체해 이식성을 확보하는 관용구
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)


class StackRuntimeError(RuntimeError):
    # 이 도구 모음 전용 예외 타입 — 표준 예외와 구분해서 잡아낼 수 있도록 별도로 정의만 해두고 동작은 그대로 상속
    pass


class ComposeProject:
    """하나의 docker compose 실행 단위(프로젝트명·env 파일·compose 파일 조합)를 감싸는 값 객체."""

    def __init__(
        self,
        project: str,
        env_file: Path,
        compose_file: Path = DEFAULT_COMPOSE_FILE,
        *,
        # "*" 이후의 인자는 위치 인자로 넘길 수 없고 반드시 이름을 붙여서만(timeout=...) 넘겨야 하는
        # 키워드 전용 인자 — 호출부에서 인자 순서 실수를 막기 위한 문법
        timeout: int = 300,
    ) -> None:
        if not PROJECT_PATTERN.fullmatch(project):
            raise StackRuntimeError(
                "프로젝트 이름은 소문자·숫자·밑줄·하이픈 3~63자여야 합니다"
            )
        if timeout < 1 or timeout > 3600:
            raise StackRuntimeError("Compose 제한 시간은 1~3600초여야 합니다")
        self.project = project
        # resolve(strict=True): 상대경로·심볼릭 링크·".."를 모두 풀어 하나의 정규 절대경로로 만들고,
        # 실제로 존재하지 않으면 그 자리에서 예외를 던진다 — "나중에 열 때 실패"가 아니라 "구성 시점에 즉시 실패"
        self.env_file = env_file.expanduser().resolve(strict=True)
        self.compose_file = compose_file.expanduser().resolve(strict=True)
        self.timeout = timeout

    def command(self, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--env-file",
            str(self.env_file),
            "--file",
            str(self.compose_file),
            *arguments,
        ]

    def run(
        self,
        *arguments: str,
        input_data: bytes | None = None,
        input_stream: BinaryIO | None = None,
        capture: bool = False,
        check: bool = True,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        if input_data is not None and input_stream is not None:
            raise StackRuntimeError("subprocess 입력 형식을 하나만 지정해야 합니다")
        try:
            # subprocess.run(check=True): 자식 프로세스가 0이 아닌 종료 코드를 내면 자동으로 예외를 던진다.
            # capture 여부로 stdout/stderr를 파이프로 받을지, 부모 프로세스 화면에 그대로 흘려보낼지 갈린다
            return subprocess.run(
                self.command(*arguments),
                cwd=ROOT,
                input=input_data,
                stdin=input_stream,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                check=check,
                timeout=self.timeout if timeout is None else timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise StackRuntimeError(
                f"Docker Compose 명령이 {error.timeout}초 안에 끝나지 않았습니다"
            ) from error

    def config(self) -> dict[str, object]:
        # `docker compose config`는 여러 겹의 환경변수 치환·기본값 적용까지 모두 끝낸 "최종 확정 설정"을
        # JSON으로 뽑아준다 — docker-compose.yml의 x-secret-files 같은 확장 필드를 이 도구가 직접 파싱하지 않고
        # Compose 자신에게 물어봐서 얻는 것 (파일을 두 곳에서 따로 파싱하며 어긋날 위험을 없앤다)
        result = self.run("config", "--format", "json", capture=True)
        try:
            parsed = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StackRuntimeError(
                f"Compose 설정 JSON을 읽을 수 없습니다: {error}"
            ) from error
        if not isinstance(parsed, dict):
            raise StackRuntimeError("Compose 설정이 객체 형식이 아닙니다")
        return parsed

    def running_services(self) -> set[str]:
        result = self.run(
            "ps",
            "--status",
            "running",
            "--services",
            capture=True,
        )
        return {
            line
            for line in result.stdout.decode(errors="replace").splitlines()
            if line
        }


def _private_directory(path: Path, description: str) -> None:
    # 함수명 앞의 "_"는 파이썬 관용적인 "모듈 내부 전용" 표시 — 강제되진 않지만 외부에서 import해 쓰지 말라는 신호
    try:
        # os.stat이 아니라 os.lstat — 경로가 심볼릭 링크일 때 그 링크가 가리키는 대상이 아니라
        # 링크 자체의 정보를 확인한다(디렉터리 검사 자리에 심볼릭 링크가 슬쩍 들어오는 것을 그대로 통과시키지 않기 위함)
        info = os.lstat(path)
    except OSError as error:
        raise StackRuntimeError(f"{description}을 확인할 수 없습니다: {path}") from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        # 0o077은 "그룹/기타 사용자"에 해당하는 권한 비트 마스크 — 비트 AND 결과가 0이 아니면
        # 소유자 외에 읽기/쓰기/실행 권한이 하나라도 새어나가 있다는 뜻
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise StackRuntimeError(
            f"{description}은 현재 사용자만 접근하는 일반 디렉터리여야 합니다: {path}"
        )


def read_private_secret(path: Path) -> str:
    path = path.expanduser()
    _private_directory(path.parent, "비밀 파일 상위 디렉터리")
    try:
        # O_NOFOLLOW: 경로 마지막 구성요소가 심볼릭 링크면 open 자체를 실패시킨다 — "먼저 확인하고 나중에 연다"는
        # 두 단계 검사(TOCTOU 취약)가 아니라, 커널이 open 시점에 원자적으로 링크 여부를 판정하게 만든다.
        # O_NONBLOCK: 경로가 이름 있는 파이프(FIFO)처럼 열자마자 블로킹될 수 있는 특수 파일이어도 즉시 반환되게 함 —
        # 비밀 파일 자리에 그런 특수 파일을 심어 스크립트를 무한 대기시키는 공격을 막는다
        descriptor = os.open(path, os.O_RDONLY | NOFOLLOW | NONBLOCK)
    except OSError as error:
        raise StackRuntimeError(f"비밀 파일을 안전하게 열 수 없습니다: {path}") from error
    try:
        # 경로가 아니라 이미 열린 파일 디스크립터를 stat — 이 검사와 실제 읽기가 같은 대상을 보고 있음을 보장한다
        # (경로로 다시 stat하면 그 사이에 다른 파일로 바뀌어치기 당할 여지가 생김)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            # st_nlink(하드링크 개수)가 1이 아니면 이 파일이 다른 경로에서도 접근 가능하다는 뜻 —
            # 비밀 파일은 단일 경로로만 참조되는 상태를 강제한다
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise StackRuntimeError(
                f"비밀 파일은 현재 사용자가 소유한 0600 단일 링크 일반 파일이어야 합니다: {path}"
            )
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            # fdopen이 성공하면 이 파일 객체가 디스크립터의 소유권을 넘겨받아 with 블록이 끝날 때 알아서 닫는다 —
            # descriptor를 -1로 지워둬서, 아래 finally에서 같은 디스크립터를 이중으로 close하지 않게 막는다
            descriptor = -1
            # 허용 크기(1024자)보다 한 글자 더 읽어서, 그 한 글자가 실제로 존재하면(=파일이 더 길면) 바로 걸러낸다 —
            # 별도로 파일 크기를 다시 조회할 필요 없이 읽기 한 번으로 크기 제한을 검증하는 방식
            value = stream.read(1025)
            if len(value) > 1024 or stream.read(1):
                raise StackRuntimeError(f"비밀 파일이 허용 크기를 넘었습니다: {path}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    value = value.removesuffix("\n")
    if not PASSWORD_PATTERN.fullmatch(value):
        raise StackRuntimeError(
            f"비밀값은 허용 문자로 작성한 24~128자 한 줄이어야 합니다: {path.name}"
        )
    return value


def secret_source_paths(
    config: dict[str, object],
    *,
    compose_directory: Path | None = None,
) -> dict[str, Path]:
    # 시크릿 파일 경로를 이 스크립트에 하드코딩하지 않고, docker-compose.yml의 x-secret-files 확장 필드를
    # 그대로 읽어온다 — 경로 정의가 compose 파일 한 곳에만 있도록 하는 설계(단일 진실 공급원)
    configured = config.get("x-secret-files")
    if not isinstance(configured, dict):
        raise StackRuntimeError("Compose x-secret-files 설정을 찾을 수 없습니다")
    paths: dict[str, Path] = {}
    for name in SECRET_FILENAMES:
        entry = configured.get(name)
        if not isinstance(entry, str):
            raise StackRuntimeError(f"Compose secret 원본을 찾을 수 없습니다: {name}")
        path = Path(entry)
        if not path.is_absolute():
            if compose_directory is None:
                raise StackRuntimeError(
                    f"상대 secret 경로의 기준 디렉터리가 없습니다: {name}"
                )
            path = compose_directory / path
        paths[name] = Path(os.path.abspath(path))
    canonical = [path.resolve(strict=True) for path in paths.values()]
    # 네 개의 시크릿 이름이 우연히 같은 물리 파일을 가리키는 설정 실수(예: db_password와 wp_admin_password가
    # 같은 파일)를 집합의 크기 비교만으로 잡아낸다
    if len(set(canonical)) != len(canonical):
        raise StackRuntimeError("Compose secret 원본 경로는 서로 달라야 합니다")
    return paths


def load_secret_values(project: ComposeProject) -> dict[str, str]:
    paths = secret_source_paths(
        project.config(),
        compose_directory=project.compose_file.parent,
    )
    return {name: read_private_secret(path) for name, path in paths.items()}


def service_environment(
    config: dict[str, object], service: str
) -> dict[str, str]:
    services = config.get("services")
    if not isinstance(services, dict):
        raise StackRuntimeError("Compose 서비스 설정을 찾을 수 없습니다")
    configured = services.get(service)
    if not isinstance(configured, dict):
        raise StackRuntimeError(f"Compose 서비스를 찾을 수 없습니다: {service}")
    environment = configured.get("environment")
    if not isinstance(environment, dict):
        raise StackRuntimeError(f"서비스 환경 변수를 찾을 수 없습니다: {service}")
    return {str(key): str(value) for key, value in environment.items()}


def secret_payload(*values: str) -> bytes:
    # 값을 줄바꿈으로 이어붙여 바이트로 만든다 — 이 바이트열이 그대로 mariadb/wordpress entrypoint 스크립트의
    # bootstrap()이 `IFS= read -r`로 한 줄씩 읽어가는 표준입력이 된다 (entrypoint 쪽 주석 참고)
    for value in values:
        if not PASSWORD_PATTERN.fullmatch(value):
            raise StackRuntimeError("표준 입력 비밀값의 형식이 올바르지 않습니다")
    return ("\n".join(values) + "\n").encode()


@contextmanager
# @contextmanager: 이 제너레이터 함수를 `with project_operation_lock(...):` 형태로 쓸 수 있게 바꿔주는 데코레이터.
# yield 이전 코드가 with 블록 진입 시, yield 이후(finally 안 포함) 코드가 블록 종료 시(예외 발생 여부와 무관하게) 실행된다.
# 즉 "잠금을 걸고 → 블록 실행 → 무슨 일이 있어도 잠금 해제"를 try/finally 없이 호출부에 강제하는 패턴
def project_operation_lock(project_name: str) -> Iterator[None]:
    # 잠금 디렉터리 이름에 UID를 넣어서, 여러 사용자가 함께 쓰는 서버에서 다른 사용자의 잠금 디렉터리와
    # 섞이거나 그걸 건드리지 못하게 사용자별로 완전히 분리한다
    lock_directory = Path("/tmp") / f"container-stack-operation-locks-{os.getuid()}"
    try:
        lock_directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    # mkdir의 mode 인자는 umask에 따라 실제 권한이 달라질 수 있고, 디렉터리가 이미 존재하던 경우엔 아예
    # 적용되지 않는다 — 그래서 생성 뒤 별도로 소유자·권한을 명시적으로 재검증한다(위 mkdir을 신뢰하지 않음)
    _private_directory(lock_directory, "관리 작업 잠금 디렉터리")
    # 디렉터리 자체를 디스크립터로 열어두고 아래에서 dir_fd로 사용 — 이후 그 디렉터리 아래에 파일을 열 때
    # 경로 문자열이 아니라 이 고정된 디스크립터를 기준으로 삼는다. 디렉터리가 중간에 다른 것으로 바뀌어도
    # (경로 기반 접근과 달리) 이 디스크립터가 가리키는 대상 자체는 바뀌지 않는다
    directory_descriptor = os.open(
        lock_directory,
        os.O_RDONLY | DIRECTORY | NOFOLLOW,
    )
    # 프로젝트 이름을 그대로 파일명으로 쓰지 않고 해시로 변환 — 파일명에 경로 구분자 등 위험한 문자가
    # 섞여 들어올 가능성 자체를 차단한다
    lock_name = hashlib.sha256(project_name.encode()).hexdigest() + ".lock"
    lock_descriptor: int | None = None
    try:
        lock_descriptor = os.open(
            lock_name,
            os.O_RDWR | os.O_CREAT | NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        # os.open의 mode(0o600)는 파일이 새로 생성될 때만 적용된다 — 이미 존재하던 잠금 파일이 다른 권한을
        # 갖고 있었을 가능성까지 대비해 fchmod로 다시 한번 강제한다
        os.fchmod(lock_descriptor, 0o600)
        info = os.fstat(lock_descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise StackRuntimeError("관리 작업 잠금 파일이 안전하지 않습니다")
        try:
            # flock(LOCK_EX | LOCK_NB): 배타적 잠금을 시도하되, 이미 다른 프로세스가 쥐고 있으면 기다리지 않고
            # 즉시 BlockingIOError로 실패한다 — 같은 프로젝트에 대한 백업/복원/시크릿 회전 등 관리 작업이
            # 동시에 두 개 실행되는 것을 조용히 대기시키는 대신 바로 알려주기 위한 선택
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StackRuntimeError(
                "같은 프로젝트의 다른 관리 작업이 실행 중입니다"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        os.close(directory_descriptor)
