# 환경 변수 레퍼런스

이 문서는 이 저장소가 읽는 모든 환경 변수를 한자리에 모은 것이다. 설치 순서는
[docs/setup.md](setup.md)에 있다.

행마다 **읽는 곳**을 `파일:줄` 로 적었다. 코드가 읽지 않는 변수는 그 사실을 표에
적어 두었다 — 이름만 보고 동작한다고 가정하면 안 되는 것이 두 개 있다.

## 값이 전달되는 경로

변수가 프로세스까지 도달하는 경로는 네 가지이고, 서로 겹치지 않는다.

| 경로 | 무엇이 읽나 | 무엇에 닿나 |
|---|---|---|
| `.env` (저장소 루트) | `docker compose`가 자동으로 읽음 | compose 파일 안의 `${...}` 치환. 컨테이너 안으로는 `environment:` 에 적힌 것만 들어간다 |
| `secrets/slack.env` | `compose.slack.yaml:6`의 `env_file` | `collector-slack` 컨테이너 **한 개**뿐 |
| 셸 환경 | `.venv/bin/worklog`를 직접 실행할 때 | 그 프로세스 |
| `systemctl --user import-environment` | `scripts/install-incoming-timer.sh:28-30` | `hkwa-incoming` / `hkwa-wake` 유닛 |

`.env`에 값을 적었다고 앱 컨테이너가 그것을 보는 것이 아니다. `compose.yaml`의 각
서비스 `environment:` 블록에 나열된 이름만 컨테이너 안으로 들어간다. 예를 들어
`LEDGER_ROOT`는 `.env.example`에 있지만 어떤 서비스의 `environment:`에도 없으므로
컨테이너 안에서는 설정되지 않은 상태다.

## 코드가 읽는 변수

`src/` 아래에서 `os.environ`으로 읽는 전부다.

| 이름 | 읽는 곳 | 기본값 | compose가 넘기나 | `.env.example`에 있나 |
|---|---|---|---|---|
| `DATABASE_URL` | `web.py:30`; `cli.py:262`, `:344`, `:560`, `:666`, `:725`, `:740`, `:762`, `:816` | 없음. 웹은 `RuntimeError("DATABASE_URL is required")` | 예 (`:?` 필수) | 예 |
| `RAW_ARCHIVE_ROOT` | `cli.py:318`, `:554`, `:650`; `collection_status.py:221`; `schedules.py:299` | `/data/rlwrld-worklog` | 예 | 예 |
| `APP_CONFIG_ROOT` | `admin_store.py:100`; `work_store.py:615`; `daily.py:106`; `github_client.py:64`; `collection_status.py:223`; `collection_progress.py:66`, `:81` | `~/.config/hk-work-assistant` | 예 (기본 `/config/hk-work-assistant`) | 예 |
| `LEDGER_ROOT` | `cli.py:787`; `collection_status.py:222` | `$RAW_ARCHIVE_ROOT/staging/ledger` | 아니오 | 예 |
| `LEGACY_ROOT` | `collection_status.py:227` | `$RAW_ARCHIVE_ROOT/legacy/claude/weekly` | 아니오 | 주석 처리됨 |
| `BACKFILL_ARCHIVE_ROOTS` | `collection_status.py:228` | 미설정이면 `discover_backfill_roots()`가 자동 탐색 | 아니오 | **없음** |
| `COLLECTION_PROGRESS_ROOT` | `collection_progress.py:66`, `:81` | `APP_CONFIG_ROOT`, 그다음 `~/.config/hk-work-assistant` | 아니오 | **없음** |
| `ADMIN_SESSION_SECURE` | `admin_web.py:177` | `false` | 예 | 예 |
| `EMERGENCY_LOGIN_ENABLED` | `admin_web.py:42` | `false` | 예 (리터럴 고정) | **없음** |
| `SLACK_USER_TOKEN` | `cli.py:287`; `daily.py:127`; `slack_collector.py:660` | 없음 | `collector-slack`만 (`env_file`) | **없음** (`secrets/slack.env`) |
| `SLACK_EXPECTED_TEAM_ID` | `cli.py:292`, `:319`; `daily.py:85` | 없음 | `collector-slack`만 (`env_file`) | **없음** (`secrets/slack.env`) |
| `NOTION_TOKEN` | `cli.py:620`; `daily.py:129` | 없음 | 아니오 | **없음** |
| `GOOGLE_TOKEN_PATH` | `cli.py:576`; `daily.py:119` | `secrets/google-token.json` (상대 경로) | 아니오 | **없음** |
| `GITHUB_TOKEN` | `github_client.py:70` | 없음 | 아니오 | **없음** |
| `GITHUB_ORG` | `cli.py:382` | `rlwrld` | 아니오 | **없음** |
| `GITHUB_MIRROR_ROOT` | `cli.py:384` | `/data/rlwrld-worklog/legacy/claude/weekly/scripts/github_mirrors` | 아니오 | **없음** |
| `GH_BIN` | `github_client.py:34` | `gh` | 아니오 | **없음** |
| `GIT_BIN` | `github_client.py:35` | `git` | 아니오 | **없음** |
| `SLURM_DUMP_BASE_URL` | `slurm_client.py:33` | `http://infra-node:8888` | 아니오 | **없음** |
| `WORKLOG_ACTOR` | `work_cli.py:215` | `local-cli` | 아니오 | **없음** |

### import 시점에 한 번만 읽는 변수

세 개는 함수 안이 아니라 모듈 최상위에서 읽힌다. 모듈이 import 된 뒤에
`os.environ`을 바꿔도 값이 반영되지 않는다. 프로세스를 띄우기 전에 설정해야 한다.

- `GH_BIN` — `github_client.py:34`, `GH_BIN = os.environ.get("GH_BIN") or "gh"`
- `GIT_BIN` — `github_client.py:35`
- `SLURM_DUMP_BASE_URL` — `slurm_client.py:33`,
  `DEFAULT_BASE_URL = os.environ.get(...) or "http://infra-node:8888"`

`slurm-collect --base-url`은 이 상수와 무관하게 인자로 덮어쓸 수 있다.

### 기본값에 주의가 필요한 항목

- `COLLECTION_PROGRESS_ROOT` — 읽기 함수 `progress_root()`는 최종적으로 홈 디렉터리로
  떨어지지만, 쓰기 함수 `configured_progress_root()`는
  `COLLECTION_PROGRESS_ROOT`도 `APP_CONFIG_ROOT`도 없으면 `None`을 돌려준다
  (`collection_progress.py:71-82`). 둘 다 없는 셸에서 수집기를 돌리면 진행 스냅샷이
  조용히 기록되지 않는다. 운영자의 홈에 파일을 만들지 않기 위한 의도적 설계다.
- `BACKFILL_ARCHIVE_ROOTS` — `os.pathsep`(리눅스에서 `:`)으로 구분한 목록이다.
  빈 문자열을 명시적으로 넣으면 "라이브 루트만 읽는다"는 뜻이 되고, 변수를 아예 두지
  않으면 자동 탐색이 돈다. 둘은 다른 동작이다(`collection_status.py:228-234`).
- `GOOGLE_TOKEN_PATH`와 `--client-secrets`, `--migrations-dir` 기본값은 상대 경로다.
  작업 디렉터리가 저장소 루트가 아니면 파일을 찾지 못한다.

## compose가 요구하지만 코드가 읽지 않는 변수

| 이름 | 선언 위치 | 실제 동작 |
|---|---|---|
| `ALLOWED_GOOGLE_EMAIL` | `compose.yaml:52`, `:79`, `:109` — `:?` 필수 | 저장소의 어떤 Python 코드도 읽지 않는다. 실제 허용 계정은 `APP_CONFIG_ROOT/settings.json`의 `allowed_google_email` 키다(`admin_store.py:17`, `:131`). 값이 비어 있으면 compose가 뜨지 않으므로 채워야 하지만, 무엇을 채우든 로그인 허용 대상은 바뀌지 않는다 |
| `OPENSEARCH_URL` | `compose.yaml:50`, `:77`, `:107` | 세 서비스에 전달되지만 읽는 코드가 없다. `opensearch` 컨테이너는 뜨고 애플리케이션은 그것을 사용하지 않는다 |

## compose 파일만 쓰는 변수

컨테이너 안으로 들어가지 않고, 배치와 게시 방법만 결정한다.

| 이름 | 쓰이는 곳 | 기본값 | 비고 |
|---|---|---|---|
| `POSTGRES_DB` | `compose.yaml:8` | 없음 (`:?`) | postgres 컨테이너 초기화 |
| `POSTGRES_USER` | `compose.yaml:9` | 없음 (`:?`) | |
| `POSTGRES_PASSWORD` | `compose.yaml:10` | 없음 (`:?`) | `DATABASE_URL` 안의 비밀번호와 같아야 한다 |
| `APP_PORT` | `compose.yaml:57`, `:58`, `:59` | `8080` | 호스트 쪽 포트. 컨테이너는 항상 8080 |
| `HOST_UID` / `HOST_GID` | `compose.yaml:47`, `:73`, `:103`; `compose.slack.yaml:4` | `1000` / `1000` | `Dockerfile:13`의 `USER 65532:65532`를 덮어쓴다. 마운트한 호스트 디렉터리의 소유자와 맞아야 한다 |
| `RAW_ARCHIVE_HOST_ROOT` | `compose.yaml:61`, `:89`, `:116`; `compose.slack.yaml:11` | `/data/rlwrld-worklog` | 호스트 경로. 앱·백오피스는 `:ro`, `collector-slack`만 `:rw` |
| `APP_CONFIG_HOST_ROOT` | `compose.yaml:62`, `:90`, `:117` | 없음 (`:?`) | 기동 전에 존재해야 한다. `scripts/prepare-admin-config.sh` 참고 |
| `VPN_BIND_ADDRESS` | `compose.yaml:58` | 없음 (`:?`) | 이 호스트의 Tailscale IPv4. 문자 그대로 바인드된다 |
| `LAN_BIND_ADDRESS` | `compose.yaml:59`, `:114` | 없음 (`:?`) | 이 호스트의 LAN IPv4. 8080과 8081 양쪽에 쓰인다 |
| `BACKOFFICE_BIND_ADDRESS` | `compose.yaml:87` | `127.0.0.1` | `LAN_BIND_ADDRESS`와 같은 값이면 8081이 충돌한다 |
| `SLACK_ENV_FILE` | `compose.slack.yaml:6` | `./secrets/slack.env` | `env_file` 경로 |

`.env.example`의 `VPN_BIND_ADDRESS=100.64.0.1`과 `LAN_BIND_ADDRESS=192.168.1.10`은
자리표시자다. `docker compose config` 출력에 `host_ip:` 값으로 문자 그대로 들어가므로,
호스트가 그 주소를 가지고 있지 않으면 바인드가 실패한다.

## 테스트가 읽는 변수

| 이름 | 읽는 곳 | 없을 때 |
|---|---|---|
| `WORKLOG_TEST_DATABASE_URL` | `tests/test_ledger_load.py:261`, `:270`, `:330`, `:339` | 실제 DB 왕복 테스트 2개를 건너뛴다. 나머지 전부는 DB 없이 통과한다 |

버려도 되는 데이터베이스만 가리켜야 한다. 이 테스트는 실제로 스키마를 만들고 행을
쓴다. `compose.ledger-test.yaml`이 그런 데이터베이스를 정의해 두었지만, 이 변수와
연결해 주는 문서나 스크립트는 없다.

## 스크립트가 읽는 변수

| 이름 | 읽는 곳 | 기본값 | 용도 |
|---|---|---|---|
| `APP_CONFIG_ROOT` | `scripts/wake-local.sh:25` | `$HOME/.config/hk-work-assistant` | 보드 파일 `work/items.json`과 로그 디렉터리 위치 |
| `APP_CONFIG_HOST_ROOT` | `scripts/prepare-admin-config.sh:5` | `${XDG_CONFIG_HOME:-$HOME/.config}/hk-work-assistant` | 만들 디렉터리 |
| `XDG_CONFIG_HOME` | `scripts/prepare-admin-config.sh:4` | `$HOME/.config` | 위 항목의 기반 |
| `WAKE_EXECUTOR` | `scripts/wake-local.sh:30` | `local` | 이 장비가 집어갈 항목의 `assigned_to` 값 |
| `WAKE_TIMEOUT` | `scripts/wake-local.sh:103` | `3600` (초) | 깨운 세션의 `timeout(1)` 한도 |
| `GH_TOKEN` | `scripts/install-incoming-timer.sh:29` | 없음 | 있으면 user manager로 넘긴다. 없으면 push가 git 자격 증명 헬퍼에 의존한다 |

`wake-local.sh:105`는 `$APP_CONFIG_ROOT/wake-local.env`가 있으면 source 한다.
`WAKE_TIMEOUT`과 도구 allowlist를 그 파일에서 덮어쓸 수 있다.

## `.env.example`에 없는데 코드·테스트·스크립트가 읽는 변수

열일곱 개다. 대부분 기본값이 있어 조용히 동작하므로, 기본값이 원하는 값이 아닐 때만
문제가 드러난다.

`BACKFILL_ARCHIVE_ROOTS`, `COLLECTION_PROGRESS_ROOT`, `EMERGENCY_LOGIN_ENABLED`,
`SLACK_USER_TOKEN`, `SLACK_EXPECTED_TEAM_ID`, `NOTION_TOKEN`, `GOOGLE_TOKEN_PATH`,
`GITHUB_TOKEN`, `GITHUB_ORG`, `GITHUB_MIRROR_ROOT`, `GH_BIN`, `GIT_BIN`,
`SLURM_DUMP_BASE_URL`, `WORKLOG_ACTOR`, `WORKLOG_TEST_DATABASE_URL`,
`WAKE_EXECUTOR`, `WAKE_TIMEOUT`.

이들은 `.env.example`에 주석 형태로 들어가 있다. 토큰류는 `.env`가 아니라
`secrets/slack.env` 또는 `APP_CONFIG_ROOT/credentials/` 아래에 둔다
(`src/rlwrld_worklog/daily.py:104-131`).
