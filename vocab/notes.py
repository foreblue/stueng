"""수업 노트 파서 — 의존성 없이 마크다운 표만 읽는다.

`english-class` 스킬이 만든 `영어수업 YYYY-MM-DD.md` 에서 어휘를 뽑는다. 이 파일이
`collect` 에서 떨어져 나온 이유는 **수업이 다른 PC 에서 돌기 때문**이다. 노트를 쓰는
것도 그 PC 의 Claude 이고, 그 PC 는 어휘 저장소를 갖고 있지 않다. 파서가 sqlalchemy
를 끌고 오면 그쪽에서 쓸 수 없어 같은 규칙을 두 번 구현하게 된다.

그래서 여기서는 표준 라이브러리만 쓰고 평범한 dict 를 돌려준다. 맥은 `collect` 가
그것을 `Entry` 로 감싸 DB 에 넣고, 수업 PC 는 `remote` 가 그대로 `/ingest` 페이로드로
쓴다. 표를 읽는 규칙은 한 곳에만 있다.

읽는 표는 둘이다.

- `새 단어`/`표현` 이 든 제목 아래: `표현 | 뜻 | 예문`
- `교정` 이 든 제목 아래: `내가 한 말 | 자연스러운 표현 | 왜`
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

#: 같은 날 두 번째 수업은 `(2)` 가 붙는다.
CLASS_NOTE_RE = re.compile(r"^영어수업 (\d{4}-\d{2}-\d{2})(?: \((\d+)\))?\.md$")

#: 이스케이프되지 않은 파이프에서만 자른다.
CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")

SOURCE_CLASS = "class"
SOURCE_CORRECTION = "correction"  # 영어수업에서 내가 틀려 교정받은 표현


#: 노트는 사람이 읽는 마크다운이다. 고친 부분을 `**이렇게**` 강조해 두고, 볼트가
#: Obsidian 이라 다른 노트를 `[[위키링크]]` 로 건다. 그 표시가 어휘로 넘어오면 카드
#: 앞면에 별표가 그대로 뜨고, `headword` 에도 섞여 같은 표현을 다른 어휘로 세게 된다.
#: 표시는 벗기고 사람이 읽는 글자만 남긴다.
#:
#: 한 구문만 막으면 다음 구문에서 같은 버그가 난다. 그래서 노트에 나올 만한 것을
#: 함께 다룬다 — 지금 데이터에 있는 것은 강조뿐이지만, 입력이 마크다운인 이상 나머지도
#: 언제든 들어온다.
WIKILINK_RE = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]")  # [[대상|보이는 글자]]
LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")  # [보이는 글자](주소)
EMPHASIS_RE = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`", re.DOTALL)

#: 취소선은 안의 글자를 살리면 안 된다. "이건 아니다" 라는 표시라, 교정 표에서
#: `~~틀린 표현~~ 맞는 표현` 이 둘 다 남으면 카드가 틀린 것을 함께 가르친다.
STRIKE_RE = re.compile(r"~~.+?~~", re.DOTALL)


def strip_markup(text: str) -> str:
    text = WIKILINK_RE.sub(r"\1", text)
    text = STRIKE_RE.sub("", text)
    text = LINK_RE.sub(r"\1", text)
    previous = None
    # 중첩(`**a *b* c**`)을 위해 더 벗길 것이 없을 때까지 돈다.
    while previous != text:
        previous = text
        text = EMPHASIS_RE.sub(lambda m: next(g for g in m.groups() if g is not None), text)
    # 짝이 맞지 않아 남은 표시는 그냥 버린다. 남겨 두면 카드에 그대로 뜬다.
    return text.replace("**", "").replace("~~", "").replace("`", "")


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", strip_markup(str(value or "")).strip())


def is_header(cells: list[str], needles: tuple[str, ...]) -> bool:
    joined = " ".join(cells)
    return any(n in joined for n in needles)


def markdown_tables(body: str) -> dict[str, list[list[str]]]:
    """`## 제목` 아래에 붙은 파이프 표를 제목별로 모은다.

    구분선(`| --- |`)과 빈 셀만 있는 행은 버린다. 템플릿이 빈 표를 남기기 때문이다.
    """
    tables: dict[str, list[list[str]]] = {}
    heading = ""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            continue
        if not stripped.startswith("|"):
            continue
        # 셀 안의 파이프는 `\|` 로 이스케이프된다. 그냥 split 하면 셀이 쪼개져
        # 뜻의 뒷부분이 통째로 사라진다.
        cells = [c.strip().replace("\\|", "|") for c in CELL_SPLIT_RE.split(stripped.strip("|"))]
        if not any(cells):
            continue
        if all(set(c) <= {"-", ":", " "} for c in cells if c):
            continue
        tables.setdefault(heading, []).append(cells)
    return tables


def note_date(name: str) -> dt.date | None:
    """파일 이름에서 수업 날짜. 규칙에 맞지 않으면 None."""
    match = CLASS_NOTE_RE.match(name)
    return dt.date.fromisoformat(match.group(1)) if match else None


def parse(body: str, occurred_on: dt.date) -> list[dict]:
    """노트 본문 → 어휘 항목. `kind` 는 넣지 않는다 — 표기 형태가 정할 몫이다."""
    tutor = topic = ""
    for line in body.splitlines()[:20]:
        if line.startswith("- 튜터:"):
            tutor = clean(line.split(":", 1)[1])
        elif line.startswith("- 주제:"):
            topic = clean(line.split(":", 1)[1])

    title = " · ".join(x for x in [f"영어수업 {occurred_on:%Y-%m-%d}", tutor, topic] if x)
    entries: list[dict] = []

    for heading, rows in markdown_tables(body).items():
        if ("새 단어" in heading or "표현" in heading) and "교정" not in heading:
            # | 표현 | 뜻 | 예문 |
            for cells in rows[1:] if is_header(rows[0], ("표현", "뜻")) else rows:
                if len(cells) < 2:
                    continue
                display, meaning = clean(cells[0]), clean(cells[1])
                if not display or not meaning:
                    continue
                entries.append({
                    "display": display,
                    "meaning_kr": meaning,
                    "sentence": clean(cells[2]) if len(cells) > 2 else None,
                    "source_kind": SOURCE_CLASS,
                    "source_title": title,
                    "occurred_on": occurred_on,
                })

        elif "교정" in heading:
            # | 내가 한 말 | 자연스러운 표현 | 왜 |
            for cells in rows[1:] if is_header(rows[0], ("내가", "자연")) else rows:
                if len(cells) < 2:
                    continue
                said, natural = clean(cells[0]), clean(cells[1])
                why = clean(cells[2]) if len(cells) > 2 else ""
                if not natural:
                    continue
                entries.append({
                    "display": natural,
                    "meaning_kr": why or f"'{said}' 대신 쓰는 자연스러운 표현",
                    "usage_note": f"내가 한 말: {said}" if said else None,
                    "sentence": natural,
                    "source_kind": SOURCE_CORRECTION,
                    "source_title": title,
                    "occurred_on": occurred_on,
                })

    return entries


def parse_file(path: Path | str) -> list[dict]:
    """파일 이름에서 날짜를 읽어 파싱한다. 이름 규칙에 맞지 않으면 빈 목록."""
    path = Path(path)
    occurred_on = note_date(path.name)
    if occurred_on is None:
        return []
    return parse(path.read_text(encoding="utf-8"), occurred_on)
