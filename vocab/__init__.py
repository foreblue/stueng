"""전사에서 뽑은 어휘를 모아 간격 반복으로 복습하는 서비스.

- `collect`: 로컬 파이프라인 산출물(팟캐스트 분석 JSON, 영어수업 노트)을 어휘 저장소로 정규화
- `candidates`: 전사문에서 빈도 기반으로 학습 후보를 뽑는다
- `remote`: 수업 PC 에서 도는 추출기 — 전사문 → 후보 → 서버 (뜻은 비운 채)
- `banding`: 어휘의 학습 우선순위 밴드 판정
- `models`: 어휘(로컬이 원본) + 학습 상태(서버가 원본) 스키마
- `study`: 복습 세션 로직 — 재인출·형식 승급·예문 순환
- `scheduler`: FSRS 래퍼
- `compose` / `tutor`: 주 1회 작문 과제와 그 첨삭을 처리하는 로컬 워커
- `sync`: 로컬 <-> 서버
- `app`: 복습 웹앱
"""

# 로컬에서 돌 때 설정을 .env 한 곳에 모아 두기 위한 것. 배포 환경에는 .env 파일이
# 없고 진짜 환경변수가 주입되므로 아무 일도 하지 않는다.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - 서버 이미지에는 python-dotenv 가 없을 수 있다
    pass
