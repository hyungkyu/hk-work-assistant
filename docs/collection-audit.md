# 수집 감사

2026-09-05, 다섯 소스 중 셋이 나흘 동안 수집되지 않았다. 증거는 그 나흘 내내
수집 현황 격자에 있었다. **아무도 그 페이지를 열지 않았을 뿐이다.** 배치는 두
소스만 이름으로 적고 있었고 코드는 다섯으로 자랐는데, 그 차이는 격자를 여는
사람에게만 보였다. 이것이 격자를 여는 쪽이다.

**한 시간.** 소스-날짜 하나가 비어 있는 채로 아무도 모르고 지나가도 되는 가장
긴 시간이다.

```bash
worklog collection audit                  # 끝난 KST 3일, 프로덕션
worklog collection audit --days 7
worklog collection audit --source notion
worklog collection audit --summary        # next_action 에 들어갈 한 줄만
```

매일 02:00 Asia/Seoul 에 `hkwa-collection-audit.timer` 가 돌린다. 현재 수집
배치는 06:00에 시작해 두 시간 이상 걸릴 수 있으므로, 감사는 그보다 먼저 전날까지
완료된 실행을 읽는다. 두 타이머 모두 `Persistent=true`다.

## 재는 것 하나

`collected` 도 `collected_with_skips` 도 아닌 **소스-날짜 칸 전부**.

| 판정 | 무엇을 뜻하나 | 보고되나 |
|---|---|---|
| `collected` | 그 날짜를 읽은 실행이 있다 | 아니오 |
| `collected_with_skips` | 실행이 끝났고 못 닿은 것을 이름으로 말했다 | 아니오 |
| `partial` | 실행이 무엇을 놓쳤는지 스스로 모른다 | 예 |
| `failed` | 그 날짜의 실행이 실패로 끝났다 | 예 |
| `running` | 아직 돌고 있다 | 예 |
| `not_collected` | 증거가 없다 | 예 |
| `unknown` | 증거는 있는데 읽어낼 수 없다 | 예 |
| `unverified` | V0 legacy 디렉터리. 있다는 것 말고는 아무것도 증명하지 않는다 | 예 |
| `unexamined` | 아무도 열어보지 않았다 | 예 |

`collected_with_skips` 가 위반이 아닌 이유: **정직한 보고는 결함이 아니다.**
끝까지 돌았고 못 닿은 것을 하나하나 이름으로 남긴 실행이다. `partial` 은 자기가
무엇을 놓쳤는지 모르는 실행이고, 그래서 그쪽만 보고된다.

`unknown` · `unverified` · `unexamined` 는 각각 다른 이유로 아카이브가 답을 못
하는 상태다. 셋 다 "읽었다"는 뜻은 아니다. **증거의 부재는 수집의 증거가
아니다.**

행에 아예 없는 소스도 `unknown` 으로 보고된다. 칸이 없다는 것은 가장 큰 소리로
수집되지 않았다는 뜻이다.

## 오늘은 왜 안 재나

오늘은 아직 안 끝났다. 끝나지 않은 날짜의 칸이 불완전한 것은 결함이 아니라
사실이고, 그것을 매일 보고하는 감사는 하루에 한 번씩 늑대를 부른다. 창은 항상
**어제까지**다(`--days` 는 어제부터 거꾸로 센다).

## 시각 커버리지는 판정을 바꾸지 않는다

한 실행이 흠 없이 돌고도 하루의 절반만 봤을 수 있다. 그것은 `coverage` 가 아니라
`completeness` · `time_coverage` 의 문제이고, 감사는 `coverage` 만 본다.
발견 항목에 `time_coverage` 를 실어 보내니 읽는 쪽이 판단할 수는 있다.

## 보고할 뿐, 수집하지 않는다

배치는 아무것도 다시 수집하지 않는다. 발견 하나하나가 사람이 결정할 일이고 —
다시 돌릴 것인가, 아니면 그 구멍을 받아들이고 이유를 적을 것인가 — 조용히
백필을 시작하는 배치는 그 결정을 새벽 두 시에 아무도 안 보는 데서 대신 내리는
것이다.

결과는 두 군데로 간다.

1. `incoming/last-collection-audit.json` — 전체 보고서, 매 틱. 다른 틱 파일과
   같은 `outcome` 키를 쓴다(`clean` · `gaps` · `busy` · `no-worklog` ·
   `audit-failed`).
2. `$APP_CONFIG_ROOT/collection-audit-target` 에 업무 ID 가 적혀 있으면, 그
   항목의 `next_action` 에 한 줄 요약. 아웃박스를 통해 쓰므로 감사는 잠금을
   잡지 않고 다른 큐 편집과 같은 영수증을 남긴다.

대상 파일이 없어도 감사는 돌고 기록도 남는다. 보드에 쓰는 줄만 건너뛴다.

`$APP_CONFIG_ROOT/collection-audit-days` 에 숫자가 있으면 창의 길이가 그쪽이
된다. 없으면 3일.

## 한 줄이 500자를 넘지 않는 이유

`next_action` 은 저장소가 500자로 자르고, 넘으면 **거부**한다. 그래서 요약은
큐에서가 아니라 만드는 쪽에서 잘린다(`collection_audit.MAX_SUMMARY_CHARS`) —
`board_audit` 과 같은 이유, 같은 자리다.

요약은 세는 데서 그치지 않고 **빠진 소스-날짜를 이름으로 부른다**. 숫자는 뭔가
잘못됐다는 것까지만 말하고, 이름은 무엇을 돌려야 하는지를 말한다. 여섯 개까지
부르고 나머지는 `외 N` 으로 센다.

```
수집 감사 09-06 02:00 KST · 2026-09-03~2026-09-05 15칸 — 미수집 3 · notion 09-03, notion 09-04, slurm 09-05
```

## 프로덕션이 기본이다

`--environment` 를 안 주면 `production` 이다. 이 보고서를 읽는 사람은 **진짜
데이터가 수집됐는지**를 판단하려는 것이고, 그 질문에 스모크 실행이 답하면 그건
거짓말이다. `--environment all` 만이 창을 넓힌다 — 수집 현황 API 가 같은 규칙을
쓴다(`collection_web.DEFAULT_ENVIRONMENT`).

## 구멍을 발견한 다음

하루짜리면 그 하루만 다시 돌린다.

```bash
scripts/backfill-days.sh 2026-09-03 2026-09-03
```

여러 날이면 여전히 하루에 하나씩이다. `daily-collect` 는 이틀 넘는 창을 한
실행으로 도는 것을 거부한다 — 끝나기 전까지 아무것도 적립되지 않기 때문이다.
[daily-collection.md](daily-collection.md#a-window-too-wide-for-one-run) 를 보라.
