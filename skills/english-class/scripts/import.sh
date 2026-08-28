#!/bin/bash
# 다른 장비에서 녹음한 수업 파일을 맥의 전사 파이프라인에 꽂는다.
#   import.sh [--file PATH] [--swap] [--date YYYY-MM-DD_HHMM]
#
# 수업이 다른 PC 에서 돌아가면 ScreenCaptureKit 으로는 못 잡는다. 대신 그 PC 가 만든
# 녹화(오디오 트랙 2개: 데스크톱 소리=강사, 마이크=나)를 받아 이 저장소의 규약대로
# `<프리픽스>-tutor.m4a` / `<프리픽스>-me.m4a` 로 쪼갠다. 그 뒤는 record.sh 로 녹음한
# 것과 완전히 같다 — transcribe.sh --prefix 가 그대로 먹는다.
#
# 한 컨테이너 안의 두 트랙은 같은 클럭에서 나왔으므로 어긋남이 없다. sync.json 을
# offset 0 으로 써 두어 merge_transcript.py 가 "보정 못 했다" 안내를 띄우지 않게 한다.
set -euo pipefail
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

HERE="$(cd "$(dirname "$0")" && pwd)"
SDIR="$HOME/mylogs/study/.session"
OUTDIR="$HOME/Movies/english-class"
INBOX="$OUTDIR/inbox"

FILE=""; SWAP=0; STAMP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --file) FILE="$2"; shift 2 ;;
    --swap) SWAP=1; shift ;;
    --date) STAMP="$2"; shift 2 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 1 ;;
  esac
done

command -v ffmpeg >/dev/null || { echo "ffmpeg 없음 — brew install ffmpeg" >&2; exit 1; }

# 1) 대상 파일 — 지정이 없으면 inbox 에서 가장 최근 것
mkdir -p "$INBOX" "$SDIR"
if [ -z "$FILE" ]; then
  FILE=$(find "$INBOX" -maxdepth 1 -type f \
    \( -name '*.mkv' -o -name '*.mp4' -o -name '*.mov' -o -name '*.m4a' -o -name '*.wav' -o -name '*.flac' \) \
    -print0 2>/dev/null | xargs -0 ls -t 2>/dev/null | head -1 || true)
  [ -n "$FILE" ] || { echo "받은 녹화가 없다: $INBOX" >&2; exit 1; }
  echo "가장 최근 파일을 쓴다: $FILE"
fi
[ -f "$FILE" ] || { echo "파일 없음: $FILE" >&2; exit 1; }

# 2) 오디오 트랙 수 확인
NTRACK=$(ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$FILE" | wc -l | tr -d ' ')
[ "$NTRACK" -ge 1 ] || { echo "오디오 트랙이 없다: $FILE" >&2; exit 1; }

STAMP="${STAMP:-$(date -r "$FILE" +%F_%H%M)}"
PREFIX="$OUTDIR/english-class-$STAMP"

if [ "$NTRACK" = "1" ]; then
  # 화자 구분 불가 — 통짜 전사로 넘긴다. 노트에서 문맥으로 가르는 수밖에 없다.
  echo "경고: 오디오 트랙이 하나뿐이라 강사/나를 가를 수 없다." >&2
  echo "      녹음 쪽에서 데스크톱 소리와 마이크를 별도 트랙으로 담으면 화자가 확정된다." >&2
  echo "--- 전사 명령 ---"
  echo "$HERE/transcribe.sh --file \"$FILE\""
  exit 0
fi

if [ "$NTRACK" -gt 2 ]; then
  echo "참고: 오디오 트랙이 $NTRACK 개다. 앞의 두 개만 쓴다." >&2
fi

# 3) 두 트랙을 규약대로 쪼갠다
if [ "$SWAP" = "1" ]; then T_IDX=1; M_IDX=0; else T_IDX=0; M_IDX=1; fi
for pair in "tutor:$T_IDX" "me:$M_IDX"; do
  role="${pair%%:*}"; idx="${pair##*:}"
  ffmpeg -y -v error -i "$FILE" -map "0:a:$idx" -vn -c:a aac -b:a 96k "$PREFIX-$role.m4a"
done

printf '{"tutor":{"offset":0,"dropped":0},"me":{"offset":0,"dropped":0}}\n' > "$PREFIX-sync.json"
echo "$PREFIX" > "$SDIR/record.prefix"

dur() { ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" | cut -d. -f1; }
echo "가져오기 완료 (원본: $(basename "$FILE"))"
echo "  강사: $PREFIX-tutor.m4a ($(dur "$PREFIX-tutor.m4a")초)"
echo "  나  : $PREFIX-me.m4a ($(dur "$PREFIX-me.m4a")초)"
echo "강사/나가 뒤바뀌었으면 --swap 을 붙여 다시 실행할 것."
echo "--- 전사 명령 ---"
echo "$HERE/transcribe.sh --prefix \"$PREFIX\""
