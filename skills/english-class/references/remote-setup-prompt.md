이 PC 의 english-class 스킬이 만든 수업 노트를 어휘 복습 서버로 보내려 한다.
저장소는 이미 clone 돼 있으니 git pull 부터 하고 (vocab/notes.py, vocab/remote.py 가
새로 들어왔다) 아래를 설정해줘.

1. pip install wordfreq simplemma requests
2. 환경변수
       VOCAB_SERVER_URL=https://stueng.deepheart.duckdns.org
       VOCAB_REMOTE_TOKEN=<나에게 물어봐라. 여기 적지 않았다>
   주소는 반드시 이 이름으로 쓴다. 공인 IP 는 바뀌고 DuckDNS 가 그걸 따라간다.
   IP 를 박아넣거나 TLS 검증을 끄지 말 것 — 토큰이 오가는 채널이다.
   토큰은 셸 히스토리나 저장소에 남기지 말 것.

검증: 이미 쌓여 있는 노트 하나로
    python -m vocab.remote --note "<노트 경로>" --dry-run
어휘 몇 개와 교정 몇 건이 나오면 성공. --dry-run 은 아무것도 보내지 않는다.
확인되면 --dry-run 없이 실행하고, 지금까지 쌓인 노트를 전부 한 번씩 올려줘.
같은 노트를 두 번 올려도 서버가 합치므로 안전하다.

앞으로 수업 종료 시: 노트를 저장한 뒤 session.sh close 하기 전에 --note 로 보낸다.
로그에 "학습 상태를 못 받았습니다/거부당했습니다" 가 뜨면 멈추고 알려줘.
