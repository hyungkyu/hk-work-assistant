# Setup checklist

이 문서는 설치 명령을 자동으로 실행하지 않는다. 시스템 변경은 사용자가 검토한 후
직접 실행하거나 별도 승인을 거쳐 수행한다.

## 1. Docker 설치

Ubuntu 패키지 저장소에서 다음 패키지를 사용한다.

```bash
sudo apt-get install -y docker.io docker-compose-v2
sudo usermod -aG docker "$USER"
```

그룹 변경은 로그아웃 후 다시 로그인해야 적용된다. `docker` 그룹은 사실상 root와
유사한 권한을 가지므로 이 PC의 신뢰된 사용자만 포함해야 한다.

## 2. HDD 준비

현재 확인된 대상 후보는 `/dev/sda`의 2TB 디스크다. 파티션 생성과 포맷은 기존
데이터를 삭제할 수 있으므로, 실행 직전에 모델명·시리얼·파티션 상태를 다시 확인하고
사용자의 명시적 승인을 받는다.

최종 마운트 경로:

```text
/data
```

애플리케이션 원문 경로:

```text
/data/rlwrld-worklog
```

## 3. 환경 파일

`.env.example`을 `.env`로 복사하고 임의의 긴 PostgreSQL 비밀번호와 허용할 Google
계정을 설정한다. `.env`는 Git에 포함하지 않는다.

## 4. 컨테이너 검증

```bash
docker compose config
docker compose build
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:8080/healthz
```

현재 웹 API는 인증 계층을 붙이기 전이므로 Tailscale Serve에 노출하면 안 된다.
Google OAuth 로그인과 계정 allowlist가 구현된 후에만 원격 접근을 활성화한다.

앱 컨테이너는 호스트 사용자와 같은 UID/GID로 실행하고 `/data/rlwrld-worklog`를
읽기 전용으로 마운트한다. 실제 수집기는 이후 별도 서비스로 구성해 원문 디렉터리에만
쓰기 권한을 부여한다.

## 5. Slack 연결

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

먼저 읽기 권한과 워크스페이스 ID만 확인한다. 이 단계는 메시지를 저장하지 않는다.

```bash
docker compose --env-file .env -f compose.yaml -f compose.slack.yaml \
  run --rm collector-slack worklog doctor slack
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

테스트 결과를 확인하기 전에는 2년 백필이나 스케줄러를 실행하지 않는다.
# Legacy Google Drive mirror

The historical Slack and Google Calendar JSON in Drive is mirrored to the HDD before it is normalized. The mirror uses Drive read-only permission and never follows attachment links found inside JSON.

1. In Google Cloud Console, create an OAuth 2.0 Client ID of type **Desktop app** for the company account that can read the legacy folder. Enable the Google Drive API.
2. Download the client JSON to `secrets/google-client.json` and keep it out of Git.
3. Run the local login. A Google login/consent page opens on localhost:

   ```bash
   scripts/worklog-local.sh google-auth
   ```

4. Mirror both supported sources:

   ```bash
   scripts/worklog-local.sh legacy-drive-download \
     --folder-id 1oLHCQpKfYTJPC_TKzSJU8_Fi1Xv_cCpa \
     --source slack --source gcal \
     --archive-root /data/rlwrld-worklog
   ```

Files are stored below `/data/rlwrld-worklog/legacy/google_drive/daily_raw`. Every run writes a manifest with Drive IDs, timestamps, sizes, and SHA-256 hashes below `/data/rlwrld-worklog/legacy/manifests`. Duplicate sibling names are retained with a `__drive_<id>` suffix. Re-running is safe: identical files are skipped and replaced versions are retained with a `superseded-<sha>` suffix.
