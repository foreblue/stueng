#!/bin/bash
# 영어 수업 세션 관리 — start / status / end / close
set -euo pipefail

STUDY="$HOME/mylogs/study"
SDIR="$STUDY/.session"
META="$SDIR/current.json"
QA="$SDIR/qa.md"

cmd="${1:-status}"; shift || true

case "$cmd" in
  start)
    if [ -f "$META" ]; then
      echo "이미 진행 중인 세션이 있음:"
      cat "$META"
      echo "(이어서 진행하거나, close 후 새로 시작할 것)"
      exit 2
    fi
    mkdir -p "$SDIR"
    TUTOR=""; TOPIC=""; RECORD=1; RECARGS=()
    while [ $# -gt 0 ]; do
      case "$1" in
        --tutor) TUTOR="$2"; shift 2 ;;
        --topic) TOPIC="$2"; shift 2 ;;
        --no-record) RECORD=0; shift ;;
        --no-video) RECARGS+=(--no-video); shift ;;
        --no-menubar) RECARGS+=(--no-menubar); shift ;;
        *) shift ;;
      esac
    done
    NOW=$(date +%s)
    printf '{"started_at":%s,"started_hm":"%s","date":"%s","tutor":"%s","topic":"%s"}\n' \
      "$NOW" "$(date +%H:%M)" "$(date +%F)" "$TUTOR" "$TOPIC" > "$META"
    printf '# Q&A %s\n\n' "$(date +%F)" > "$QA"
    rm -f "$SDIR"/transcript.* "$SDIR/audio.wav"
    echo "세션 시작: $(date +%F) $(date +%H:%M)"
    echo "질문 로그: $QA"
    if [ "$RECORD" = "1" ]; then
      "$(dirname "$0")/record.sh" start "${RECARGS[@]+"${RECARGS[@]}"}" || echo "녹화 시작 실패 — 수동으로 녹화할 것" >&2
    fi
    ;;

  status)
    if [ ! -f "$META" ]; then echo "진행 중인 세션 없음"; exit 0; fi
    cat "$META"
    echo "질문 로그: $QA ($(grep -c '^## ' "$QA" 2>/dev/null || echo 0)건)"
    ;;

  end)
    if [ ! -f "$META" ]; then echo "진행 중인 세션 없음"; exit 1; fi
    START=$(sed -n 's/.*"started_at":\([0-9]*\).*/\1/p' "$META")
    cat "$META"
    echo "질문 로그: $QA"
    echo "--- 녹화 중지 ---"
    PREFIX=""
    if [ -f "$SDIR/record.prefix" ]; then PREFIX=$(cat "$SDIR/record.prefix"); fi
    "$(dirname "$0")/record.sh" stop || true
    echo "--- 전사 명령 ---"
    if [ -n "$PREFIX" ] && [ -f "$PREFIX-tutor.m4a" ]; then
      echo "$(dirname "$0")/transcribe.sh --prefix \"$PREFIX\""
    else
      echo "$(dirname "$0")/transcribe.sh --since $START"
    fi
    ;;

  close)
    if [ ! -f "$META" ]; then echo "진행 중인 세션 없음"; exit 0; fi
    ADIR="$SDIR/archive/$(date +%F_%H%M)"
    mkdir -p "$ADIR"
    mv "$META" "$ADIR/" 2>/dev/null || true
    mv "$QA" "$ADIR/" 2>/dev/null || true
    # 전사 결과도 함께 보관 — 남겨두면 다음 세션 전사와 섞인다
    for t in "$SDIR"/transcript.*; do [ -e "$t" ] && mv "$t" "$ADIR/"; done
    rm -f "$SDIR/audio.wav"
    echo "세션 종료. 원본 보관: $ADIR"
    ;;

  *)
    echo "사용법: session.sh {start|status|end|close}" >&2; exit 1 ;;
esac
