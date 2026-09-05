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
   └── incoming/NNNN-*.patch ──────────────▶ scripts/apply-incoming.sh
       (device_commit_files 로 직접 씀)          git am --3way
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

## 적용

```bash
cd "/home/hk/Documents/ChatGPT/RLWRLD workspace"
bash scripts/apply-incoming.sh
.venv/bin/python -m pytest -q
git push origin main
```
