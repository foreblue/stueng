"""어휘 저장소 스키마.

소유권이 두 갈래로 나뉜다.

- **Word / Occurrence — 로컬이 원본.** `data/*.json` 과 영어수업 노트에서 언제든
  다시 만들 수 있다. 서버에는 복제본이 올라간다.
- **Card / ReviewLog — 서버가 원본.** 어디에도 원본이 없는 유일한 데이터다.
  특히 ReviewLog 는 나중에 FSRS 파라미터를 개인 데이터로 재최적화할 때 쓰는
  재료이므로 **절대 삭제하지 않는다.** 소급해서 만들 수 없다.

같은 스키마를 로컬 SQLite 와 서버 Postgres 양쪽에 쓴다.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from . import banding

KIND_WORD = "word"
KIND_EXPRESSION = "expression"


def kind_for(display: str) -> str:
    """표기 형태로 종류를 정한다.

    예전에는 LLM 이 `vocabulary` 에 넣었는지 `expressions` 에 넣었는지로 정했다.
    그러면 'missing in action' 이 word 가 되고 'backfire' 가 expression 이 된다 —
    실제로 676개 중 58개가 어긋나 있었다.

    종류가 하는 일이 둘이라 이게 그냥 라벨 문제가 아니다. 4지선다의 오답 보기를
    같은 종류에서 뽑으므로 세 단어짜리와 한 단어짜리가 섞이고, 산출 단계의 질문도
    "이 뜻의 단어는?" 과 "이 뜻의 표현은?" 으로 갈린다. 둘 다 형태를 따라야 맞다.
    """
    return KIND_EXPRESSION if banding.is_phrase(display) else KIND_WORD

SOURCE_UPFIRST = "upfirst"
SOURCE_PLANETMONEY = "planetmoney"
SOURCE_CLASS = "class"
SOURCE_CORRECTION = "correction"  # 영어수업에서 내가 틀려 교정받은 표현

#: 출제 형식. 카드가 안정될수록 인출 노력이 큰 쪽으로 승급한다.
STAGE_RECOGNITION = "recognition"  # 영 -> 한 4지선다
STAGE_CLOZE = "cloze"  # 원문 예문 빈칸 채우기
STAGE_PRODUCTION = "production"  # 한 -> 영 산출

STAGES = (STAGE_RECOGNITION, STAGE_CLOZE, STAGE_PRODUCTION)


class UtcDateTime(TypeDecorator):
    """항상 timezone-aware UTC 로 주고받는 시각 컬럼.

    SQLite 는 tzinfo 를 조용히 버리고 Postgres 는 유지한다. 그대로 두면 로컬에서는
    naive, 서버에서는 aware 가 나와서 `now - card.last_review` 같은 계산이 한쪽에서만
    터진다. 실제로 재현하기 어려운 시점에 터지는 종류의 버그다.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)


def sentence_key(text: str) -> str:
    """예문 중복 판정용 키. 공백·대소문자 차이는 같은 문장으로 본다."""
    normalized = " ".join((text or "").split()).lower()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


class Base(DeclarativeBase):
    pass


class Word(Base):
    """어휘 1급 레코드. 같은 단어가 여러 에피소드에 나와도 한 줄이다."""

    __tablename__ = "word"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: 정규화된 표제어 (소문자, 공백 정리). 중복 판정 기준.
    headword: Mapped[str] = mapped_column(String(160), index=True)
    #: 화면에 보여줄 원래 표기.
    display: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16))

    meaning_kr: Mapped[str] = mapped_column(Text)
    meaning_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    zipf: Mapped[float | None] = mapped_column(Float, nullable=True)
    band: Mapped[str] = mapped_column(String(16), index=True)

    #: 사용자가 "이미 안다"고 표시하면 큐에서 빠진다. 밴드 판정을 사람이 덮어쓰는 장치.
    known: Mapped[bool] = mapped_column(Boolean, default=False)

    #: 반복해서 실패하는 카드에만 붙는 기억술. 니모닉은 초기 회상에는 강하지만 시간이
    #: 지나면 이점이 감쇠하므로, 기본 장치가 아니라 막힌 카드의 탈출구로만 쓴다.
    mnemonic: Mapped[str | None] = mapped_column(Text, nullable=True)

    first_seen: Mapped[dt.date] = mapped_column(Date)
    updated_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, default=lambda: dt.datetime.now(dt.UTC)
    )

    occurrences: Mapped[list["Occurrence"]] = relationship(
        back_populates="word", cascade="all, delete-orphan", order_by="Occurrence.occurred_on"
    )
    card: Mapped["Card | None"] = relationship(back_populates="word", uselist=False)

    __table_args__ = (UniqueConstraint("headword", "kind", name="uq_word_headword_kind"),)

    def __repr__(self) -> str:
        return f"<Word {self.display!r} {self.kind} {self.band}>"


class Occurrence(Base):
    """이 어휘가 실제로 등장한 지점.

    한 단어에 여러 개가 쌓이는 것이 이 프로젝트의 핵심이다. 문맥 다양성 연구에 따르면
    서로 다른 문맥에서 만난 단어라야 새 문맥으로 일반화된다 — 그래서 출제할 때마다
    다른 예문을 돌려 쓴다.
    """

    __tablename__ = "occurrence"

    id: Mapped[int] = mapped_column(primary_key=True)
    word_id: Mapped[int] = mapped_column(ForeignKey("word.id", ondelete="CASCADE"), index=True)

    #: 전사문에서 그대로 따온 문장. 지어낸 예문이 아니다.
    sentence: Mapped[str] = mapped_column(Text)
    sentence_hash: Mapped[str] = mapped_column(String(40))
    translation_kr: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 그 출처에서 붙었던 뜻·설명. 출처마다 다를 수 있어 통합하지 않고 남긴다.
    definition_kr: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_kind: Mapped[str] = mapped_column(String(24), index=True)
    source_title: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_on: Mapped[dt.date] = mapped_column(Date)

    word: Mapped[Word] = relationship(back_populates="occurrences")

    __table_args__ = (
        UniqueConstraint("word_id", "sentence_hash", name="uq_occurrence_word_sentence"),
    )

    def __repr__(self) -> str:
        return f"<Occurrence {self.source_kind} {self.occurred_on} {self.sentence[:40]!r}>"


class Card(Base):
    """학습 단위. 어휘 하나에 카드 한 장이고, 출제 형식은 stage 로 승급한다.

    형식별로 카드를 나누지 않는 이유: 형식은 같은 지식에 대한 인출 난이도 조절이지
    별개의 항목이 아니다. 나누면 같은 단어를 하루에 세 번 만나게 된다.

    필드 이름은 FSRS 의 카드 상태를 그대로 따른다.
    """

    __tablename__ = "card"

    id: Mapped[int] = mapped_column(primary_key=True)
    word_id: Mapped[int] = mapped_column(
        ForeignKey("word.id", ondelete="CASCADE"), unique=True, index=True
    )

    stage: Mapped[str] = mapped_column(String(16), default=STAGE_RECOGNITION)

    #: FSRS State — 1=Learning 2=Review 3=Relearning. 새 카드는 Learning 에서 시작한다.
    state: Mapped[int] = mapped_column(Integer, default=1)
    due: Mapped[dt.datetime] = mapped_column(UtcDateTime, index=True)
    stability: Mapped[float | None] = mapped_column(Float, nullable=True)
    difficulty: Mapped[float | None] = mapped_column(Float, nullable=True)
    step: Mapped[int | None] = mapped_column(Integer, nullable=True)  # learning step index

    reps: Mapped[int] = mapped_column(Integer, default=0)
    lapses: Mapped[int] = mapped_column(Integer, default=0)
    last_review: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)

    #: 반복해서 실패하는 카드. 니모닉 제안 같은 탈출구를 여기에만 건다.
    leech: Mapped[bool] = mapped_column(Boolean, default=False)
    suspended: Mapped[bool] = mapped_column(Boolean, default=False)

    #: 마지막으로 쓴 예문. 다음 출제 때 다른 것을 고르기 위한 커서.
    last_occurrence_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, default=lambda: dt.datetime.now(dt.UTC)
    )

    word: Mapped[Word] = relationship(back_populates="card")
    reviews: Mapped[list["ReviewLog"]] = relationship(
        back_populates="card", order_by="ReviewLog.reviewed_at"
    )

    __table_args__ = (Index("ix_card_due_suspended", "due", "suspended"),)

    def __repr__(self) -> str:
        return f"<Card word={self.word_id} {self.stage} due={self.due:%Y-%m-%d}>"


class ReviewLog(Base):
    """복습 1회의 기록. **삭제 금지.**

    FSRS 가 요구하는 필드에 더해 객관 지표(정오답, 반응 시간, 실제 출제 형식,
    사용한 예문)를 함께 남긴다. 학습자의 체감은 실제 효과와 어긋나는 것으로 알려져
    있어(Karpicke & Roediger 2008), 자기평가 rating 만으로는 나중에 보정할 수 없다.
    """

    __tablename__ = "review_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("card.id", ondelete="CASCADE"), index=True)

    #: FSRS rating. 1=Again 2=Hard 3=Good 4=Easy
    rating: Mapped[int] = mapped_column(Integer)
    state: Mapped[int] = mapped_column(Integer)
    reviewed_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, index=True)

    #: 복습 시점의 카드 상태 스냅샷 (FSRS 재최적화 입력)
    due: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    stability: Mapped[float | None] = mapped_column(Float, nullable=True)
    difficulty: Mapped[float | None] = mapped_column(Float, nullable=True)
    elapsed_days: Mapped[int] = mapped_column(Integer, default=0)
    last_elapsed_days: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_days: Mapped[int] = mapped_column(Integer, default=0)

    #: 객관 지표
    correct: Mapped[bool] = mapped_column(Boolean)
    response_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stage: Mapped[str] = mapped_column(String(16))
    occurrence_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 같은 세션 안에서 다시 돌린 재인출인지 (successive relearning)
    in_session_retry: Mapped[bool] = mapped_column(Boolean, default=False)

    card: Mapped[Card] = relationship(back_populates="reviews")

    def __repr__(self) -> str:
        return f"<ReviewLog card={self.card_id} r={self.rating} {self.reviewed_at:%Y-%m-%d}>"


class Composition(Base):
    """주 1회 작문 과제.

    Involvement Load Hypothesis (Hulstijn & Laufer 2001): 단어를 다루는 과제가 필요·탐색·
    평가를 많이 요구할수록 파지가 좋고, 작문이 읽기나 빈칸 채우기보다 앞섰다. 다만 매일
    시키면 부담이 커서 이탈하므로 주 1회로 둔다.

    첨삭은 서버가 하지 않는다. LLM 은 로컬 프록시에만 있으므로, 제출된 글은 여기 쌓여
    있다가 로컬 워커(`python -m vocab.tutor`)가 가져가 첨삭하고 돌려놓는다.
    """

    __tablename__ = "composition"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: 그 주의 월요일. 한 주에 하나만 만든다.
    week_start: Mapped[dt.date] = mapped_column(Date, unique=True, index=True)

    #: 과제에 쓸 어휘. 표시용 문자열과 id 를 함께 담는다 — 어휘가 지워져도 과제 기록은 남는다.
    words: Mapped[str] = mapped_column(Text)

    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        UtcDateTime, default=lambda: dt.datetime.now(dt.UTC)
    )
    submitted_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)
    feedback_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)

    @property
    def word_list(self) -> list[dict]:
        return json.loads(self.words)

    @property
    def awaiting_feedback(self) -> bool:
        return self.submitted_at is not None and self.feedback is None

    def __repr__(self) -> str:
        return f"<Composition {self.week_start} {'제출' if self.text else '미제출'}>"
