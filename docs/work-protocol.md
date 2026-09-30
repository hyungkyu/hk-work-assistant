# 업무 프로토콜

이 저장소에서 일하는 모든 세션이 따르는 하나의 경로.
**작업을 시작하기 전에 읽는다.**

---

## 체인

```
업무목록 ──▶ 내 브랜치 ──▶ GitHub main ──▶ 통합데브 ──▶ 프로덕션
 (보드)      (작업)         (수렴점)      (검증)      (서비스)
```

각 칸에는 **그 칸의 상태를 말해주는 파일 하나**가 있다. 추측하지 말고 읽는다.

| 칸 | 무엇 | 어디를 읽나 |
| --- | --- | --- |
| 업무목록 | 누가 무엇을 하고 있나 | 보드 (`incoming/last-board.json`) |
| 내 브랜치 | 내가 지금 뭘 물고 있나 | `git branch -v`, `git status --short` |
| GitHub main | 합쳐진 코드 | `git fetch origin && git log --oneline -5 origin/main` |
| 통합데브 | **그게 실제로 도는가** | `incoming/last-integration.json` |
| 프로덕션 | 지금 돌고 있는 것 | `incoming/last-deploy.json` |

패치 캐리어를 쓰는 경우 하나가 더 있다: `incoming/last-run.json`.

---

## 다섯 단계

### 1. 보드에 올린다 — 끝나고가 아니라 시작할 때

항목을 만들거나, 있는 항목을 `in_progress` 로 옮긴다. 커밋 메시지에 번호를
적는다:

```
Refs wi_1bda10e6938ed059
```

캐리어가 이것을 읽어 `last-run.json` 의 `items` 에 남긴다. 비어 있으면 보드에
없는 작업이 들어왔다는 뜻이고, 그건 질문거리가 된다. 막지는 않는다.

**끝나고 올린 일은 아무도 방향을 바꿔줄 수 없었던 일이다.**

### 2. `git fetch` 하고, 자기 브랜치에서 시작한다

```bash
git fetch origin
git log --oneline -5 origin/main     # 내가 뭘 놓쳤나
git branch -v                        # 다른 세션이 뭘 물고 있나
git switch -c <세션이름>/<작업> origin/main
```

**마지막 줄의 `origin/main` 이 중요하다.** 로컬 `main` 에서 갈라지면 안 된다.

패치 캐리어는 `git am` 으로 패치를 적용하므로 같은 내용이라도 **새 sha 를
만든다.** 그래서 캐리어를 쓰는 세션의 로컬 `main` 은 내용이 같아도
`origin/main` 과 영구히 갈라져 있고, `git merge --ff-only origin/main` 은
조용히 실패한다. 그 상태에서 만든 패치는 맞지 않는 기준 위에 올라가 튕긴다.

2026-09-30 에 정확히 이렇게 패치 하나가 `incoming/failed/` 로 갔다. 원인은
`2>/dev/null` 이 그 실패를 가린 것이었다 — **git 명령의 에러를 버리지 않는다.**

**main 을 체크아웃하고 작업하지 않는다.**

트리가 더러운데 내 작업이 아니면 **건드리지 않는다.** `git stash`,
`git checkout --`, `git reset` 전부 남의 시간을 지운다. 사람에게 말하고 멈춘다.

### 3. 테스트가 초록일 때만 올린다

```bash
.venv/bin/python -m pytest -q
```

데이터베이스가 붙은 테스트까지 돌리려면:

```bash
set -a; . "$HOME/.config/hk-work-assistant/collect.env"; set +a
WORKLOG_TEST_DATABASE_URL="$WORKLOG_INTEGRATION_DATABASE_URL" \
  .venv/bin/python -m pytest -q
```

**이 두 줄의 차이가 70개다.** 없이 돌리면 원장·투영·블록·페어링 테스트가
조용히 건너뛰어지고, 숫자는 여전히 초록으로 보인다.

올리는 길은 둘 중 하나:

* **패치 캐리어** — `git format-patch -1 -o incoming/` 후 손을 뗀다. 3분 안에
  캐리어가 적용하고, 테스트를 돌리고, 초록일 때만 푸시한다. 결과는
  `incoming/last-run.json`.
* **직접 푸시** — 자기 브랜치를 GitHub 에 푸시한다. 이 경우 위 명령을
  **직접 돌린 뒤에** 푸시한다.

### 4. 통합데브의 판정을 확인한다

```bash
cat incoming/last-integration.json
```

10분마다 `origin/main` 을 별도 클론으로 풀 받아, 별도 DB(`worklog_dev`)
상대로 전체 테스트를 돌린다.

| `outcome` | 뜻 |
| --- | --- |
| `green` | 이 커밋은 이 기계에서 실제로 돈다 |
| `red` | 어느 테스트가 왜 깨졌는지 `detail` 에 있다 |
| `untested` | 돌리지 못했다 — 이유가 적혀 있다. **초록이 아니다** |
| `diverged` | 통합 클론에 누가 커밋을 남겼다 |

**고치지도, 되돌리지도 않는다.** 사실만 남긴다.

### 5. 프로덕션은 초록만 받는다

```bash
cat incoming/last-deploy.json
```

`green` 판정이 난 커밋만 배포된다. 판정이 아직 없으면
`awaiting-verification` 으로 기다리고(최대 10분), `red` 면 `blocked` 이다.

**배포가 안 된다고 손으로 밀지 않는다.** 그 문구가 이유를 말하고 있다.

---

## 막혔을 때

**"고쳤는데 그대로"** 면 코드를 파기 전에 순서대로 읽는다:

```bash
cat incoming/last-run.json          # 패치가 적용은 됐나
cat incoming/last-integration.json  # main 이 초록인가
cat incoming/last-deploy.json       # 도는 게 그 커밋인가
```

실제로 여러 번 답이 여기 있었다:

* `refused` — 트리가 더러워 캐리어가 멈춰 있다. 어느 파일 때문인지, 몇 분째
  인지 적혀 있다. (2026-09-16: 4시간, 2026-09-30: 26분)
* `apply-failed` — 패치가 `incoming/failed/` 에 있다.
* `tests-failed` / `red` — 올라가지 않았다.
* `awaiting-verification` — 아직 검증 전이다. 곧 된다.

**커밋된 코드와 지금 돌고 있는 코드는 다를 수 있다.** 세 파일이 그 차이를
말해준다.

---

## 하지 않는 것

* **main 에서 작업하지 않는다.**
* **테스트 없이 푸시하지 않는다.**
* **내가 하지 않은 작업을 덮지 않는다.** 남의 커밋을 리베이스하거나, 남의
  브랜치를 고치거나, 남의 미완성 작업을 정리하지 않는다. 남의 변경이 내 것을
  되돌렸을 때도 마찬가지 — 그들의 커밋을 고쳐 쓰는 대신 **현재 파일 위에 내
  변경만 다시 올린다.** (2026-09-30: 리베이스했으면 923줄이 날아갔다.)
* **수집한 데이터를 저장소에 넣지 않는다.** 예외 없는 유일한 규칙.
* **비밀값을 저장소에 넣지 않는다.** `~/.config/hk-work-assistant/collect.env`.

---

## 왜 이렇게까지 하나

2026-09-30 에 두 세션이 같은 날 `cli.py` 를 서로 모르고 고쳤다. 한쪽이 낡은
기준으로 고쳐 main 에 직접 커밋했고, 그것이 다른 쪽의 변경을 되돌렸다. 테스트
두 개가 빨개져 패치 큐가 멈췄고 배포도 함께 섰다. **26분 동안 아무도 몰랐다.**

두 세션 다 잘못한 것이 없다. 서로가 무엇을 만지는지 볼 방법이 없었을 뿐이다.

그날 실제로 세어 보니, 캐리어를 통과한 패치 26건은 충돌 0건이었고 사고는
캐리어를 거치지 않은 한 건에서 났다. 그리고 그 김에 드러난 것이 하나 더
있다 — **데이터베이스가 붙은 테스트 70개가 어디서도 돌고 있지 않았다.**

규칙은 전부 이미 일어난 일에서 나왔다.

---

## 더 읽을 것

| 필요한 것 | 문서 |
| --- | --- |
| 역할과 보드의 규칙 | `docs/agent-onboarding.md` |
| 보드의 필드와 경로 | `docs/work-board-reference.md` |
| 테스트와 커밋 | `CONTRIBUTING.md` |
| 무엇을 수집하고 무엇을 저장하지 않는가 | `docs/collection-rules.md`, `docs/data-policy.md` |
