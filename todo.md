# TODO

## mycron 배치 두 건 등록

`vocab.tutor` 와 `vocab.sync notify` 가 아직 등록돼 있지 않다. (2026-08-28 기준)

```bash
mycron add --name stueng-vocab-tutor --cron '0 21 * * *' \
  --command "/bin/bash -lc 'cd ~/workspace/stueng && .venv/bin/python -m vocab.tutor'" \
  --skip-if-running --no-success-notify

mycron add --name stueng-vocab-notify --cron '5 7 * * *' \
  --command "/bin/bash -lc 'cd ~/workspace/stueng && .venv/bin/python -m vocab.sync notify'" \
  --skip-if-running --no-success-notify
```

**`vocab.tutor` 가 없으면 뜻이 빈 어휘가 영영 카드가 되지 않는다.** 수업 노트를
`vocab.remote --note` 로 올리는 경로는 뜻이 이미 있어 지금은 영향이 없지만,
`--transcript` / `--audio` 로 올린 후보는 뜻을 비운 채 들어오고 그걸 채우는 것이
이 작업이다. 증상이 조용해서(카드가 안 늘 뿐이다) 없는 줄 모르고 지나가기 쉽다.
전사문 경로를 쓰기 시작하면 그때는 반드시 넣어야 한다.

`vocab.tutor` 는 로컬 LLM 프록시(`localhost:9000`)가 떠 있어야 한다.
`vocab.sync notify` 가 없으면 복습할 게 있어도 알림이 가지 않는다 — 앱을 직접 연다.

등록 현황은 `mycron list`, 등록된 것들은 README 의 "정기 실행" 참고.
