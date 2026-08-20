#!/bin/bash
# 수업 녹화(동영상/오디오)에서 음성을 전사한다.
#   transcribe.sh [--file PATH] [--since EPOCH] [--dir DIR] [--model NAME] [--lang en]
# 결과: ~/mylogs/study/.session/transcript.{txt,srt,json}
set -euo pipefail

STUDY="$HOME/mylogs/study"
SDIR="$STUDY/.session"
FILE=""; DIR=""; SINCE=""; PREFIX=""
MODEL="mlx-community/whisper-large-v3-turbo"
LANG="en"

while [ $# -gt 0 ]; do
  case "$1" in
    --file)   FILE="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --dir)   DIR="$2"; shift 2 ;;
    --since) SINCE="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --lang)  LANG="$2"; shift 2 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 1 ;;
  esac
done

command -v ffmpeg >/dev/null || { echo "ffmpeg 없음 — brew install ffmpeg" >&2; exit 1; }
command -v mlx_whisper >/dev/null || { echo "mlx_whisper 없음 — uv tool install mlx-whisper" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"

# 0) 이중 트랙(강사/나)이면 각각 전사한 뒤 시각 순으로 합친다
if [ -n "$PREFIX" ]; then
  mkdir -p "$SDIR"
  ANY=0
  for role in tutor me; do
    SRC="$PREFIX-$role.m4a"
    [ -f "$SRC" ] || { echo "없음: $SRC (건너뜀)"; continue; }
    echo "전사 중 ($role): $(basename "$SRC")"
    mlx_whisper "$SRC" --model "$MODEL" --language "$LANG" \
      --condition-on-previous-text False \
      --output-dir "$SDIR" --output-name "transcript-$role" \
      --output-format json --verbose False
    ANY=1
  done
  [ "$ANY" = "1" ] || { echo "전사할 트랙이 없다: $PREFIX-{tutor,me}.m4a" >&2; exit 1; }
  python3 "$HERE/merge_transcript.py" \
    "$SDIR/transcript-tutor.json" "$SDIR/transcript-me.json" "$SDIR/transcript.md" \
    "$PREFIX-sync.json"
  echo "완료: $SDIR/transcript.md"
  exit 0
fi

# 1) 대상 파일 결정
if [ -z "$FILE" ]; then
  if [ -z "$DIR" ]; then
    DIR="$(defaults read com.apple.screencapture location 2>/dev/null || true)"
    DIR="${DIR/#\~/$HOME}"
    [ -d "$DIR" ] || DIR="$HOME/Desktop"
  fi
  CUTOFF="${SINCE:-0}"
  FILE=$(find "$DIR" -maxdepth 1 -type f \
    \( -iname '*.mov' -o -iname '*.mp4' -o -iname '*.m4a' -o -iname '*.mp3' -o -iname '*.wav' \) \
    -print0 2>/dev/null \
    | xargs -0 -r stat -f '%m%t%N' 2>/dev/null \
    | awk -F'\t' -v c="$CUTOFF" '$1 >= c' | sort -n | cut -f2- | tail -1)
  [ -n "$FILE" ] || { echo "녹화 파일 없음 (경로: $DIR${SINCE:+, 세션 시작 이후})"; exit 0; }
fi
[ -f "$FILE" ] || { echo "파일 없음: $FILE" >&2; exit 1; }

mkdir -p "$SDIR"
echo "대상: $FILE"

# 오디오 트랙 확인 — 없으면 전사할 게 없다
if ! ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$FILE" | grep -q .; then
  echo "오디오 트랙 없음 — 음성이 녹음되지 않은 파일이다." >&2; exit 1
fi

DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$FILE" 2>/dev/null | cut -d. -f1)
echo "길이: $((DUR/60))분 $((DUR%60))초"

# 2) 16kHz mono wav 추출 (whisper 입력 규격)
WAV="$SDIR/audio.wav"
echo "오디오 추출 중..."
ffmpeg -nostdin -y -loglevel error -i "$FILE" -vn -ac 1 -ar 16000 -c:a pcm_s16le "$WAV"

# 3) 전사
echo "전사 중... (모델: $MODEL)"
mlx_whisper "$WAV" --model "$MODEL" --language "$LANG" \
  --condition-on-previous-text False \
  --output-dir "$SDIR" --output-name transcript \
  --output-format all --verbose False

echo "완료:"
for f in "$SDIR"/transcript.*; do echo "  $f"; done
rm -f "$WAV"
