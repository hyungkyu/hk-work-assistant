# 개발계 / 운영계 분리

## 왜 나눴나

한 대에서 개발하고 운영하면, 고치는 중인 코드가 곧 도는 코드다.
2026-09-04 의 배포는 커밋되지 않은 워크트리에서 나온 이미지였고,
"도는 것 = 커밋된 것"이 그날까지 한 번도 증명된 적이 없었다.

| | 개발계 | 운영계 |
|---|---|---|
| 어디 | 모리의 클라우드 컨테이너 | hk-linux (`/home/hk/Documents/ChatGPT/RLWRLD workspace`) |
| 데이터 | 최소 테스트 데이터 | 원본 전량 (테라급) |
| 하는 일 | 구현, 단위 테스트 | 운영 데이터로의 검증, 배포 |
| 깃 | 읽기만 (풀) | 읽기·쓰기 (풀·푸시) |

## 왜 클라우드가 푸시를 못 하나

컨테이너의 깃 트래픽은 전부 샌드박스 프록시를 지난다.
그 프록시는 **세션 생성 시 붙인 저장소**에만 자격 증명을 주입한다.
세션 도중에는 바꿀 수 없다. GitHub 커넥터가 연결돼 있어도 이 층과는 무관하다.

읽기는 열려 있다. 막힌 것은 쓰기 한 방향뿐이다.

## 전달 경로

```
클라우드                                    hk-linux
--------                                    --------
구현 · 테스트 · 커밋
git format-patch
   │
   └── incoming/NNNN-*.patch ──────────────▶ scripts/apply-incoming.sh   (사람)
       (device_commit_files 로 직접 씀)      scripts/incoming-tick.sh    (타이머)
                                                git am --3way
                                                pytest
                                                git push origin main
   ◀────────────────────────────────────────────┘
   git pull  (읽기는 항상 열려 있다)
```

`incoming/` 은 `.gitignore` 에 있다. 전달 수단이지 저장소의 내용이 아니다.
적용된 패치는 `incoming/applied/`, 실패한 것은 `incoming/failed/` 로 간다.

## 규칙

- **패치는 항상 `main` 위에서 적용한다.** 스크립트가 브랜치를 확인하고 거부한다.
- **더러운 워크트리에서는 적용하지 않는다.** 미커밋 수정과 들어온 패치를
  섞어서 푸는 순간 무엇이 도는지 다시 알 수 없어진다.
- **푸시 전에 운영계에서 테스트를 돌린다.** 개발계 통과는 개발계 통과일 뿐이다.
- **한 패치가 실패하면 거기서 멈춘다.** 뒤 패치는 시도하지 않는다.

## 수동 적용

```bash
cd "/home/hk/Documents/ChatGPT/RLWRLD workspace"
bash scripts/apply-incoming.sh
.venv/bin/python -m pytest -q
git push origin main
```

`scripts/apply-incoming.sh` 는 `git am` 까지만 한다. 테스트도 푸시도 하지 않고
저 두 줄을 출력하고 끝낸다 (`scripts/apply-incoming.sh:56-60`).
`set -euo pipefail` 이라 거부할 때는 0 이 아닌 코드로 죽는다 — 사람이 보라고
만든 경로다.

---

# 자동 경로: 타이머 두 개

수동 경로 말고, hk-linux 의 사용자 systemd 에 타이머 두 개가 돈다.
`scripts/install-incoming-timer.sh` 가 `deploy/systemd/` 의 유닛 네 개를
`$HOME/.config/systemd/user` 로 깔고 켠다.

| 유닛 | 언제 | 무엇을 | 제한 |
|---|---|---|---|
| `hkwa-incoming.timer` | 부팅 후 2분, 이후 비활성 3분마다 | `hkwa-incoming.service` | `Persistent=true`, `AccuracySec=30s` |
| `hkwa-incoming.service` | (타이머) | `scripts/incoming-tick.sh` | `Type=oneshot`, `TimeoutStartSec=900` |
| `hkwa-wake.timer` | 부팅 후 4분, 이후 비활성 3분마다 | `hkwa-wake.service` | `Persistent=true`, `AccuracySec=30s` |
| `hkwa-wake.service` | (타이머) | `scripts/wake-local.sh` | `Type=oneshot`, `TimeoutStartSec=4200` |

두 서비스 다 `StartLimitIntervalSec=0` 이다
(`deploy/systemd/hkwa-incoming.service:7`, `deploy/systemd/hkwa-wake.service:3`).
이유가 유닛에 적혀 있다: "A watcher that can be permanently disabled by its own
failures is not a channel. The outbox path unit taught us this on 2026-09-04: six
failures in ten seconds hit the default start rate limit and killed both the
service and the unit watching for it, silently."

타임아웃 값은 각각 스크립트가 실제로 하는 일 위로 잡혀 있다. 캐리어의 900초는
전체 테스트 한 판보다 넉넉히 위 (`hkwa-incoming.service:13-15`), 깨우기의
4200초는 스크립트가 `timeout(1)` 으로 스스로 거는 3600초 위다 — "so systemd
never kills it between the session ending and the state being recorded"
(`hkwa-wake.service:9-11`).

`ExecStart` 의 경로는 따옴표로 감쌌다
(`hkwa-incoming.service:12`, `hkwa-wake.service:8`). 첫 판은 공백이 든 경로를
따옴표 없이 넣어 systemd 가 인자를 쪼갰고, 서비스는 상태 파일을 쓰기도 전에
죽었으며, 증상은 "파일이 안 생긴다" 하나뿐이었다
(`scripts/install-incoming-timer.sh:54-57`).

## 상태는 종료 코드가 아니라 파일에 있다

**두 스크립트 다 모든 경로에서 `exit 0` 이다**
(`incoming-tick.sh:34`, `wake-local.sh:66`). `systemctl --user status` 가
초록인 것은 아무 의미가 없다. 봐야 할 것은 두 파일이다:

```bash
cat incoming/last-run.json    # 캐리어
cat incoming/last-wake.json   # 깨우기
```

둘 다 **아무것도 하지 않은 경로를 포함해 매 틱마다** 쓰인다
(`incoming-tick.sh:9-11`, `wake-local.sh:21-22`). 파일이 아예 없다면 그것은
"할 일이 없었다"가 아니라 "서비스가 자기 상태 기록에 도달하지 못했다"이고,
설치 스크립트가 마지막에 확인하는 것도 정확히 그것이다
(`install-incoming-timer.sh:59-66`).

## `incoming-tick.sh` 의 outcome 전부

`incoming/last-run.json` 의 모양 (`incoming-tick.sh:29-33`):

```json
{"started_at":"…","finished_at":"…","outcome":"…","applied":0,"head":"abc1234","detail":"…"}
```

`head` 는 `git rev-parse --short HEAD`, 실패하면 문자열 `unknown`.
`detail` 은 `python3` 로 JSON 인코딩하고, 그마저 실패하면 `""`.

| outcome | 줄 | 뜻 | 트리에 남는 상태 |
|---|---|---|---|
| `idle` | `:25` 기본값, `:48-50` 도달 | 락은 잡았고 `incoming/*.patch` 가 없다 | 아무것도 안 함. HEAD 그대로 |
| `busy` | `:41-43` | `flock -n` 실패 — 앞 틱이 아직 `.tick.lock` 을 쥐고 있다 | 아무것도 안 함 |
| `refused` | `:53`, `:60`, `:66` | 원인 셋, `detail` 로만 구분된다: (a) 워크트리가 더럽다, (b) `HEAD` 가 `main` 이 아니다, (c) `git fetch origin main` 또는 `git merge --ff-only origin/main` 실패 | 패치는 `incoming/` 에 그대로. 아무것도 적용 안 됨 |
| `apply-failed` | `:78` | 어느 패치의 `git am --3way` 가 실패 | `git am --abort` 후 그 패치는 `incoming/failed/` 로. **앞서 이미 적용된 패치들은 로컬 커밋으로 남고 푸시되지 않는다.** 뒤 패치는 `incoming/` 에 그대로 |
| `untested` | `:91` | `.venv/bin/python` 도 `python3` 도 없다 | 적용된 패치는 로컬 커밋, 푸시 안 됨. 주석 그대로: "A missing interpreter is a reason not to push, not a reason to push untested" (`:84-85`) |
| `tests-failed` | `:98` | `pytest -q` 가 0 이 아니다. `detail` 은 출력 마지막 20줄 | 적용된 패치는 로컬 커밋, 푸시 안 됨 |
| `pushed` | `:104` | 테스트 통과, `git push -q origin main` 성공. `detail` 은 pytest 마지막 줄 | 커밋이 `origin/main` 에 올라감 |
| `push-failed` | `:107` | 테스트는 통과했는데 푸시가 거부됨 (자격 증명, non-fast-forward, 네트워크) | 적용됐고 초록인 커밋이 로컬에만 남는다 |

`GH_TOKEN` 은 설치 시점에 사용자 매니저로 넘어간다. 없으면 설치 스크립트가
"Watch for outcome=push-failed" 라고 미리 말한다
(`install-incoming-timer.sh:29-35`).

## 캐리어의 빈틈 둘

둘 다 실제 동작이다. 고쳐진 것이 아니라 여기 적혀 있을 뿐이다.

**1. `pushed` 는 "방금 적용한 커밋이 올라갔다"는 뜻이 아니다.**

`incoming-tick.sh:103` 은 `git push -q origin main` 이다. 패치에서 나온 커밋만
고르는 것이 아니라 `main` 위에 있는 것을 전부 민다. 앞선 틱이 남긴 커밋, 사람이
손으로 만든 커밋, 무엇이든 같이 간다. `outcome=pushed` 와 `applied=2` 를 같이
읽어도 "그 2개가 올라갔다"까지고, "그 2개만 올라갔다"는 말할 수 없다.

**2. 푸시하지 못한 커밋을 다음 틱이 재시도하지 않는다.**

`apply-failed`, `untested`, `tests-failed`, `push-failed` 넷 다 커밋은 로컬에
남기고 푸시는 하지 않은 채 끝난다. 그런데 패치 파일은 이미
`incoming/applied/` 또는 `incoming/failed/` 로 옮겨진 뒤다 (`:73`, `:77`).
그래서 3분 뒤 다음 틱은 `incoming/*.patch` 글롭이 빈 것을 보고
`:48-50` 에서 `outcome=idle` 로 끝난다.

즉 **푸시되지 않은 커밋을 안은 채 상태 파일은 `idle` 이라고 말한다.** 새 패치가
들어와 푸시 경로에 다시 진입할 때까지 그 커밋들은 그대로 앉아 있다.
`last-run.json` 이 `idle` 이라고 해서 `main` 이 `origin/main` 과 같다는 뜻이
아니다. 확인은 따로 해야 한다:

```bash
git rev-list --count origin/main..main   # 0 이 아니면 로컬에만 있는 커밋이 있다
```

---

# `wake-local.sh`: 운영계에서만 되는 일을 세션에 시킨다

클라우드는 개발하고 테스트할 수 있지만 운영 데이터, 도는 컨테이너, 이 기계의
systemd 유닛에는 손댈 수 없다. 그런 항목은 보드에 로컬 실행자 앞으로 앉아 있고,
원래는 사람이 프롬프트를 붙여넣을 때까지 기다린다. 이 스크립트가 그 붙여넣기다
(`scripts/wake-local.sh:5-8`).

- 읽는 것: 보드 `$APP_CONFIG_ROOT/work/items.json` (`:30`), 그리고 자기
  냉각 대장 `incoming/wake-skips.json` (`:35`)
- 고르는 것: `status == "ready"` 이고 `assigned_to` 가 `$WAKE_EXECUTOR` (기본
  `local`, `:38`) 이며 아카이브되지 않았고 **냉각 중이 아닌** 항목 중
  `created_at` 이 가장 오래된 하나 (`:129-209`)
- 하는 것: `timeout "$wake_timeout" claude -p "$prompt" --permission-mode
  acceptEdits --allowedTools "$allowed"` (`:232-235`)
- **세션이 끝난 뒤 보드를 다시 읽는 것**: 그 항목의 `status` 와 `revision` 을
  세션 전 값과 대조한다 (`:241-323`)
- 남기는 것: 전사 `$APP_CONFIG_ROOT/cowork/logs/wake-<UTC>-<item_id>.log`
  (`:230`) 와 `incoming/last-wake.json`

보드에는 **쓰지 않는다.** 항목을 잡는 것도 막혔다고 표시하는 것도 실행자인
세션의 일이다. 런처가 실행자를 대신해 보고서를 쓰기 시작하면 아웃박스 규칙이
막으려는 바로 그 혼동이 된다. 이 스크립트가 하는 것은 읽고, 재고, 자기 파일에
적는 것뿐이다.

## 왜 `woke` 가 다시 정의됐나 — 2026-09-06

`wake-local.sh` 는 설치된 뒤 3분마다 꼬박꼬박 돌았고, `incoming/last-wake.json`
은 매번 `outcome: woke` 였고, **한 일은 아무것도 없었다.** 마지막 전사가 그
이유를 적어 두었다:

> `worklog work show` 가 거부됐다 (`This command requires approval`) — 앞선
> 300 틱에 걸쳐 기록된 것과 같은 게이트다. 2단계 규칙대로 항목을 작업하지
> 않았다. `items.json` 을 직접 읽었다: 보드 리비전 671, 그런데 이 항목은
> 여전히 `status=ready`, 항목 리비전 3 … 주변은 움직이는데 이 항목만 얼어 있다.

결함 둘이 겹쳐 있었다.

1. **허용목록이 존재하지 않는 명령을 적고 있었다.** `Bash(work:*)` 였는데
   프롬프트는 `worklog work …` 를 시킨다. 두 문자열은 서로 다른 파일에 있고
   사람이 맞춰 두는 것 말고는 맞는지 확인할 방법이 없었다. 지금은
   `$root/.venv/bin` 을 `PATH` 앞에 붙이고 (`:40-51`) 허용목록은 `Bash(worklog:*)`
   와 `Bash(python:*)` 를 적으며, 프롬프트가 인쇄하는 모든 명령이 허용목록에
   있는지를 `tests/test_wake_local.py` 가 확인한다. 절대경로를 프롬프트에
   심는 방법은 택하지 않았다 — 운영계의 저장소 경로에는 공백이 있고, 따옴표 친
   경로는 허용목록 접두사 매칭이 살아남지 못한다.
2. **잡히지 않는 항목 하나가 큐 전체를 굶겼다.** 고르는 것은 가장 오래된
   `ready` 항목이었고, 세션이 영영 잡지 못하므로 다음 틱이 같은 항목을 다시
   골랐다. 더 새로운 `local` 일감은 그 뒤에서 손도 닿지 않은 채 앉아 있었다.

`woke` 는 이제 "CLI 가 0 으로 끝났다"가 아니라 **"항목이 움직였다"** 는 뜻이다.

## `wake-local.sh` 의 outcome 전부

`incoming/last-wake.json` 의 모양 (`wake-local.sh:61-67`):

```json
{"started_at":"…","finished_at":"…","outcome":"…","item_id":"…",
 "skipped":[{"item_id":"…","attempts":2,"until":"…","reason":"…"}],"detail":"…"}
```

`skipped` 는 **이 틱이 손대지 않기로 한 항목들**이다. 냉각 중이라 건너뛴 것과,
방금 시도했는데 움직이지 않아 새로 냉각에 들어간 것이 함께 들어간다. 빈 배열이
정상이다.

| outcome | 줄 | 뜻 | 남는 상태 |
|---|---|---|---|
| `idle` | `:56` 기본값, `:211-218` 도달 | 나머지는 정상인데 `$executor` 앞으로 온 뽑을 수 있는 `ready` 항목이 없다 | 세션 안 뜸. `item_id` 는 빈 문자열. `skipped` 가 비어 있지 않다면 큐가 빈 것이 아니라 **전부 냉각 중**이고 `detail` 이 그렇게 말한다 |
| `busy` | `:72-75` | `flock -n` 실패 — 앞 틱이 깨운 세션이 아직 돌고 있다 | 세션 안 뜸. 한 워크트리를 두 세션이 다투는 것을 막는 장치 (`:69-70`) |
| `no-claude` | `:78-81` | 사용자 매니저의 `PATH` 에 `claude` 가 없다 | 세션 안 뜸. 설치 스크립트가 설치 시점에 같은 것을 경고한다 (`install-incoming-timer.sh:37-42`) |
| `no-worklog` | `:87-90` | `PATH` 에도 `$root/.venv/bin` 에도 `worklog` 가 없다 | 세션 안 뜸. 보드에 아무것도 못 쓰는 세션은 틱만 태운다 |
| `no-board` | `:93-96` | `$APP_CONFIG_ROOT/work/items.json` 이 없다 | 세션 안 뜸 |
| `no-prompt` | `:223-225` | `scripts/local-work-prompt.md` 가 없거나 못 읽는다 | 세션 안 뜸. 검사가 `-z` 라서 `python3` 실패도 여기로 온다 |
| `timeout` | `:331-333` | `timeout(1)` 이 124 반환 — 세션이 `$WAKE_TIMEOUT` (기본 3600초, `:104`) 를 넘겼다 | **세션이 도중에 잘렸다.** 워크트리는 그 세션이 남긴 중간 상태 그대로. 항목이 안 움직였으면 냉각도 걸린다 |
| `session-failed` | `:334-336` | `claude` 가 그 밖의 사유로 0 이 아닌 코드 반환 | 마찬가지. `detail` 에 종료 코드와 전사 마지막 5줄. 안 움직였으면 냉각 |
| `woke` | `:337-339` | `claude` 가 0 으로 끝났고 **그 항목의 `status`/`revision` 이 세션 전과 다르다** — 보드에서 사라진 것(아카이브·삭제)도 움직인 것으로 센다 | 항목이 실제로 움직였다. 냉각 대장에서 빠진다. 무엇이 됐는지는 여전히 보드에만 있다 |
| `unclaimed` | `:340-345` | `claude` 는 0 으로 끝났는데 항목이 **그대로다**. 또는 세션 뒤 보드를 다시 읽을 수 없어 움직였다고 말할 수 없다 | 세션은 돌았고 보드는 그대로. 앞의 경우는 냉각이 걸리고, 뒤의 경우는 아무것도 알아낸 것이 없으므로 걸리지 않는다 |

`idle` 에는 하나가 더 섞여 있다. 보드를 고르는 인라인 파이썬은 어떤 예외에서든
출력 없이 0 으로 끝난다 (`:134-137`). 그래서 **깨졌거나 반쯤 쓰인 `items.json` 은
빈 큐와 구별되지 않는다.** 일부러 그렇게 뒀다: "A partially written board is not
an error worth reporting: the next tick is three minutes away" (`:127-128`).
대신 `outcome=idle` 이 길게 이어질 때 큐가 빈 것인지 보드가 깨진 것인지는 이
파일이 알려주지 못한다.

## 냉각: 움직이지 않은 항목은 잠시 뒤로 간다

항목이 움직이지 않은 채 세션이 끝나면 그 항목은 `$WAKE_SKIP_HOURS` (기본 6시간,
`:107`) 동안 뽑히지 않는다. 대장은 `incoming/wake-skips.json` 이고 항목마다
`attempts` · `first_at` · `last_at` · `until` · `reason` 을 담는다.

**횟수 상한이 아니라 기간인 이유.** 항목이 안 움직이는 두 가지 방식이 서로 반대
되는 것을 원한다. 영구적인 원인(세션이 실행할 수 없는 도구, 따를 수 없는 지시)은
3분마다 틱을 태우는 것을 멈춰야 하고, 일시적인 원인(중간에 죽은 세션)은 저절로
돌아와야 한다 — 이 기계에는 카운터를 지워 줄 사람이 앉아 있지 않다. 기간은 둘 다
한다. 영구적으로 막힌 항목이 하루에 480번이 아니라 4번의 깨우기를 쓴다.

**조용히 사라지지는 않는다.** 세 군데에 남는다.

1. `incoming/last-wake.json` 의 `skipped` — 어느 항목을 왜, 몇 번째로,
   언제까지 건너뛰는지.
2. `incoming/wake-skips.json` — 대장 자체. 항목이 보드를 떠나면 그 줄도 지워진다.
3. **보드**. 냉각된 항목은 `ready` 인 채로 아무도 손대지 않은 채 남는다.
   그것이 정확히 `worklog work audit` 의 `ready_untouched` 가 하루 뒤에 보고하는
   것이다 (`docs/board-audit.md`). 감사가 이미 보고 있는 것을 스크립트가 보드에
   다시 적을 이유가 없다.

## 도구 허용목록은 저장소 밖에서 넓혀진다

`:103` 에서 허용목록을, `:104` 에서 타임아웃을, `:107` 에서 냉각 시간을 정하고,
**그다음** `:109` 에서 설정 파일을 읽는다:

```bash
allowed='Read,Glob,Grep,Edit,Write,Bash(git:*),…,Bash(worklog:*),…'
wake_timeout="${WAKE_TIMEOUT:-3600}"
skip_hours="${WAKE_SKIP_HOURS:-6}"
[ -f "$config_root/wake-local.env" ] && . "$config_root/wake-local.env"
```

순서가 그러므로 `$APP_CONFIG_ROOT/wake-local.env` 는 세 값을 **덮어쓴다**.
그 파일은 이 저장소에 없고, `~/.config` 아래 있으며, 리뷰도 diff 도 거치지
않는다. 거기 한 줄이면 `allowed` 가 `Bash(sudo:*)` 를 포함하도록 넓어진다.
스크립트 본문만 읽어서는 그 기계의 세션이 무엇을 할 수 있는지 알 수 없다.
실제로 무엇이 실렸는지는 파일을 직접 봐야 한다:

```bash
cat ~/.config/hk-work-assistant/wake-local.env   # 없으면 :103 값 그대로다
```

의도된 확장 지점이다 — `:99-102` 주석: "Override in $config_root/wake-local.env
if a task genuinely needs more". 여기 적는 이유는 그 범위가 스크립트가 아니라 그
기계에 있다는 사실 자체다.

**이 파일이 `allowed` 를 좁히면 프롬프트와의 합의도 같이 깨진다.** 테스트는
저장소 안의 기본값을 검사하지 그 기계에 실린 값을 검사하지 못한다. 넓히는 데
쓰고 좁히는 데 쓰지 마라.

## 이중 기동을 막는 것은 스크립트가 아니라 프롬프트다

`flock` (`:71`) 은 **세션이 도는 동안만** 두 번째 기동을 막는다. 세션이 끝나면
락은 풀리고, 3분 뒤 다음 틱이 같은 보드를 다시 읽는다.

같은 항목으로 두 번 깨우지 않게 하는 것은 세션 자신의 첫 동작이다
(`scripts/local-work-prompt.md:18-26`):

```
worklog work update {{ITEM_ID}} --actor {{EXECUTOR}} --status in_progress
```

프롬프트가 그 자리에서 그렇게 말한다: "착수 표시가 다음 타이머 틱이 같은 일로
두 번째 세션을 깨우는 것을 막는 유일한 장치다. 표시 없이 일하면 세션이 겹친다."

**그래서 이 보장은 코드가 아니라 세션이 지시를 따르는지에 걸려 있다.** 세션이
그 단계를 건너뛰거나, 그 명령이 실패하거나, 그 전에 죽으면 항목은 `ready` 로
남는다. `worklog work update` 가 실패하면 아무것도 하지 말고 끝내라고 프롬프트가
지시하지만 (`local-work-prompt.md:24`), 그 지시를 강제하는 것 역시 스크립트가
아니다.

바뀐 것은 그 다음이다. 예전에는 다음 틱이 같은 항목으로 새 세션을 깨웠고 그것이
영원히 반복됐다. 지금은 스크립트가 세션 뒤에 보드를 다시 읽으므로 **잡히지
않았다는 사실 자체는 기계가 안다** — `outcome=unclaimed`, 그리고 냉각. 여전히
모르는 것은 왜 안 잡혔는지이고, 그것은 전사에만 있다.

## 권한 충돌

`ready` 상태 하나로 세션이 깨어나고 그 세션이 `git push` 까지 하는 것은,
`docs/cowork-mailbox.md` 가 명시적으로 금지하는 두 가지와 정면으로 부딪친다
(`cowork.py:269` 의 `NEVER_AUTONOMOUS` 에 `push` 가 있고,
`scripts/local-work-prompt.md:57-64` 는 푸시하라고 지시한다).
이 충돌은 해결되지 않았고, 양쪽 `file:line` 과 함께
`docs/cowork-mailbox.md` Part 3 에 기록돼 있다. 여기서 다시 판정하지 않는다.

스크립트 하나하나의 설명은 `docs/scripts.md` 에 있다.

## 배포도 배치다 — `hkwa-deploy`

`scripts/deploy-tick.sh`, 10분마다. 사람도 세션도 기다리지 않는다.

```
1  더러운 워크트리면 거부           빌드한 이미지를 아무도 이름 붙일 수 없다
2  origin/main 이 앞서면 ff-only
3  도는 이미지의 모듈 해시를 읽는다   앱이 실제로 import 하는 경로에서
4  HEAD 와 같으면 current, 끝
5  다르면 build → up -d
6  다시 해시를 읽어 HEAD 와 대조
```

**요점은 6번이다.** "빌드했다"는 확인이 아니다. 2026-09-04 에 세 번 연속으로
"배포됐다"고 보고됐는데, 대조한 것이 앱이 import 하지 않는 `/app/src` 사본이었다.
이 스크립트는 `rlwrld_worklog.__file__` 이 가리키는 경로에서 해시한다.

| `outcome` | 뜻 | 남은 상태 |
|---|---|---|
| `current` | 도는 이미지가 이미 HEAD 다 | 아무것도 안 함 |
| `deployed` | 빌드·재시작 후 해시가 HEAD 와 일치 | 배포 완료, 증명됨 |
| `image-stale` | 빌드·재시작했는데 여전히 HEAD 가 아니다 | **컨테이너는 재시작됐다.** 무엇이 도는지 `detail` 이 파일명으로 말한다 |
| `unverified` | 빌드·재시작했으나 컨테이너가 응답하지 않았다 | 재시작됨. 도는 것 불명 |
| `build-failed` / `up-failed` | 그 단계에서 실패 | 이전 이미지가 계속 돈다 |
| `refused` | 더러운 워크트리, 또는 ff 불가 | 아무것도 안 함 |
| `no-docker` | 사용자 매니저 PATH 에 docker 없음 | 아무것도 안 함 |
| `busy` | 앞 틱이 아직 돈다 | 아무것도 안 함 |

`image-stale` 과 `unverified` 는 둘 다 "재시작은 했다"는 점에서 위험하다.
이 둘이 나오면 사람이 봐야 한다.
