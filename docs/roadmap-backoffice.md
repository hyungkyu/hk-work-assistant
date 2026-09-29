# 로드맵 백오피스 — 구현 명세

작성: 모리(Cowork), 2026-09-28. HK 지시로 착수. 세 개의 업무 항목이 이 문서를 참조한다.

- `A. 권한 — staff 비번과 자물쇠`
- `B. 로드맵 DB 전환과 조회 API`
- `C. 매핑 어드민과 현행화·변경 이력`

현재 산출물은 `/data/rlwrld-worklog/digest/` 에 있다: `roadmap.html`(뷰어, 단독 동작), `roadmap.json`(스냅샷), `roadmap-product-map.csv`, `roadmap-item-map.csv`. 뷰어는 완성이고, 아래 셋은 서버 쪽이다.

---

## A. 권한 — staff 비번과 자물쇠

HK가 어드민 계정과 일반 계정 두 가지로 접속해 화면을 비교하고 싶어 한다. role 모델(`super_admin` / `company_user` / `agent`)과 서버 차단(`require_super_admin_session`)은 이미 있다. 없는 것은 비밀번호로 `company_user` 가 되는 문과, UI 표시 두 가지다.

**A-1. staff 비번** (`admin_store.py`)

- `admin-password.json` 과 같은 형식으로 `staff-password.json` 을 둔다. scrypt, 같은 파라미터, `_atomic_private_write`, 0600.
- `set_staff_password` / `verify_staff_password` / `staff_password_set()` 를 `set_admin_password` / `verify_admin_password` 와 같은 모양으로 추가한다. 길이 검증은 `_validate_password` 를 그대로 쓴다.
- admin 비번과 달리 **재설정이 가능해야 한다.** `set_admin_password` 는 이미 존재하면 거절하지만, staff 비번은 돌려쓰는 값이라 super_admin 이 언제든 바꿀 수 있어야 한다.

**A-2. 로그인 분기** (`admin_web.py`)

- `POST /api/v1/admin/login` 에서 admin → staff 순으로 검증한다. admin 이 맞으면 지금처럼 `create_session(subject=..., role="super_admin")`, staff 가 맞으면 `role="company_user"`, `auth_method="local_emergency_staff"`.
- 둘 다 틀리면 지금과 같은 401. **어느 쪽이 틀렸는지 응답으로 구분하지 않는다.**
- 감사 기록의 `details.method` 에 `local_emergency` / `local_emergency_staff` 를 구분해 남긴다.
- `role` 값이 기존 Google 로그인의 `company_user` 와 같으므로 **권한 분기 코드는 건드리지 않는다.** 이것이 이 설계를 고른 이유다.
- staff 비번 설정: `PUT /api/v1/admin/staff-password`, `require_super_admin_session` + CSRF. 보안 탭에 입력란.
- `GET /api/v1/admin/session` 응답에 `staff_password_set: bool` 을 더한다.

**A-3. 자물쇠** (`static/admin.html`, `static/admin.js`)

- super_admin 전용 화면의 nav 버튼과 카드에 `data-requires="super_admin"` 을 붙인다.
- 세션 role 이 `super_admin` 이면 그 요소에 🔒 배지를 렌더하고, 아니면 요소를 **감춘다**(`hidden` 클래스, disabled 가 아니라).
- 자물쇠는 "지금 내가 보는 이것은 남에게 안 보인다" 는 표시이지 잠금장치가 아니다. 실제 차단은 서버가 한다. 이 문장을 주석으로 남긴다.
- 로드맵 탭은 `company_user` 에게도 보인다(자물쇠 없음). 매핑 편집만 super_admin.

**A-4. 시험**

- staff 비번으로 로그인하면 role 이 `company_user` 이고 super_admin 전용 엔드포인트가 403 이다.
- admin 비번으로 로그인하면 지금과 똑같이 동작한다(회귀).
- staff 비번 미설정 상태에서 아무 비번이나 넣어도 admin 검증만 돌고 결과가 달라지지 않는다.
- staff 비번 설정은 super_admin 만 가능하고, company_user 는 403.

---

## B. 로드맵 DB 전환과 조회 API

정적 HTML 생성의 속도 이점은 이 규모(95행·34제품·7패밀리)에서 없다. 지금 파일은 86KB JSON 을 통째로 품은 채 갱신 없이 늙는다. DB 로 옮긴다.

**B-1. 스키마** (`sql/` 에 마이그레이션 추가)

```sql
roadmap_team     (id text pk, label_ko, label_en, label_ja, sort int)
roadmap_family   (id text pk, label_ko, label_en, label_ja, color text, sort int)
roadmap_product  (id bigserial pk, name text unique, family_id, owner_team_id,
                  detail_url text, sort int)
roadmap_item     (id bigserial pk, notion_block_id text null, team_id, product_id,
                  horizon text, kind text, text_ko, text_en, text_ja,
                  source_url text, hash text, snapshot_id bigint,
                  horizon_override bool default false,
                  product_override bool default false,
                  kind_override bool default false)
roadmap_snapshot (id bigserial pk, taken_at timestamptz, label text, prev_html_url text)
roadmap_change   (id bigserial pk, snapshot_id, item_id, type text,
                  before_text text, after_text text)
```

- `horizon` ∈ `now` / `next` / `soon` / `someday`. `kind` ∈ `dev` / `ops`.
- `hash` = 한국어 원문 SHA-1 앞 10자. 변경 판정의 단위다.
- 시드는 `/data/rlwrld-worklog/digest/roadmap.json` 과 두 CSV 에서 넣는다. `roadmap.json` 의 `items[]` 가 그대로 `roadmap_item` 이고, `product_map.csv` 가 `roadmap_product` 다.

**B-2. 조회 API**

- `GET /api/v1/roadmap` → 현행 `roadmap.json` 과 **동일한 스키마**를 돌려준다. 이것이 계약이다. `ui` / `teams` / `families` / `groups` / `horizons` / `kinds` / `teamNotes` / `items` / `snapshot` / `source` / `generated`.
- `require_company_session` (조회는 일반 사용자도).
- 언어는 서버에서 거르지 않는다. 세 언어를 다 실어 보내고 토글은 화면이 한다. 95행이면 그게 싸다.

**B-3. 뷰어 교체** (`digest/roadmap.html` → `static/roadmap.html`)

- 지금 뷰어는 `<script id="payload">` 의 인라인 JSON 을 읽는다. **최초 로드를 `GET /api/v1/roadmap` 으로 바꾸는 것이 전부다.** 렌더러·필터·언어·그룹 머리글·이력 서랍 코드는 손대지 않는다.
- 현행화 버튼이 이미 같은 모양의 JSON 을 `fetch` 해 대조하도록 되어 있으므로, 그 함수의 URL 만 `./roadmap.json` → `/api/v1/roadmap` 으로 바꾼다.
- 좌측 nav 의 `로드맵` 버튼에서 `disabled` 를 떼고 `data-page="roadmap"` 페이지를 붙인다.
- 정적 파일은 백오피스가 죽었을 때의 오프라인 사본으로만 야간 배치 1회 남긴다.

---

## C. 매핑 어드민과 현행화·변경 이력

**C-1. 매핑 화면** — `설정 > 로드맵 매핑`. 표 두 개.

제품 표: 제품명 / 패밀리(드롭다운 7종) / 주관팀(드롭다운 5팀) / 상세 URL 이 편집 가능. 걸친 팀·건수·개발운영 수는 항목에서 계산되는 읽기 전용. 행 추가·삭제 가능하되 삭제는 그 제품을 쓰는 항목이 0건일 때만.

항목 표: 본문(ko)·팀은 읽기 전용(원본이 정본), 제품·개발운영·시간축이 편집 가능.

**제품이 팀 하나에 안 붙는다.** 실제로 `Simulation`(rp+hw), `Data Contract`(rp+infra), `Dataset Capture`(loop+rp) 가 두 팀에 걸친다. 그래서 팀×제품 N:M 테이블을 따로 두지 않는다 — 팀은 항목에 달려 있고, 제품에는 집계 기본값인 `owner_team_id` 만 둔다. 두 곳에 두면 어긋난다.

편집은 `require_super_admin_session` + CSRF, 변경은 기존 `store().audit` 로 감사 기록에 남긴다.

**C-2. 현행화가 사람 손을 덮어쓰지 않는 규칙 — 이것이 이 업무의 핵심이다**

현행화는 원본에서 오는 것만 갱신한다: 본문 3개 국어, 팀, `source_url`, `hash`. 어드민에서 지정한 값은 `*_override` 플래그가 서 있으면 **보존한다**. 새로 생긴 항목만 자동 분류 규칙을 태운다. 자동 분류 규칙은:

- 원본 표의 `이번달` → `now`, `다음달` → `next`.
- 원본 `장기계획` 중 분기(4Q 등)가 명시된 것 → `soon`, 시점 표기가 없는 것 → `someday`.
- 원본이 "일정 미정" 이라 적은 것 → `someday`.
- `kind` 자동 판정은 하지 않는다. 새 항목은 `dev` 로 두고 어드민에서 고친다. 추측이 틀리면 조용히 틀리기 때문이다.

**C-3. 현행화** — `POST /api/v1/roadmap/refresh`, super_admin + CSRF. Notion 에서 대상 페이지를 읽어 새 `roadmap_snapshot` 을 만들고, 항목을 `hash` 로 대조해 `roadmap_change` 를 남긴다. 직전 렌더 HTML 을 `digest/history/roadmap-<UTC stamp>.html` 로 복사하고 그 경로를 `prev_html_url` 에 넣는다. CLI `worklog roadmap refresh` 도 같은 코드를 부른다.

대상 페이지는 공식 로드맵 `3ce6cbdff6f68086b8f7cc174bbb040d` 이고, 이 페이지는 **지금 수집기가 한 번도 잡은 적이 없다**(`manifests/notion/production/checkpoint.json` 의 `known_objects` 에 없음). 그래서 현행화는 로컬 DB 가 아니라 Notion 서버를 직접 읽어야 한다. 별개로 이 페이지를 백오피스 `연결 > Notion` 의 seed 에 넣는 일이 있다.

**C-4. 변경 이력** — 뷰어의 `변경 이력` 서랍은 이미 있다. `GET /api/v1/roadmap` 응답의 `history[]` 에 `{at, prev, changes:[{type, team, text}]}` 를 실어주면 그대로 쌓인다. `type` 은 `added` / `changed` / `removed`.

**C-5. 남은 한계 (이 업무의 범위 밖, 기록만)**

- 항목 링크가 페이지 단위다. Notion 블록 id 를 잡아오면 블록 앵커까지 내려가고, `roadmap_item.id` 를 블록 id 로 승격하면 변경 추적이 정확해진다. 지금은 순번 id 라 원본에서 순서가 바뀌면 전부 추가+삭제로 잡힌다.
- Human Data 는 원본이 공란이라 팀 노트만 있다.
- 다음달(10월) 열은 2026-09-06 시점에 쓰인 계획 그대로다. 원본이 그 뒤로 갱신되지 않았다.
