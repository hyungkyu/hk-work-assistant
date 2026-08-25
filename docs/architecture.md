# Architecture

## 목표

원천 데이터를 먼저 손실 없이 로컬에 보존하고, 결정론적인 코드로 구조화한 뒤,
검색 결과 중 사용자가 선택한 범위만 LLM에 전달한다.

```text
Slack / Google Calendar / GitHub
              |
        read-only collectors
              |
      append-only raw archive (HDD)
              |
       deterministic normalizers
              |
   PostgreSQL + OpenSearch (NVMe)
              |
       read-only local web app
              |
       explicit LLM hand-off
```

## 수집 범위

### Slack

- 모든 공개 채널
- 인증 사용자가 참여한 비공개 채널
- 인증 사용자가 포함된 DM과 그룹 DM
- 메시지, 스레드, 수정/삭제 상태, 리액션, 핀, 별표
- 첨부파일은 이름, 유형, 크기, 작성자, 링크만 저장
- 직접 멘션, 사용자 그룹 멘션, `@channel`, `@here`, `@everyone` 별도 인덱싱

### Google Calendar

- 전 임직원의 공개 캘린더
- 팀/프로젝트/회의실/리소스 캘린더
- 공개 이벤트의 제목, 설명, 참석자, 회의 링크
- 비공개 이벤트는 시간 범위와 `Busy` 상태만 보존
- 변경과 취소 이력 보존

### GitHub

- 저장소와 조직 구성원 목록
- 커밋, PR, 리뷰, 리뷰 요청, 코드 댓글, 일반 댓글
- 이슈, 담당자, 라벨, 마일스톤, 릴리스, 배포, Actions 실행
- 조직/팀/저장소 권한 변경(플랜과 권한이 허용하는 범위)
- 사람과 봇/자동화 활동을 분리
- 소스 blob은 기본 수집 대상이 아님

## 통합 이벤트 모델

각 이벤트는 다음 공통 필드를 갖는다.

- `event_id`: 원천 ID와 버전에서 만든 안정적인 ID
- `source`: slack, google_calendar, github
- `event_type`: message, calendar_event, pull_request 등
- `actor_id`: 행위자 ID
- `occurred_at`: 원천에서 실제 발생한 시각
- `updated_at`: 원천 객체가 마지막으로 변경된 시각
- `ingested_at`: 로컬에서 확인한 시각
- `container_id`: 채널, 캘린더 또는 저장소
- `thread_id`: 대화/PR/이슈 묶음
- `permalink`: 원천으로 돌아가는 링크
- `payload`: 정규화 과정에서 보존할 구조화 속성
- `classification`: 규칙 기반 업무 후보

업무 후보는 `request`, `promise`, `decision`, `question`, `response`,
`schedule_change`, `unclassified` 중 하나 이상이다. 확신할 수 없으면
`unclassified`를 유지하며 LLM이 원문 없이 사실을 발명하지 못하게 한다.

## 동기화

- 최초 백필: 전체 과거 기록을 페이지 단위로 재개 가능하게 수집
- 증분 동기화: 매일 오전 5시 KST
- 최근 구간 중첩 조회로 수정/삭제/취소 탐지
- 원문 파일의 영속화와 체크섬 기록 후에만 체크포인트 갱신
- API 오류와 rate limit은 재시도 가능 상태로 기록
- 하루 사이 생성 후 삭제된 Slack 메시지는 배치 방식으로 복원할 수 없음을 명시

## 배포 경계

- Docker Compose로 앱, PostgreSQL, OpenSearch를 관리
- 앱은 `127.0.0.1`에만 바인딩
- Tailscale Serve가 tailnet 내부 HTTPS를 제공
- Google OAuth 로그인을 사용하며 초기에는 단일 계정 allowlist
- UI와 수집 권한은 읽기 전용
- 재부팅 후 서비스와 스케줄 자동 시작

## 비밀정보

예정 경로:

```text
/etc/rlwrld-worklog/secrets/
  slack.env
  google-service-account.json
  google-web-oauth.json
  github-app.pem
  github.env
```

비밀정보 값은 로그, fixture, Git, 테스트 실패 메시지에 포함하지 않는다.

