#!/usr/bin/env python3
"""강사 트랙과 내 트랙의 전사를 시각 순으로 합쳐 대화록을 만든다.

트랙이 나뉘어 있으므로 화자는 추정이 아니라 확정이다.

    merge_transcript.py <tutor.json> <me.json> <출력.md> [sync.json]

sync.json 이 있으면 트랙별 시작 지연(offset)을 더해 두 트랙의 시각을 같은 기준에
올린다. 각 m4a 의 t=0 은 그 트랙의 첫 샘플이라, 보정하지 않으면 늦게 시작한 트랙의
발화가 실제보다 이른 시각에 찍힌다.
"""
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

# 스피커로 수업을 들으면 마이크가 강사 목소리까지 주워담아 양쪽 트랙에 같은 말이 남는다.
# 이 시간 창 안에서 텍스트가 겹치면 내 트랙 쪽을 지운다(시스템 오디오가 원본이다).
ECHO_WINDOW_SEC = 4.0
ECHO_SIMILARITY = 0.75


def norm(text):
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def drop_echo(tutor_segs, me_segs):
    """내 트랙에서 강사 목소리가 새어든 세그먼트를 걷어낸다."""
    kept, dropped = [], 0
    for start, speaker, text in me_segs:
        mine = norm(text)
        if not mine:
            continue
        echo = False
        for t_start, _, t_text in tutor_segs:
            if abs(t_start - start) > ECHO_WINDOW_SEC:
                continue
            theirs = norm(t_text)
            if not theirs:
                continue
            if theirs in mine or mine in theirs:
                echo = True
                break
            if SequenceMatcher(None, mine, theirs).ratio() >= ECHO_SIMILARITY:
                echo = True
                break
        if echo:
            dropped += 1
        else:
            kept.append((start, speaker, text))
    return kept, dropped


def load(path, speaker, offset=0.0):
    p = Path(path)
    if not p.exists():
        return []
    data = json.loads(p.read_text())
    out = []
    for seg in data.get("segments", []):
        text = seg.get("text", "").strip()
        if not text:
            continue
        out.append((seg.get("start", 0.0) + offset, speaker, text))
    return out


def load_sync(path):
    """녹음기가 남긴 트랙별 시작 지연·유실 정보. 없으면 보정 없이 진행한다."""
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return {}


def hhmmss(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def main():
    if not 4 <= len(sys.argv) <= 5:
        print(__doc__, file=sys.stderr)
        return 2

    tutor_json, me_json, out_path = sys.argv[1:4]
    sync = load_sync(sys.argv[4] if len(sys.argv) > 4 else None)
    offsets = {k: float(sync.get(k, {}).get("offset", 0.0) or 0.0) for k in ("tutor", "me")}
    tutor_segs = load(tutor_json, "Tutor", offsets["tutor"])
    me_segs = load(me_json, "Me", offsets["me"])
    me_raw = len(me_segs)
    me_segs, echoes = drop_echo(tutor_segs, me_segs)

    segs = tutor_segs + me_segs
    if not segs:
        print("전사 세그먼트가 없다", file=sys.stderr)
        return 1
    segs.sort(key=lambda s: s[0])

    lines = ["# 수업 전사"]
    prev_speaker = None
    for start, speaker, text in segs:
        # 같은 화자가 이어 말하면 한 덩어리로 묶는다
        if speaker == prev_speaker:
            lines[-1] += " " + text
        else:
            lines.append(f"**{speaker}** `{hhmmss(start)}` {text}")
            prev_speaker = speaker

    Path(out_path).write_text("\n\n".join(lines) + "\n")

    n_tutor = sum(1 for _, s, _ in segs if s == "Tutor")
    n_me = len(segs) - n_tutor
    print(f"합침: Tutor {n_tutor}개 / Me {n_me}개 세그먼트 → {out_path}")
    if echoes:
        print(f"내 트랙에서 강사 목소리가 새어든 {echoes}개 세그먼트를 제거했다 "
              f"(스피커로 들으면 생긴다 — 헤드셋을 쓰면 깨끗해진다)")

    skew = abs(offsets["tutor"] - offsets["me"])
    if skew >= 0.5:
        late = "내" if offsets["me"] > offsets["tutor"] else "강사"
        print(f"두 트랙의 시작이 {skew:.1f}초 어긋나 있어 보정했다 ({late} 트랙이 늦게 시작)")
    for key, name in (("tutor", "강사"), ("me", "내")):
        lost = int(sync.get(key, {}).get("dropped", 0) or 0)
        if lost:
            print(f"경고: {name} 트랙 녹음 중 샘플 {lost}개가 유실됐다 — "
                  f"그 지점 이후 시각이 밀렸을 수 있다", file=sys.stderr)
    if not sync:
        print("참고: sync.json 이 없어 트랙 간 시작 지연을 보정하지 못했다 "
              "(구버전 녹음기로 만든 파일)")
    if n_tutor == 0:
        print("경고: 강사 트랙이 비었다 — 시스템 오디오가 녹음되지 않았다", file=sys.stderr)
    if n_me == 0:
        if me_raw == 0:
            print("경고: 내 트랙이 비었다 — 마이크가 녹음되지 않았다", file=sys.stderr)
        else:
            print("경고: 내 트랙이 전부 강사 목소리의 반향이었다 — 내 발화가 잡히지 않았다",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
