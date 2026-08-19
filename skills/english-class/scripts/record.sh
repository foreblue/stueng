#!/bin/bash
# 수업 녹화 시작/중지.
#   record.sh start [--no-video] [--fps N] [--vdevice N]
#   record.sh stop | status
#
# 오디오: audio-recorder(ScreenCaptureKit) 가 시스템 오디오(강사)와 마이크(나)를 각각 다른
#         파일에 담는다. 헤드셋을 써도 강사 목소리가 잡히고, 화자 구분이 확정된다.
# 영상:   ffmpeg 이 화면만 담는다(-an). 소리는 위에서 이미 받는다.
# macOS 기본 screencapture -v 는 중간에 멈추면 파일이 저장되지 않아 쓰지 않는다.
set -euo pipefail
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

HERE="$(cd "$(dirname "$0")" && pwd)"
SDIR="$HOME/mylogs/study/.session"
OUTDIR="$HOME/Movies/english-class"   # 볼트는 git 저장소라 큰 파일은 밖에 둔다
APIDF="$SDIR/record-audio.pid"
VPIDF="$SDIR/record-video.pid"
PREFIXF="$SDIR/record.prefix"

cmd="${1:-status}"; shift || true

alive() { [ -f "$1" ] && ps -p "$(cat "$1")" >/dev/null 2>&1; }

case "$cmd" in
  start)
    if alive "$APIDF"; then echo "이미 녹화 중: $(cat "$PREFIXF" 2>/dev/null)"; exit 2; fi
    [ -x "$HERE/audio-recorder" ] || { echo "audio-recorder 없음 — swiftc -O -parse-as-library -o $HERE/audio-recorder $HERE/AudioRecorder.swift" >&2; exit 1; }

    VIDEO=1; FPS=5; VDEV=4; ARGS=()
    while [ $# -gt 0 ]; do
      case "$1" in
        --no-video) VIDEO=0; shift ;;
        --no-menubar) ARGS+=(--no-menubar); shift ;;
        --fps) FPS="$2"; shift 2 ;;
        --vdevice) VDEV="$2"; shift 2 ;;
        *) shift ;;
      esac
    done

    mkdir -p "$SDIR" "$OUTDIR"
    PREFIX="$OUTDIR/english-class-$(date +%F_%H%M)"
    echo "$PREFIX" > "$PREFIXF"

    "$HERE/audio-recorder" "$PREFIX" "${ARGS[@]+"${ARGS[@]}"}" >"$SDIR/record-audio.log" 2>&1 &
    APID=$!
    # 첫 줄("recording")이 나올 때까지 기다린다 — 권한 거부는 여기서 드러난다
    for _ in $(seq 1 20); do
      grep -q recording "$SDIR/record-audio.log" 2>/dev/null && break
      ps -p $APID >/dev/null 2>&1 || break
      sleep 0.5
    done
    if ! ps -p $APID >/dev/null 2>&1 || ! grep -q recording "$SDIR/record-audio.log" 2>/dev/null; then
      echo "오디오 녹음 시작 실패:" >&2; cat "$SDIR/record-audio.log" >&2
      rm -f "$PREFIXF"; exit 1
    fi
    echo "$APID" > "$APIDF"
    echo "오디오 녹음 시작 (pid $APID) — 메뉴 막대의 파형 아이콘을 클릭하면 화자별 파형을 볼 수 있다"
    echo "  강사: $PREFIX-tutor.m4a"
    echo "  나  : $PREFIX-me.m4a"

    if [ "$VIDEO" = "1" ] && command -v ffmpeg >/dev/null; then
      ffmpeg -nostdin -y -loglevel error -f avfoundation -framerate "$FPS" -capture_cursor 1 \
        -i "$VDEV:none" -vf "scale=1600:-2" -an \
        -c:v h264_videotoolbox -b:v 1000k -pix_fmt yuv420p "$PREFIX.mov" \
        >"$SDIR/record-video.log" 2>&1 &
      VPID=$!
      sleep 2
      if ps -p $VPID >/dev/null 2>&1; then
        echo "$VPID" > "$VPIDF"; echo "화면 녹화 시작 (pid $VPID): $PREFIX.mov"
      else
        echo "화면 녹화는 실패 — 오디오만 계속 녹음한다:" >&2; cat "$SDIR/record-video.log" >&2
      fi
    fi
    ;;

  stop)
    [ -f "$PREFIXF" ] || { echo "녹화 중 아님"; exit 1; }
    PREFIX=$(cat "$PREFIXF")
    for f in "$APIDF" "$VPIDF"; do
      if alive "$f"; then kill -INT "$(cat "$f")" 2>/dev/null || true; fi
    done
    # 파일을 정상 마무리할 시간을 준다. 강제 종료하면 m4a/mov 가 깨진다.
    for _ in $(seq 1 30); do
      alive "$APIDF" || alive "$VPIDF" || break
      sleep 1
    done
    for f in "$APIDF" "$VPIDF"; do
      if alive "$f"; then echo "정상 종료 실패 — 파일이 손상됐을 수 있음" >&2; kill -9 "$(cat "$f")" 2>/dev/null || true; fi
    done
    rm -f "$APIDF" "$VPIDF" "$PREFIXF"

    echo "녹화 종료:"
    for f in "$PREFIX-tutor.m4a" "$PREFIX-me.m4a" "$PREFIX.mov"; do
      [ -f "$f" ] || continue
      DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f" 2>/dev/null | cut -d. -f1)
      VOL=$(ffmpeg -hide_banner -nostdin -i "$f" -af volumedetect -f null - 2>&1 | sed -n 's/.*mean_volume: //p')
      echo "  $(basename "$f")  ${DUR:+$((DUR/60))분 $((DUR%60))초}  $(du -h "$f"|cut -f1)${VOL:+  평균음량 $VOL}"
    done
    echo "프리픽스: $PREFIX"
    ;;

  status)
    if alive "$APIDF"; then echo "녹화 중: $(cat "$PREFIXF" 2>/dev/null)"; else echo "녹화 중 아님"; fi
    ;;

  *) echo "사용법: record.sh {start|stop|status}" >&2; exit 1 ;;
esac
