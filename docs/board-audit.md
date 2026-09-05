# 보드 감사

P0 는 "실제로 하고 있는 일과 백오피스 업무 현황을 일치시키는 것"이었다.
2026-09-05 에 손으로 한 번 맞췄고, 같은 날 안에 다시 어긋났다. 아무도 보고
있지 않았기 때문이다. **한 번 확인한 성질은 성질이 아니다.** 이것이 보는 쪽이다.

```bash
worklog work audit --executor mori --executor local --executor batch --executor hk
worklog work audit --summary        # next_action 에 들어갈 한 줄만
```

30분마다 `hkwa-board-audit.timer` 가 돌린다.

## 재는 것 다섯 가지

| 검사 | 무엇을 잡나 | 왜 |
|---|---|---|
| `in_progress_without_assignee_activity` | `in_progress` 인데 **담당자 본인**의 이력이 N시간 없는 항목 | P0 조건 3. 계속 다시 깨지던 그것 |
| `live_without_next_action` | 열린 항목인데 `next_action` 이 비어 있다 | 보드가 다음 걸음을 말하지 않는다 |
| `progress_summary_over_three_lines` | 요약이 3줄을 넘는다 | P0 조건 6 |
| `assigned_outside_roster` | 존재하지 않는 실행자에게 걸린 일 | 아무도 집지 않을 큐 |
| `ready_untouched` | `ready` 인데 하루 넘게 안 바뀐 항목 | ready 가 아니거나 실행자가 없다 |

`done` 과 `cancelled` 은 재지 않는다. 큐를 떠난 항목에 대해서는 보드가 틀릴 것이
없다. 보관된 항목도 마찬가지다.

## 왜 `updated_at` 을 안 쓰나

첫 번째 검사는 항목의 `updated_at` 이 아니라 **이력에서 담당자 본인의 항목**을
찾는다. 요청자가 티켓을 손보면 `updated_at` 이 갱신되고, 그러면 찾으려던 바로 그
상태가 가려진다. 실제로 그렇게 가려진 적이 있다.

이력이 아예 없는 `in_progress` 항목도 보고된다.
`hours_since_assignee_touched` 가 `null` 로 나온다. **조용한 것은 일이
진행됐다는 증거가 아니다.**

## 명부는 추론하지 않는다

`--executor` 를 하나도 안 주면 명부 검사는 **건너뛴다**. 이 명령은 누가 존재하는지
알 방법이 없고, 보드에서 추론하면 고아 항목이 스스로를 정당화하게 된다.

기본 명부는 `scripts/board-audit-tick.sh` 안에 있고 —
`mori`(클라우드 개발) · `local`(운영계 검증) · `batch`(타이머) · `hk` —
`$APP_CONFIG_ROOT/board-audit-roster` 파일이 있으면 그쪽이 이긴다(한 줄에 하나).

## 보고할 뿐, 고치지 않는다

배치는 아무것도 재배정하거나 취소하지 않는다. 발견 하나하나가 사람이나 요청자가
결정할 일이고, 조용히 큐를 다시 짜는 배치는 그 결정을 대신 내리는 것이다.

결과는 두 군데로 간다.

1. `incoming/last-audit.json` — 전체 보고서, 매 틱.
2. `$APP_CONFIG_ROOT/board-audit-target` 에 업무 ID 가 적혀 있으면, 그 항목의
   `next_action` 에 한 줄 요약. 아웃박스를 통해 쓰므로 감사는 잠금을 잡지 않고
   다른 큐 편집과 같은 영수증을 남긴다.

대상 파일이 없어도 감사는 돌고 기록도 남는다. 보드에 쓰는 줄만 건너뛴다.

## 한 줄이 500자를 넘지 않는 이유

`next_action` 은 저장소가 500자로 자르고, 넘으면 **거부**한다. 2026-09-04 에
지시 하나가 정확히 그것 때문에 거부됐다. 그래서 요약은 큐에서가 아니라 만드는
쪽에서 잘린다(`board_audit.MAX_SUMMARY_CHARS`).
