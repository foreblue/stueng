# stueng

NPR 팟캐스트와 화상 영어 수업에서 어휘를 모아, 간격 반복으로 외우는 개인용 서비스.

설계 근거는 [어휘 암기 리서치 브리프](https://claude.ai/code/artifact/44514bc7-cfbc-40b3-a8eb-c9a3b2d223f6)에
정리돼 있다. 핵심만 옮기면 세 가지다 — **모든 노출은 문제여야 하고**(재노출은 학습이
아니다), **간격을 두어야 하며**, **틀린 카드는 그 세션 안에서 맞힐 때까지 다시 나와야 한다.**

## 구조

일이 두 군데로 나뉜다. 나누는 기준은 LLM 자격증명과 macOS 의존성이다.

```
맥북 (로컬)                              Fly.io (서버)
─────────────────────────────           ─────────────────────────
main.py          Up First 일일 분석
weekly_study.py  Planet Money 주간
english-class    수업 녹음·전사
   ↓ (localhost:9000 LLM 프록시)
data/*.json
   ↓
vocab.collect    어휘 정규화
vocab.sync push  ──────────────────→    /ingest      어휘 저장
                                          ↓
                                        복습 웹앱     문제 출제·채점
                                          ↓
vocab.tutor      ←──────────────────    /api/tasks   LLM 일감
                 ──────────────────→    (첨삭·기억술)
messenger.py     텔레그램 알림    ←──   /api/progress
```

**서버는 복습만 한다.** 팟캐스트를 받아오는 일도, LLM 을 부르는 일도 하지 않는다.
분석에 쓰는 프록시는 이 맥북에만 있고 수업 녹음은 ScreenCaptureKit 기반이라 옮길 수
없다. 그 제약을 우회하는 대신 받아들였다 — 덕분에 API 비용이 0원이고 서버가 가볍다.

**소유권도 나뉜다.** 어휘(`word`, `occurrence`)는 로컬이 원본이라 언제든 다시 만들 수
있다. 복습 기록(`card`, `review_log`)은 서버에만 있는 유일한 데이터다. 그래서
`/stats` 에 백업 내려받기가 있다.

## 로컬 명령어

```bash
python -m vocab.collect            # data/*.json + 수업 노트 → 어휘 저장소
python -m vocab.collect --rebuild  # 저장소를 비우고 다시 만든다

python -m vocab.sync push          # 어휘를 모아 서버로 (멱등, 매번 전체를 보낸다)
python -m vocab.sync push --dry-run
python -m vocab.sync notify        # 복습할 게 있으면 텔레그램 알림
python -m vocab.sync status        # 서버 현황

python -m vocab.tutor              # 작문 첨삭 + 막힌 카드 기억술 (LLM 사용)
python -m vocab.tutor --dry-run

python -m vocab.optimize --check   # FSRS 재학습에 기록이 충분한지
python -m vocab.optimize           # 파라미터 재학습 (torch 필요)
```

로컬 개발 서버:

```bash
VOCAB_PASSWORD=dev VOCAB_INGEST_TOKEN=dev \
  uvicorn vocab.app.main:app --reload --port 8000
```

## 배포

`flyctl` 과 Neon 계정이 필요하다. 처음 한 번만.

```bash
# 1) Postgres — neon.tech 에서 프로젝트를 만들고 연결 문자열을 복사한다.
#    Fly Postgres 대신 Neon 을 쓰는 이유: 복습 기록은 로컬에 원본이 없는 유일한
#    데이터라 관리형 백업이 있는 쪽이 맞다.

# 2) 앱 생성 (fly.toml 의 app 이름이 바뀐다)
fly launch --no-deploy

# 3) 비밀값
fly secrets set \
  VOCAB_DB_URL="postgresql://...neon.tech/vocab?sslmode=require" \
  VOCAB_PASSWORD="$(openssl rand -base64 24)" \
  VOCAB_SECRET_KEY="$(openssl rand -base64 32)" \
  VOCAB_INGEST_TOKEN="$(openssl rand -hex 32)"

# 4) 배포
fly deploy

# 5) 어휘를 밀어 올린다
export VOCAB_SERVER_URL=https://<앱이름>.fly.dev
export VOCAB_INGEST_TOKEN=<위에서 만든 값>
python -m vocab.sync push
```

`VOCAB_SECRET_KEY` 를 빼먹으면 프로덕션에서 기동이 거부된다 — 없으면 재시작마다
로그아웃되고, 출제 토큰 서명도 매번 무효가 된다.

폰에서는 사파리로 열어 "홈 화면에 추가" 하면 PWA 로 설치된다.

## cron

로컬 crontab. 시각은 취향대로.

```cron
# 아침 6시 — Up First 분석 + 에피소드 알림
0 6 * * *  cd ~/workspace/stueng && .venv/bin/python main.py >> logs/daily.log 2>&1

# 월요일 6시 반 — Planet Money 주간 어휘 준비 + 에피소드 알림
30 6 * * 1 cd ~/workspace/stueng && .venv/bin/python weekly_study.py prepare >> logs/weekly.log 2>&1
35 6 * * 1 cd ~/workspace/stueng && .venv/bin/python weekly_study.py send    >> logs/weekly.log 2>&1

# 7시 — 어휘를 서버로 밀고 복습 알림
0 7 * * *  cd ~/workspace/stueng && .venv/bin/python -m vocab.sync push   >> logs/sync.log 2>&1
5 7 * * *  cd ~/workspace/stueng && .venv/bin/python -m vocab.sync notify >> logs/sync.log 2>&1

# 저녁 9시 — 작문 첨삭·기억술 (LLM 프록시가 떠 있어야 한다)
0 21 * * * cd ~/workspace/stueng && .venv/bin/python -m vocab.tutor >> logs/tutor.log 2>&1
```

`.venv/bin/python` 을 쓴다. 어휘 후보 선정에 `wordfreq` 가, 저장소에 `sqlalchemy` 가
필요하기 때문이다. 시스템 파이썬으로 돌리면 `analyzer.py` 가 조용히 예전 선정 방식으로
돌아간다(로그에 경고가 남는다).

## 환경변수

| 이름 | 어디서 | 설명 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | 로컬 | 알림 |
| `PROXY_URL` | 로컬 | LLM 프록시. 기본 `http://localhost:9000` |
| `AI_MODELS` | 로컬 | 폴백 순서 |
| `VOCAB_SERVER_URL` | 로컬 | 배포된 서버 주소 |
| `VOCAB_APP_URL` | 로컬 | 알림에 넣을 주소. 비우면 서버 주소를 쓴다 |
| `VOCAB_INGEST_TOKEN` | 양쪽 | 기계용 API 인증. 양쪽이 같아야 한다 |
| `VOCAB_DB_URL` | 양쪽 | 비우면 로컬 `data/vocab.db` |
| `VOCAB_PASSWORD` | 서버 | 로그인 비밀번호 |
| `VOCAB_SECRET_KEY` | 서버 | 쿠키·출제 토큰 서명 |
| `VOCAB_TZ` | 양쪽 | 기본 `Asia/Seoul` |
| `VOCAB_DESIRED_RETENTION` | 서버 | 목표 정답률. 기본 0.9 |
| `VOCAB_SCHEDULER` | 서버 | `fsrs`(기본) 또는 `fixed` |
| `VOCAB_FSRS_PARAMS` | 서버 | `vocab.optimize` 가 학습한 21개 값 |

## 알아둘 것

**마이그레이션 도구가 없다.** 스키마는 기동 시 `create_all` 로만 만들어진다. 컬럼을
추가하면 기존 테이블에는 반영되지 않는다. 로컬은 `vocab.collect --rebuild` 로 다시
만들면 되지만(어휘는 재생성 가능), 서버에 복습 기록이 쌓인 뒤 스키마를 바꾸려면
Alembic 을 넣거나 손으로 `ALTER TABLE` 을 해야 한다.

**`analysis.key_sentences` 는 저장만 하고 쓰지 않는다.** 어휘가 아니라 문장이라
지금의 카드 모델에 맞지 않는다. 넣으려면 카드 종류를 하나 더 만들어야 하는데, 산출
단계가 이미 문장 수준 인출을 다루고 있어 얻는 것에 비해 스키마와 UI 부담이 크다.
`data/*.json` 에는 계속 남으므로 나중에 결정해도 된다.

**대조군이 살아 있다.** `VOCAB_SCHEDULER=fixed` 로 두면 Karatas et al. (2025) 이
검증한 1·3·9·17일 고정 간격으로 바뀐다. FSRS 가 실제로 더 나은지 비교할 때 쓴다.
다만 고정 간격은 기억 상태를 만들지 않으므로, 되돌릴 때 그 카드들은 학습 단계부터
다시 쌓인다.

## 테스트

```bash
for f in test_*.py; do .venv/bin/python "$f"; done
```
