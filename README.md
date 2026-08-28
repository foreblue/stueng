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
english-class    수업 전사·노트
   ↑ scp (수업 PC가 녹음한 파일)
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

**텔레그램은 두 가지를 보낸다.** 팟캐스트 어휘(`main.py`, `weekly_study.py send`)는
예전 그대로 아침에 간다 — 매일 읽는 습관이 이미 붙어 있고, 웹앱이 그것을 대체하는
것이 아니라 옆에 붙는 것이다. 여기에 복습 알림(`vocab.sync notify`)이 하나 더 붙는데,
이쪽은 단어를 싣지 않고 개수와 링크만 보내 앱을 열게 만든다.

**서버는 복습만 한다.** 팟캐스트를 받아오는 일도, LLM 을 부르는 일도 하지 않는다.
분석에 쓰는 프록시는 이 맥북에만 있고 전사는 mlx-whisper(Apple Silicon) 라 옮길 수
없다. 그 제약을 우회하는 대신 받아들였다 — 덕분에 API 비용이 0원이고 서버가 가볍다.

**수업은 다른 PC 에서 진행된다.** 헤드셋을 쓰므로 맥에서는 소리를 잡을 수 없다. 길이 둘이다.

1. **맥이 노트를 쓴다.** 그 PC 가 오디오 트랙 두 개(강사/나)로 녹음해
   `~/Movies/english-class/inbox/` 로 `scp` 하면 `import.sh` 가 맥 녹음과 같은 형태로
   쪼갠다. 그 뒤 전사·노트·수집은 종전 그대로다. 교정 표까지 나오니 어휘의 질이 가장 좋다.
2. **그 PC 가 직접 보낸다.** `english-class` 스킬이 그 PC 에서 돌고 있으면 노트도 거기
   쌓인다. `python -m vocab.remote --note <노트>` 가 그것을 `/ingest` 로 밀어 넣는다.
   **뜻·예문·교정이 이미 들어 있다** — 노트를 쓴 것이 그 PC 의 Claude 이기 때문이다.
   표를 읽는 규칙은 `vocab.notes` 한 곳에 있고 `collect` 와 같은 코드다.

둘째 길에는 보조 경로가 하나 더 있다. `--transcript` / `--audio` 는 전사문에서 반복된
낱말을 빈도 기준으로 줍는데, 여기서 **선정은 옮겨지지만 뜻은 못 옮긴다** — 규칙과 달리
뜻에는 LLM 이 필요하고 자격증명은 맥에만 있다. 그래서 뜻을 비운 채 보내고, 서버는 그
어휘를 **카드로 만들지 않은 채** 쌓아 둔다(`study._new_word_query` 가 막는다). 맥에서
`vocab.tutor` 가 돌 때 채워지고 그때부터 출제된다. 워커가 며칠 밀려도 빈 문제가 나가지
않고 새 카드만 늦어진다.

두 길은 배타적이지 않다. 표제어가 겹치면 서버가 합치고, 원격이 비워 둔 뜻은 나중에 들어온
수업 노트가 채운다. 자세한 설정은 `skills/english-class/SKILL.md`.

**수업 PC 에는 좁은 토큰만 준다.** `VOCAB_INGEST_TOKEN` 은 `/api/export` 까지 여는데,
그건 복습 기록이 담긴 DB 전체 덤프이고 서버에만 있는 유일한 데이터다. 어휘를 넣기만
하면 되는 기계에 줄 권한이 아니다. `/ingest` 에서만 통하는 `VOCAB_REMOTE_TOKEN` 을
따로 두어, 그 PC 에서 새더라도 잃는 것이 쓰기 권한 하나에 그치게 한다.

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

python -m vocab.tutor              # 뜻 채우기 + 작문 첨삭 + 막힌 카드 기억술 (LLM 사용)
python -m vocab.tutor --dry-run

python -m vocab.optimize --check   # FSRS 재학습에 기록이 충분한지
python -m vocab.optimize           # 파라미터 재학습 (torch 필요)
```

로컬 개발 서버:

```bash
VOCAB_PASSWORD=dev VOCAB_INGEST_TOKEN=dev \
  uvicorn vocab.app.main:app --reload --port 8000
```

수업 PC 에서 (이 저장소를 clone 해 두고 `VOCAB_SERVER_URL`·`VOCAB_REMOTE_TOKEN` 을 설정한 뒤):

```bash
python -m vocab.remote --note "영어수업 2026-08-28.md"   # 노트 그대로 (뜻 포함)
python -m vocab.remote --transcript transcript.md       # 전사문에서 후보 (뜻 비움)
python -m vocab.remote --audio class.mkv                # 전사부터 그 PC 에서
python -m vocab.remote --note "..." --dry-run
```

## 배포

서버 코드를 고쳤으면(`vocab/app/`, `vocab/models.py`, `vocab/study.py`, `vocab/compose.py`)
컨테이너를 다시 올려야 반영된다. `vocab/remote.py` 의 뜻 대기 왕복과 `VOCAB_REMOTE_TOKEN`
도 여기 포함된다 — 게이트웨이 `docker-compose.yml` 이 그 값을 컨테이너로 넘긴다.

`~/workspace/deepheart-gw` 게이트웨이(Traefik)에 컨테이너로 붙는다.
주소는 https://stueng.deepheart.duckdns.org 이고 인증서는 Let's Encrypt DNS-01 로
자동 발급된다.

```bash
cd ~/workspace/deepheart-gw
docker compose build stueng
docker compose up -d stueng
docker logs -f stueng
```

게이트웨이 `.env` 에 세 값이 있어야 한다. `VOCAB_INGEST_TOKEN` 은 이 저장소의
`.env` 와 **같은 값**이어야 로컬에서 어휘를 밀어 올릴 수 있다.

```
VOCAB_PASSWORD=...
VOCAB_SECRET_KEY=...
VOCAB_INGEST_TOKEN=...   # stueng/.env 와 동일
```

`VOCAB_ENV=production` 은 `docker-compose.yml` 이 직접 넣는다. 다른 곳에 배포한다면
반드시 함께 넘겨야 한다 — 빠지면 쿠키에서 `Secure` 가 사라지고 `VOCAB_SECRET_KEY`
누락도 조용히 넘어간다(재시작마다 로그아웃된다).

### 루프백 포트 8010

컨테이너는 Traefik 뒤에 있으면서 `127.0.0.1:8010` 에도 묶여 있다. 공유기가 헤어핀
NAT 을 지원하지 않아 **내부에서 공인 주소로 되돌아오지 못하기 때문**이다. 이 맥북에서
도는 것들(`vocab.sync push`, `vocab.tutor`, `vocab.optimize`)은 이 포트를 쓴다.
LAN 에는 열려 있지 않다 — 루프백 전용이다.

**수업 PC 는 이 포트를 쓰지 않는다.** 루프백 바인딩이라 LAN 에서 닿지 않고, 애초에
업로드가 집 밖에서 일어난다. 그쪽은 게이트웨이의 공인 주소
(`https://stueng.deepheart.duckdns.org`)를 그대로 쓴다 — 이미 정식 인증서로 서비스되고
있고 DuckDNS 가 IP 를 따라간다. 열 포트도 터널도 없다.

집 안에서 올릴 일이 생기면 그때만 그 PC 의 hosts 에
`192.168.45.93 stueng.deepheart.duckdns.org` 를 넣는다. Traefik 443 은 LAN 에도 열려
있고 인증서는 이름에 대해 발급된 것이라 검증도 정상이다. 밖으로 나갈 때 지워야 하므로
기본으로 넣지는 않는다.

같은 이유로 텔레그램 알림의 링크(`VOCAB_APP_URL`)는 집 안 와이파이에서는 열리지 않을
수 있다. 셀룰러로는 정상이다.

### 데이터

복습 기록은 `stueng-data` 도커 볼륨의 SQLite 에 있다. 어휘는 로컬에서 다시 만들 수
있지만 이건 여기밖에 없으므로 가끔 받아 둔다.

```bash
curl -H "X-Ingest-Token: $VOCAB_INGEST_TOKEN" \
  http://127.0.0.1:8010/api/export > backup-$(date +%Y%m%d).json
```

Postgres 로 옮기려면 `requirements-app.txt` 의 `psycopg[binary]` 주석을 풀고
`VOCAB_DB_URL` 만 바꾸면 된다. 스키마는 동일하다.

## 정기 실행

`crontab` 이 아니라 **`mycron`** (launchd `com.dysim.mycron`) 이 돌린다. 목록은
`mycron list`, 추가는 `mycron add`.

### 등록돼 있는 것

| 이름 | 시각 | 하는 일 |
| --- | --- | --- |
| `stueng-daily` | `0 7 * * *` | Up First 일일 분석 |
| `stueng-planetmoney-prepare` | `30 4 * * thu` | Planet Money 주간 학습 계획 |
| `stueng-planetmoney-send` | `0 7 * * mon-fri` | 그날 차수의 단어·표현 전송 |
| `stueng-vocab-push` | `10 7 * * *` | `vocab.sync push` |

`vocab.tutor` 와 `vocab.sync notify` 는 아직 안 넣었다 → `todo.md`.

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
| `VOCAB_INGEST_TOKEN` | 양쪽 | 기계용 API 인증. 양쪽이 같아야 한다. **여섯 엔드포인트를 다 연다 — `/api/export` 포함** |
| `VOCAB_REMOTE_TOKEN` | 서버 + 수업 PC | `/ingest` 와 `/api/handled` 만 여는 좁은 토큰. 비우면 없는 것으로 동작한다 |
| `VOCAB_DB_URL` | 양쪽 | 비우면 로컬 `data/vocab.db` |
| `VOCAB_ENV` | 서버 | `production` 이어야 한다. 아니면 세션 쿠키에서 `Secure` 가 빠지고 설정 누락 검사도 꺼진다 |
| `VOCAB_PASSWORD` | 서버 | 로그인 비밀번호 |
| `VOCAB_SECRET_KEY` | 서버 | 쿠키·출제 토큰 서명 |
| `VOCAB_TZ` | 양쪽 | 기본 `Asia/Seoul` |
| `VOCAB_DESIRED_RETENTION` | 서버 | 목표 정답률. 기본 0.9 |
| `VOCAB_SCHEDULER` | 서버 | `fsrs`(기본) 또는 `fixed` |
| `VOCAB_FSRS_PARAMS` | 서버 | `vocab.optimize` 가 학습한 21개 값 |

## 알아둘 것

**마이그레이션 도구가 없다.** 스키마는 기동 시 `create_all` 로만 만들어진다. 컬럼을
추가하면 기존 테이블에는 반영되지 않는다. 뜻 대기 상태를 새 컬럼이 아니라
`meaning_kr == ""`(`models.PENDING_GLOSS`) 로 표현한 것도 이 때문이다 — 기존 DB 에
손대지 않고 컨테이너만 다시 올리면 된다. 로컬은 `vocab.collect --rebuild` 로 다시
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
