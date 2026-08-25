# RLWRLD Worklog

RLWRLD의 Slack, Google Calendar, GitHub 활동을 로컬에서 수집하고 기계적으로
정규화해 하나의 검색 가능한 타임라인으로 만드는 개인 업무 인텔리전스 시스템입니다.

## 안전 경계

- 운영 수집은 이 저장소의 로컬 프로그램이 수행합니다.
- Codex/ChatGPT 플러그인은 운영 수집 경로로 사용하지 않습니다.
- 운영 자격증명은 Git에서 제외한 로컬 `secrets/`에 mode 0600으로 둡니다.
- 수집기는 읽기 전용 권한만 사용합니다.
- 첨부파일 본문은 다운로드하지 않고 메타데이터와 링크만 보존합니다.
- OpenAI에는 사용자가 명시적으로 선택한 데이터만 전달합니다.

## 저장 위치

- 코드, PostgreSQL, OpenSearch: NVMe
- 원본 API 응답, manifest, export, 장기 로그: `/data/rlwrld-worklog/`

자세한 설계는 [docs/architecture.md](docs/architecture.md)를 참고하세요.

## 현재 단계

정규화 코어와 Slack 읽기 전용 수집기가 구현되어 있습니다. 단위 테스트는 외부 API에
접속하지 않으며, 실제 연결은 사용자가 만든 Slack 앱의 User OAuth Token으로만
실행합니다.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m rlwrld_worklog fixtures \
  --input tests/fixtures \
  --output /tmp/rlwrld-worklog-events.jsonl \
  --self-slack-user-id U0SELF
```

테스트 인프라는 다음 명령으로 시작합니다. 앱 포트만 호스트의
`127.0.0.1:8080`에 게시되며 PostgreSQL과 OpenSearch는 Docker 내부 네트워크에만
노출됩니다.

```bash
docker compose --env-file .env up -d --build
curl -fsS http://127.0.0.1:8080/healthz
```

Slack 연결은 다음 안전 순서로 진행합니다.

1. `integrations/slack/app-manifest.yaml`로 내부 Slack 앱 생성 및 워크스페이스 설치
2. `scripts/install-slack-secret.sh`로 User OAuth Token을 로컬에 저장
3. `doctor` 명령으로 자격증명과 읽기 권한만 검증
4. 최근 24시간, 3개 채널/20개 메시지만 `test` 원문 경로에 수집
5. 결과를 확인한 뒤 2년 과거 백필 및 매일 오전 5시 KST 증분 동기화

실행 명령은 [docs/setup.md](docs/setup.md)의 "Slack 연결"을 참고하세요.
