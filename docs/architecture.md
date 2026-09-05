# Architecture

이 문서는 **지금 구현되어 있는 것**을 기술한다. 계획은 마지막 절에 따로 모았고,
계획과 구현이 어긋난 곳은 그렇다고 적었다.

## 목표

원천 데이터를 먼저 손실 없이 로컬에 보존하고, 결정론적인 코드로 구조화한 뒤,
사용자가 선택한 범위만 LLM에 전달한다. 순서가 요점이다 — 해석은 되돌릴 수 있지만
수집하지 못한 바이트는 되돌릴 수 없다.

```text
Slack · Notion · Google Calendar · GitHub · Slurm
                      │
            read-only collectors  ─────────► archive.py 를 통해서만 기록
                      │
       append-only raw archive (.json.gz + 실행 매니페스트)
                      │
            표준 v1 ledger 변환 (결정론적)
                      │
                 PostgreSQL
                      │
            읽기 전용 로컬 웹 앱 · 백오피스
                      │
               명시적 LLM 전달
```

수집기는 파일을 직접 쓰지 않는다. 전부 `archive.py` 를 지난다. 그래야 매니페스트와
수집 규칙 도장(아래 참조)이 빠짐없이 남고, 나중에 "이 파일이 어떤 규칙으로
잡혔는가"에 답할 수 있다.

## 다섯 원천

| 원천 | 수집기 | 실행 명령 | ledger 변환·적재 |
|---|---|---|---|
| Slack | `slack_collector.py` | `daily-collect`, `collect slack` | 자동 |
| Google Calendar | `calendar_collector.py` | `daily-collect`, `collect google-calendar` | 자동 |
| Notion | `notion_collector.py` | `daily-collect`, `collect notion` | 자동 |
| GitHub | `github_collector.py` | `daily-collect`, `github-collect` | 자동 |
| Slurm | `slurm_collector.py` | `daily-collect`, `slurm-collect` | 자동 |

`daily-collect` 는 다섯 소스를 모두 돌린다(`daily.py:45`). `github-collect` 와
`slurm-collect` 는 KST 날짜 구간을 지정해 손으로 수집할 때 쓰는 명령으로 남아
있고, 그 경로로 수집한 것은 `ledger-live-convert` 와 `ledger-load` 를 사람이 따로
돌려야 한다. 자세한 것은 [daily-collection.md](daily-collection.md).

### 수집 범위 — 실제

**Slack.** 공개 채널, 인증 사용자가 속한 비공개 채널·DM·그룹 DM. 메시지, 스레드,
수정/삭제 상태, 리액션. 첨부파일은 이름·유형·크기·작성자·링크만 남기고 본문은
가져오지 않는다. 멘션은 별도 검색 질의로 인덱싱한다.

**Google Calendar.** 캘린더 목록은 매 실행 전체를 다시 읽고, 이벤트는
`nextSyncToken` 증분으로 읽는다. 이벤트 본문에서 Notion URL 을 뽑아 링크 큐로
넘긴다.

**Notion.** 검색, 페이지·데이터소스 조회, 블록 하위 재귀, 블록별 댓글, 사용자 목록.
보관/휴지통 상태를 보존한다.

**GitHub.** 커밋은 베어 미러에서 `git log` 로 읽고, REST 로는 다섯 종류만 읽는다 —
`pull_request`, `review`, `review_comment`, `issue_comment`, `issue`
(`github_collector.py:69`). 조직 저장소 목록은 전체 페이지네이션한다.
**라벨·마일스톤·릴리스·배포·Actions 실행·권한 변경은 수집 대상이 아니다**
(`github_collector.py:66-68`). 소스 blob 도 아니다.

**Slurm.** 클라우드별(`kakao`, `aws`, `naver`) sacct 전량 덤프를 한 번 받아 KST
날짜로 로컬에서 투영한다. `.batch`/`.extern` 스텝 행은 원본으로 보존되지만 ledger
엔티티로는 변환되지 않는다.

## 원본 보관과 재개

```text
<root>/raw/<source>/<env>/YYYY/MM/DD/<run_id>/NNNNNN-<kind>-<sha256[:12]>.json.gz
<root>/manifests/<source>/<env>/<run_id>.json
<root>/manifests/<source>/<env>/checkpoint.json
<root>/locks/<command>-<env>.lock
```

매니페스트는 한 실행이 무엇을 요청했고 무엇을 받았는지를 전부 적는다 — 요청 창,
체크포인트 입출력, 엔드포인트별 페이지·항목 수, rate limit 횟수, 절단 여부, 건너뛴
것, 오류, 파일별 sha256, 그리고 수집 규칙 도장.

체크포인트는 **완결된 실행 뒤에만** 전진한다. Slack·Notion·GitHub·Slurm 은 절단
여부까지 확인하고 전진한다. Google Calendar 는 dry-run 만 확인하고 절단은 보지
않는다(`calendar_collector.py:254`) — 지금은 `max_calendars` 가 smoke 전용이라
드러나지 않을 뿐, 다른 넷과 다르다.

## 수집 규칙 레지스트리

`collection_rules.py` 는 "이 시점의 수집 규칙"을 버전으로 쌓는 추가 전용 레지스트리다.
각 버전의 내용은 다이제스트로 얼려지고, 모든 매니페스트에 버전·다이제스트·스키마
버전 세 필드가 찍힌다. 수집 동작을 바꾸면 `tests/test_collection_rules.py` 가
빨개지는데, 그것이 **새 버전을 추가하라는 신호이지 기존 버전을 고치라는 신호가
아니다**. [collection-rules.md](collection-rules.md).

## 통합 이벤트 모델

표준 v1 ledger 레코드가 원천별 차이를 흡수한다. `ledger_id` 는
`source|entity_type|tenant|scope|source_entity_id|window_start|content_hash` 의
uuid5 로, 같은 관찰이 두 번 들어와도 한 행이 된다. 적재는 `ledger_id` 기준
멱등이다.

엔티티는 활동(activity)과 차원(dimension)으로 나뉜다. 활동은 열두 종류이지만
타임라인으로 투영되는 것은 넷뿐이다 — `message`, `page`, `comment`, `event`
(`load.py:42`). GitHub 활동과 Slurm `job` 은 적재되지만 타임라인에는 올라가지
않는다. [ledger.md](ledger.md).

## 수집 현황 화면

매니페스트에서 원천×KST 날짜 판정을 유도한다. 파생이고, 읽기 전용이고, 언제든
다시 만들 수 있다. 판정은 아홉 값이고, 완결성·시간 범위·증거 등급을 따로 둔다 —
셋을 하나로 접으면 "왜 이 색인가"에 답할 수 없기 때문이다.
[collection-status.md](collection-status.md).

## 업무 보드

`APP_CONFIG_ROOT/work/items.json` 한 파일이다. 데이터베이스가 아니다. 옆에 잠금
파일과 추가 전용 `history.jsonl` 이 있고, 이력은 **변경보다 먼저** 쓰인다.

쓰는 문은 셋이다.

- **백오피스 웹** — 세션 인증이 있고, 권한 검사가 여기에 있다.
- **`worklog work` CLI** — 같은 파일을 같은 잠금으로 쓴다. **인가가 없다.**
  웹에서 403 인 것이 CLI 에서는 통과한다. 알려진 미해결 구멍이다.
- **아웃박스 큐** — 잠금을 잡을 수 없는 쪽이 JSON 파일을 떨어뜨리면 적용기가
  대신 쓴다. 편집은 `next_action`·`detail` 만, 생성은 요청에 해당하는 필드만
  허용한다. 요청자가 수행자의 보고까지 쓰지 못하게 하려는 제한이다.

[delegated-work.md](delegated-work.md) · [work-board-reference.md](work-board-reference.md).

## 에이전트 조율

`cowork.py` 는 지시 검증기와 활동 표시 읽기를 제공하는 라이브러리다. 문서로 적힌
메일박스 프로토콜 중 상당 부분은 아직 코드가 없다 — 어느 것이 동작이고 어느 것이
명세인지는 [cowork-mailbox.md](cowork-mailbox.md) 가 구분해 둔다.

여기에 **미해결 충돌**이 하나 있다. `cowork.py` 는 `push` 를 자율 금지 행위로
선언하고 "`ready` 상태만으로는 아무 권한도 생기지 않는다"고 규정하는데,
`scripts/wake-local.sh` 는 `ready` 만 보고 세션을 깨우고 그 세션의 프롬프트는
커밋과 푸시를 지시한다. 두 하위 시스템의 규칙이 다르다.

## 개발계 / 운영계

개발은 클라우드 컨테이너에서, 운영은 리눅스 한 대에서 돈다. 클라우드 쪽은 푸시
권한이 없어서, 커밋은 패치 파일로 건너가 타이머가 적용하고 테스트가 초록일 때만
푸시한다. [dev-prod-split.md](dev-prod-split.md).

## 배포 경계

- Docker Compose 로 앱·PostgreSQL·OpenSearch 관리
- 앱은 `127.0.0.1` 과 지정한 tailnet/LAN 주소에 바인딩
- Tailscale Serve 가 tailnet 내부 HTTPS 제공
- Google OAuth 로그인 + 단일 계정 allowlist (구현되어 있다 — `admin_web.py:313`)
- UI 와 수집 권한은 읽기 전용
- 재부팅 후 서비스와 스케줄 자동 시작

`OPENSEARCH_URL` 은 compose 가 세 서비스에 넘기지만 **읽는 코드가 없다**. 검색
색인은 아직 없다.

## 비밀정보

```text
secrets/                       # 저장소 밖. .gitignore 와 pre-commit 훅이 막는다
  slack.env
  google-client.json
  google-token.json
$APP_CONFIG_ROOT/credentials/  # 앱이 실제로 읽는 곳
```

비밀정보 값은 로그, fixture, Git, 테스트 실패 메시지에 포함하지 않는다.
변수별 출처는 [environment.md](environment.md).

## 백오피스 메뉴와 스케줄

메뉴는 **업무**(업무 현황 · 로드맵), **운영**(수집 현황 · 서버 상태 · 스케줄),
**설정**(연결 · 저장소·백업 · 로컬 모델 · 보안), 그리고 감사 기록으로 묶인다.
업무 현황이 로그인 후 첫 화면이다. 로드맵은 의도적으로 비활성 자리표시자다 —
어떤 시스템의 무슨 필드를 쓸지 정하지 않았으므로 뒤에 아무 동작도 만들지 않았다.

`schedules.py` 는 두 종류의 사실을 섞지 않는다. **목록**은 이 저장소가 정의하는
배치를 기술하고(이름·목적·실행기·주기·범위·중복 방지·로그·실패 확인 방법), 이는
코드의 성질이다. **상태**(설치됨·활성·마지막 실행·다음 실행)는 단언하지 않는다.
운영자 설정과 `systemctl show` 에서 읽고, 읽을 수 없으면 이유와 함께 `불명`이다.
설정에서 계산한 다음 실행 시각과 systemd 의 것을 나란히 보여주되 권위는 systemd
쪽에만 준다.

`GET /api/v1/admin/schedules` 는 최고 관리자 전용, 읽기 전용, 매개변수 없음이다 —
유닛 이름은 요청이 아니라 목록에서 온다. `systemctl show` 는 셸 없이 고정 인자와
타임아웃으로 실행되며, 여기서 유닛을 설치·활성화·변경하는 일은 없다.

목록이 이름 붙인 `hkwa-collect.timer` / `hkwa-collect.service` 는
`deploy/systemd/` 에 있고 `install-incoming-timer.sh` 가 설치한다. 앞선 판
(`worklog-daily.*`)은 시스템 유닛 디렉터리에 있어 개발계에서 읽을 수도 고칠 수도
없었고, 그래서 다섯 중 두 소스만 돌고 있다는 사실이 코드보다 오래 살아남았다.
2026-09-05 에 껐다. GitHub·Slurm 은 이제 일일 수집 안에서 돌므로 별도 목록 항목이
필요 없다.

## 아직 없는 것

- 검색 색인 (OpenSearch 는 선언만 되어 있다)
- 응답 초안 작성
- GitHub 의 라벨·마일스톤·릴리스·배포·Actions·권한 변경 수집
- `ledger-verify` 의 GitHub·Slurm 지원
