# 설치 및 실행 체크리스트

이 문서는 설치 명령을 자동으로 실행하지 않는다. 시스템 변경은 사용자가 검토한 후
직접 실행하거나 별도 승인을 거쳐 수행한다.

순서에 의미가 있다. 관리 설정 디렉터리는 컨테이너 기동 **전에** 만들어야 하고,
데이터베이스 마이그레이션은 기동 **후에** 사람이 직접 적용해야 한다. 각각의 이유는
해당 절에 적어 두었다.

환경 변수 하나하나의 의미, 읽는 위치, 기본값은 [docs/environment.md](environment.md)에
정리되어 있다.

## 1. 개발 환경

Python 3.12 이상이 필요하다 (`pyproject.toml`의 `requires-python = ">=3.12"`).
Ubuntu 24.04의 기본 `python3`는 3.11이므로 인터프리터를 명시한다.

```bash
cd /path/to/hk-work-assistant
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install pytest
```

`pytest`는 `pyproject.toml`의 의존성에 들어 있지 않다. 별도 dev extra도 없으므로
위와 같이 따로 설치한다.

`pip install -e .`는 `worklog` 콘솔 스크립트를 `.venv/bin/worklog`에 만든다.
이것이 이 저장소의 유일한 콘솔 진입점이다.

```bash
.venv/bin/worklog --help
```

`.venv`는 `.gitignore`에 있다. 저장소에 커밋되지 않으므로 새로 받은 클론에서는
매번 위 네 줄을 실행해야 한다.

### 커밋 훅 활성화

`.githooks/pre-commit`은 저장소에 커밋되어 있지만 클론한 상태에서는 동작하지 않는다.
Git은 `core.hooksPath`가 설정된 경우에만 그 디렉터리를 본다.

```bash
git config core.hooksPath .githooks
```

훅은 수집 데이터 경로(`secrets/`, `data/`, `raw/`, `daily_raw/`, `manifests/`,
`exports/`, `backups/`, `logs/`, `checkpoints/`), 데이터 파일 확장자
(`.jsonl`, `.ndjson`, `.json.gz`, `.parquet`, `.sqlite`, `.db`, `.dump`, `.sql.gz`,
`.log`), 자격 증명 형태의 문자열, 1 MiB를 넘는 파일을 차단한다.
`tests/` 아래 파일은 앞 5줄 안에 `hook-allow: synthetic-credentials` 표식이 있으면
자격 증명 검사에서 제외된다.

## 2. 테스트

```bash
.venv/bin/python -m pytest -q
```

PostgreSQL 없이 전부 통과한다. `tests/test_ledger_load.py`의 실제 왕복 테스트 2개만
`WORKLOG_TEST_DATABASE_URL`이 없으면 건너뛴다. 그 2개까지 돌리려면 버려도 되는
데이터베이스를 가리킨 뒤 저장소 루트에서 실행한다. 테스트가
`Path("sql/migrations")`를 상대 경로로 읽으므로 작업 디렉터리가 루트여야 한다.

```bash
WORKLOG_TEST_DATABASE_URL="postgresql://user:pass@127.0.0.1:5432/throwaway" \
  .venv/bin/python -m pytest -q tests/test_ledger_load.py
```

운영 데이터베이스를 가리키면 안 된다. 이 테스트는 실제로 스키마를 만들고 행을 쓴다.

버려도 되는 PostgreSQL은 `compose.ledger-test.yaml`이 정의해 두었다
(`127.0.0.1:55432`, DB·사용자 `worklog_test`, 비밀번호 `disposable-only`, 데이터는
tmpfs). 이 파일은 다른 문서나 스크립트에서 참조되지 않으므로 쓰려면 직접 띄워야 한다.

`PYTHONPATH`에 `.python-packages`를 넣는 예전 형태는 더 이상 쓰지 않는다.
그 디렉터리는 저장소에 없고 `.gitignore`에 있으며, 시스템 `python3`(3.11)에는
`fastapi`와 `psycopg`가 없어 수집 단계에서 실패한다.

## 3. Docker 설치

Ubuntu 패키지 저장소에서 다음 패키지를 사용한다.

```bash
sudo apt-get install -y docker.io docker-compose-v2
sudo usermod -aG docker "$USER"
```

그룹 변경은 로그아웃 후 다시 로그인해야 적용된다. `docker` 그룹은 사실상 root와
유사한 권한을 가지므로 이 PC의 신뢰된 사용자만 포함해야 한다.

## 4. HDD 준비

현재 확인된 대상 후보는 `/dev/sda`의 2TB 디스크다. 파티션 생성과 포맷은 기존
데이터를 삭제할 수 있으므로, 실행 직전에 모델명·시리얼·파티션 상태를 다시 확인하고
사용자의 명시적 승인을 받는다.

`scripts/prepare-data-disk.sh`는 대상을 하드코딩하고 있다: 장치 `/dev/sda`,
모델 `ST2000VX017-3CV102`, 시리얼 `WWD534T4`, 소유자 `hk:hk`. 하나라도 일치하지
않으면 스크립트가 거부한다. 다른 장비에서 쓰려면 스크립트 상단의 상수를 먼저 고쳐야
한다.

최종 마운트 경로:

```text
/data
```

애플리케이션 원문 경로:

```text
/data/rlwrld-worklog
```

이 경로는 설정값일 뿐 아니라 코드의 기본값이기도 하다. `RAW_ARCHIVE_ROOT`가 없으면
`src/rlwrld_worklog/cli.py:318`, `:554`, `:650`,
`src/rlwrld_worklog/collection_status.py:221`,
`src/rlwrld_worklog/schedules.py:299`가 모두 `/data/rlwrld-worklog`로 떨어진다.

## 5. 관리 설정 디렉터리

**컨테이너를 올리기 전에 만든다.** `compose.yaml:62`는 `APP_CONFIG_HOST_ROOT`를
컨테이너 안으로 쓰기 가능하게 bind mount 한다. 호스트에 그 디렉터리가 없으면
Docker가 root 소유로 만들어 버리는데, 앱은 `HOST_UID:HOST_GID`(기본 1000:1000)로
돌고 `src/rlwrld_worklog/admin_store.py:91-96`이 그 디렉터리와 하위 `credentials/`에
무조건 `chmod(0o700)`을 걸기 때문에 요청 처리 중 `PermissionError`가 난다.

```bash
APP_CONFIG_HOST_ROOT="$HOME/.config/hk-work-assistant" \
  bash scripts/prepare-admin-config.sh
```

스크립트는 `APP_CONFIG_HOST_ROOT`를 읽고, 없으면
`${XDG_CONFIG_HOME:-$HOME/.config}/hk-work-assistant`를 쓴다. 디렉터리와
`credentials/`를 모드 0700으로 만든다. `.env`에 적을 `APP_CONFIG_HOST_ROOT`와 같은
값을 써야 한다.

## 6. 환경 파일

`.env.example`을 `.env`로 복사하고 값을 채운다. `.env`는 Git에 포함하지 않는다.

```bash
cp .env.example .env
```

채워야 하는 것:

- `POSTGRES_PASSWORD`와 `DATABASE_URL` 안의 비밀번호 — 임의의 긴 값으로 바꾼다.
  두 곳에 같은 값이 들어가야 한다.
- `APP_CONFIG_HOST_ROOT` — 5절에서 만든 실제 경로.
- `HOST_UID` / `HOST_GID` — `id -u`, `id -g` 값.
- `VPN_BIND_ADDRESS` / `LAN_BIND_ADDRESS` — 아래 주의 참고.

### 바인드 주소는 문자 그대로 바인드된다

`.env.example`의 `VPN_BIND_ADDRESS=100.64.0.1`과 `LAN_BIND_ADDRESS=192.168.1.10`은
자리표시자다. `compose.yaml:58`, `:59`, `:114`가 이 값을 포트 게시 주소로 그대로
쓴다. `docker compose config` 출력에 `host_ip: 100.64.0.1`처럼 문자 그대로 들어가는
것을 볼 수 있다. 호스트가 실제로 그 주소를 가지고 있지 않으면 바인드가 실패한다.
이 장비의 Tailscale IPv4와 LAN IPv4로 바꿔야 한다.

두 변수는 `:?` 형태로 선언되어 있어 값이 없으면 `docker compose config`조차
`required variable VPN_BIND_ADDRESS is missing a value`로 멈춘다.
`APP_CONFIG_HOST_ROOT`, `DATABASE_URL`, `POSTGRES_DB`, `POSTGRES_USER`,
`POSTGRES_PASSWORD`, `ALLOWED_GOOGLE_EMAIL`도 같은 형태다.

### ALLOWED_GOOGLE_EMAIL은 compose가 요구하지만 코드가 읽지 않는다

`compose.yaml:52`, `:79`, `:109`가 `ALLOWED_GOOGLE_EMAIL`을 `:?`로 요구하므로 비워
두면 compose가 뜨지 않는다. 그러나 저장소의 어떤 Python 코드도 이 변수를 읽지 않는다.
실제 허용 계정은 `APP_CONFIG_ROOT/settings.json`의 `allowed_google_email` 키이며
(`src/rlwrld_worklog/admin_store.py:17`, `:131`), 백오피스 화면에서 설정한다.
`.env`의 값을 바꿔도 로그인 허용 대상은 달라지지 않는다.

`OPENSEARCH_URL`도 세 서비스에 전달되지만 읽는 코드가 없다. OpenSearch 컨테이너는
뜨지만 애플리케이션은 아직 그것을 사용하지 않는다.

## 7. 컨테이너 빌드와 기동

```bash
docker compose config
docker compose build
docker compose up -d
docker compose ps
```

`build`가 `up`보다 먼저 와야 한다. `backoffice-local`과 `backoffice-lan`에는
`build:` 항목이 없고 `image: hk-work-assistant-app:local`만 있다
(`compose.yaml:71`, `:101`). 이미지가 로컬에 없으면 Docker는 레지스트리에서 받으려
시도하고 실패한다. 그 이미지를 만드는 것은 `app` 서비스의 `build: .` 뿐이다.

`up`은 다섯 개 서비스를 올린다: `postgres`, `opensearch`, `app`,
`backoffice-local`, `backoffice-lan`.

| 서비스 | 게시 포트 | 비고 |
|---|---|---|
| `app` | `127.0.0.1:8080`, `${VPN_BIND_ADDRESS}:8080`, `${LAN_BIND_ADDRESS}:8080` | 본 API |
| `backoffice-local` | `${BACKOFFICE_BIND_ADDRESS:-127.0.0.1}:8081` | `EMERGENCY_LOGIN_ENABLED="true"` |
| `backoffice-lan` | `${LAN_BIND_ADDRESS}:8081` | 비상 로그인 없음 |

세 서비스 모두 `user: "${HOST_UID:-1000}:${HOST_GID:-1000}"`로 실행되며, 이것이
`Dockerfile:13`의 `USER 65532:65532`를 덮어쓴다. `/data/rlwrld-worklog`는 읽기
전용으로, `APP_CONFIG_ROOT`는 쓰기 가능으로 마운트된다.

### 8081은 두 서비스가 함께 쓴다

`backoffice-local`과 `backoffice-lan`은 둘 다 컨테이너 8081을 호스트 8081로
게시한다. 다른 것은 호스트 IP뿐이다. `BACKOFFICE_BIND_ADDRESS`와
`LAN_BIND_ADDRESS`를 같은 값으로 두면 — 예를 들어 노트북에서 시험하려고 둘 다
`127.0.0.1`로 두면 — 나중에 뜨는 컨테이너가 포트 충돌로 실패한다. 한 대에서 둘 다
띄우려면 서로 다른 주소여야 한다. LAN 노출이 필요 없으면 서비스를 지정해서 올린다.

```bash
docker compose up -d postgres opensearch app backoffice-local
```

비상 로그인(초기 슈퍼관리자 비밀번호 생성)은 `backoffice-local`에서만 켜져 있다
(`compose.yaml:82`). 그 경로는 기본적으로 loopback에만 열린다.

### 상태 확인

```bash
curl -fsS http://127.0.0.1:8080/healthz
```

데이터베이스에 연결되면 `{"status":"ok"}`를 준다. `/healthz`는
`src/rlwrld_worklog/web.py:38-44`에서 실제로 DB 커넥션을 열기 때문에, DB가 닿지
않으면 503을 준다. 프로세스 생존 확인용 probe가 아니라 DB까지 포함한 확인이다.

## 8. 데이터베이스 스키마와 마이그레이션

두 단계가 있고, **compose는 두 번째 단계를 하지 않는다.**

**1) 베이스라인 `sql/schema.sql`.** `compose.yaml:13`이 이 파일을 postgres 컨테이너의
`/docker-entrypoint-initdb.d/001-schema.sql`로 마운트한다. initdb 스크립트는 데이터
볼륨이 비어 있을 때만 실행된다. `postgres_data` 볼륨이 이미 있는 상태에서
`schema.sql`을 고치고 재기동해도 아무 일도 일어나지 않는다.

**2) `sql/migrations/` 아래 네 개 파일.** 이것을 적용하는 것은
`worklog ledger-migrate` 뿐이다(`src/rlwrld_worklog/ledger/load.py:537`). 앱 기동 시
자동 적용은 없고, `daily-collect`나 `ledger-load`도 적용하지 않는다.

```bash
export DATABASE_URL="postgresql://worklog:...@127.0.0.1:5432/worklog"
.venv/bin/worklog ledger-migrate --database-url "$DATABASE_URL"
.venv/bin/worklog ledger-migrate --database-url "$DATABASE_URL" --apply
```

`--apply`가 없으면 계획만 보여 주고 롤백한다. 적용된 파일은 `schema_migrations`
테이블에 버전과 SHA-256 체크섬으로 기록되므로 재실행은 안전하다. 두 번째 실행은 모든
항목을 `already_applied`로 보고한다. 이미 적용된 파일의 내용이 나중에 바뀌면
`CHECKSUM_MISMATCH`로 보고한다(`load.py:570`).

현재 존재하는 마이그레이션:

| 파일 | 내용 |
|---|---|
| `0001_schema_migrations.sql` | `schema_migrations` 기록 테이블 |
| `0002_ledger_v1.sql` | `ledger_batches`, `ledger_records`, 파생 테이블 |
| `0003_live_capture.sql` | 라이브 캡처의 dimension 엔티티 타입 허용 |
| `0004_github_slurm_sources.sql` | GitHub·Slurm source와 엔티티 타입 허용 |

순서를 지켜야 한다. 베이스라인 없이 마이그레이션을 적용하면 `0002`가
`sql/schema.sql`이 만드는 `people` 테이블을 참조하다가
`psycopg.errors.UndefinedTable: relation "people" does not exist`로 멈춘다.

컨테이너 안에서는 이 명령을 실행할 수 없다. `Dockerfile:8-9`는 `pyproject.toml`,
`README.md`, `src/`만 복사하므로 이미지에 `sql/` 디렉터리가 없고, `--migrations-dir`의
기본값은 상대 경로 `sql/migrations`이다(`src/rlwrld_worklog/cli.py:164`).
호스트의 체크아웃에서 실행한다.

compose가 만든 DB가 아닌 곳에 베이스라인을 직접 넣어야 한다면 `psql`을 쓴다.

```bash
psql "$DATABASE_URL" -f sql/schema.sql
```

## 9. 원격 접근

Google OAuth 로그인과 계정 allowlist는 구현되어 있다.
`src/rlwrld_worklog/admin_web.py:313`이 `/auth/google/login`, `:367`이 콜백을
담당하고, `/api/v1/timeline`은 `require_company_session` 의존성으로 보호된다
(`src/rlwrld_worklog/web.py:54`). 세션 없이 호출하면 401이 돌아온다. 인증 없이 열려
있는 경로는 `/healthz` 뿐이다.

`compose.yaml:58`은 기본 설정에서 이미 8080을 `${VPN_BIND_ADDRESS}`(이 장비의
Tailscale IPv4)에 게시한다. VPN 노출은 선택 사항이 아니라 현재의 기본 동작이다.
VPN에 열고 싶지 않다면 그 포트 항목을 지우거나 `127.0.0.1`로 바꿔야 한다.

로그인을 실제로 쓰려면 다음이 필요하다.

- `APP_CONFIG_ROOT/credentials/` 아래의 Google OAuth 클라이언트 파일. 없으면
  `/auth/google/login`이 503 `Google OAuth client is not configured`를 준다
  (`admin_web.py:318-319`).
- `APP_CONFIG_ROOT/settings.json`의 `allowed_google_email`. 6절 참고.
- HTTPS 뒤에서 운영한다면 `ADMIN_SESSION_SECURE=true`. 이 값은 세션 쿠키의 `secure`
  속성을 결정한다(`admin_web.py:177`).

초기 슈퍼관리자 비밀번호는 `backoffice-local`(8081)의 `/backoffice`에서 만든다.

## 10. Slack 연결

Slack 앱 관리 화면에서 **From an app manifest**로 새 내부 앱을 만들고
`integrations/slack/app-manifest.yaml`의 내용을 사용한다. 이 매니페스트에는 쓰기
권한이 없고 User Token 읽기 범위만 있다. `files:read`는 파일 자체를 내려받기 위한
것이 아니라 메시지에 포함된 파일 메타데이터와 링크를 보존하기 위해 사용한다.

앱을 RLWRLD 워크스페이스에 설치한 후 **OAuth & Permissions**의
**User OAuth Token**(`xoxp-`로 시작)을 복사한다. 토큰을 채팅이나 이슈에 붙이지 말고
다음 스크립트의 숨김 입력창에 직접 넣는다.

```bash
./scripts/install-slack-secret.sh
```

스크립트는 `secrets/slack.env`를 모드 0600으로 만들고 `SLACK_USER_TOKEN`과
`SLACK_EXPECTED_TEAM_ID` 두 줄을 쓴다. 이 파일은 `compose.slack.yaml`의 `env_file`로만
전달된다. `compose.yaml`의 `app` 서비스에는 `env_file`이 없으므로 앱 컨테이너는 이
토큰을 보지 못한다. 앱 쪽에서 쓰려면 `APP_CONFIG_ROOT/credentials/` 아래에 두어야
한다(`src/rlwrld_worklog/daily.py:104-131`).

먼저 읽기 권한과 워크스페이스 ID만 확인한다. 이 단계는 메시지를 저장하지 않는다.

```bash
docker compose --env-file .env -f compose.yaml -f compose.slack.yaml \
  run --rm collector-slack worklog doctor slack
```

`compose.slack.yaml`은 단독으로 쓸 수 없다. `postgres` 서비스와 `frontend`,
`backend` 네트워크를 참조하면서 정의하지는 않으므로 항상 `-f compose.yaml`과 함께
겹쳐야 한다.

호스트에서 직접 확인하려면 같은 하위 명령을 콘솔 스크립트로 부른다. 토큰이 없으면
`SLACK_USER_TOKEN is required; run scripts/install-slack-secret.sh`를 출력하고 1로
종료한다.

```bash
.venv/bin/worklog doctor slack
```

그 다음 최근 24시간의 최대 3개 대화, 최대 20개 메시지만 테스트 영역에 수집한다.
원문 API 페이지는 gzip으로 저장되고, 첨부파일은 메타데이터와 링크만 남으며, 정규화된
이벤트는 PostgreSQL에 upsert된다.

```bash
docker compose --env-file .env -f compose.yaml -f compose.slack.yaml \
  run --rm collector-slack worklog collect slack \
  --since 24h --environment test --max-channels 3 --max-messages 20
```

수집 결과는 `/data/rlwrld-worklog/raw/slack/test/`에, 실행 매니페스트와 SHA-256은
`/data/rlwrld-worklog/manifests/slack/test/`에 저장된다. 제한 때문에 중간에 멈춘
실행은 `truncated=true`로 표시하고 체크포인트를 갱신하지 않는다.

`collector-slack`은 원문 디렉터리를 **쓰기 가능**으로 마운트하는 유일한 서비스다
(`compose.slack.yaml:11`). `app`과 두 백오피스는 같은 경로를 읽기 전용으로
마운트한다.

테스트 결과를 확인하기 전에는 2년 백필이나 스케줄러를 실행하지 않는다.

## 11. 로컬 CLI 실행

설치된 콘솔 스크립트를 직접 쓴다.

```bash
.venv/bin/worklog --help
.venv/bin/worklog work meta --config-root "$HOME/.config/hk-work-assistant"
```

같은 것을 모듈로 부를 수도 있다.

```bash
.venv/bin/python -m rlwrld_worklog --help
```

`scripts/worklog-local.sh`는 `$PROJECT_ROOT/.python-packages`를 `PYTHONPATH`에 넣고
시스템 `python3`를 부른다. 그 디렉터리는 저장소에 없고, 이 장비의 시스템 `python3`는
3.11이며 `fastapi`·`psycopg`·`googleapiclient`가 없다. 인자 파싱만 하는 `--help`는
통과하지만 실제 작업은 import 단계에서 실패한다. 위의 `.venv` 형태를 사용한다.

여러 명령이 상대 경로 기본값을 가지므로 저장소 루트에서 실행하는 것을 전제로 한다:
`ledger-migrate --migrations-dir`(기본 `sql/migrations`),
`google-auth --client-secrets`(기본 `secrets/google-client.json`),
`google-auth --token`(기본 `secrets/google-token.json`).

## 12. 레거시 Google Drive 미러

Drive에 있는 과거 Slack·Google Calendar JSON은 정규화 전에 HDD로 먼저 미러링한다.
미러는 Drive 읽기 전용 권한을 쓰고, JSON 안에서 발견한 첨부 링크는 따라가지 않는다.

1. Google Cloud Console에서 레거시 폴더를 읽을 수 있는 회사 계정으로 **Desktop app**
   유형의 OAuth 2.0 Client ID를 만든다. Google Drive API를 활성화한다.
2. 클라이언트 JSON을 `secrets/google-client.json`에 두고 Git에 넣지 않는다.
   `.gitignore:39-42`가 `client_secret*.json`, `credentials*.json`,
   `google-token*.json`, `token.json`을 차단한다.
3. 로컬 로그인을 실행한다. localhost에서 Google 로그인·동의 화면이 열린다.

   ```bash
   .venv/bin/worklog google-auth
   ```

   `secrets/google-client.json`이 없으면
   `FileNotFoundError: [Errno 2] No such file or directory: 'secrets/google-client.json'`으로
   종료한다. 다른 위치를 쓰려면 `--client-secrets`로 지정한다.

4. 지원하는 두 소스를 미러링한다.

   ```bash
   .venv/bin/worklog legacy-drive-download \
     --folder-id 1oLHCQpKfYTJPC_TKzSJU8_Fi1Xv_cCpa \
     --source slack --source gcal \
     --archive-root /data/rlwrld-worklog
   ```

   `--folder-id`의 기본값이 위와 같은 값이므로 생략할 수 있다
   (`src/rlwrld_worklog/cli.py:128`).

파일은 `/data/rlwrld-worklog/legacy/google_drive/daily_raw` 아래에 저장된다. 매 실행은
Drive ID, 타임스탬프, 크기, SHA-256을 담은 매니페스트를
`/data/rlwrld-worklog/legacy/manifests` 아래에 쓴다. 이름이 겹치는 형제 파일은
`__drive_<id>` 접미사를 붙여 함께 보존한다. 재실행은 안전하다: 동일한 파일은 건너뛰고,
교체된 버전은 `superseded-<sha>` 접미사로 남긴다.
