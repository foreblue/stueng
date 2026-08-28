"""복습 웹앱 (FastAPI + Jinja).

화면은 세 개면 충분하다 — 오늘 할 것, 문제 한 장, 어휘 목록. 카드를 그냥 보여 주는
화면은 만들지 않는다. 모든 노출이 문제여야 한다는 것이 이 서비스의 전제다.

동적 갱신은 문제 영역 하나뿐이라 프론트 프레임워크를 쓰지 않는다. HTMX 를 CDN 에서
받으면 외부 의존이 생기고 vendoring 은 50KB 짜리 산출물을 리포에 넣는 일인데,
같은 일을 하는 손으로 쓴 자바스크립트가 30줄이다.

문제와 채점 사이의 상태는 서버에 저장하지 않고 서명된 토큰으로 왕복시킨다. 세션
테이블 없이도 위조를 막을 수 있고, 문제를 띄운 시각이 토큰 안에 있으므로 반응 시간을
클라이언트 자바스크립트 없이 서버에서 잰다.
"""

from __future__ import annotations

import datetime as dt
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import banding, collect, compose, study
from ..db import create_all, make_engine, session_factory
from ..models import Card, Composition, Occurrence, ReviewLog, Word
from . import security

BASE_DIR = Path(__file__).resolve().parent

#: 로그인 없이 열리는 경로. 나머지는 전부 막는다.
PUBLIC_PATHS = {"/login", "/healthz", "/manifest.webmanifest", "/sw.js", "/ingest",
                "/api/progress", "/api/export", "/api/tasks",
                "/api/handled"}

engine = make_engine()
Session_ = session_factory(engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_all(engine)
    security.secret_key()  # 프로덕션이면 여기서 설정 누락을 잡는다
    yield


app = FastAPI(title="stueng vocab", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _until(when: dt.datetime | None) -> str:
    """지금부터 얼마 뒤인지. 분 단위 학습 카드와 몇 달짜리 간격을 같은 자리에 쓴다."""
    if when is None:
        return "—"
    seconds = (when - study.now_utc()).total_seconds()
    if seconds <= 0:
        return "지금"
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))}분"
    if seconds < 86400:
        return f"{round(seconds / 3600)}시간"
    days = round(seconds / 86400)
    if days < 60:
        return f"{days}일"
    return f"{days // 30}개월"


def _localtime(when: dt.datetime | None) -> str:
    return when.astimezone(study.timezone()).strftime("%m-%d %H:%M") if when else "—"


def _external_url(url: str | None) -> str | None:
    """링크로 걸어도 되는 주소인가.

    source_url 은 NPR 피드나 /ingest 를 통해 들어오므로 우리가 쓴 값이 아니다.
    속성 이스케이프는 `javascript:` 를 막지 못하기 때문에 스킴을 직접 본다.
    지금 알려진 공격 경로는 없지만, 판정이 한 줄이고 신뢰 경계 밖의 값이다.
    """
    if not url:
        return None
    return url if url.lower().startswith(("http://", "https://")) else None


templates.env.filters["until"] = _until
templates.env.filters["localtime"] = _localtime
templates.env.filters["external_url"] = _external_url


def db() -> Session:
    with Session_() as session:
        yield session


DB = Annotated[Session, Depends(db)]


# --------------------------------------------------------------------------
# 인증
# --------------------------------------------------------------------------


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if security.is_logged_in(request):
        return await call_next(request)
    if request.headers.get("x-partial"):
        # 조각 요청에 로그인 페이지를 밀어 넣으면 화면이 깨진다. 통째로 새로고침시킨다.
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"X-Redirect": "/login"})
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if security.is_logged_in(request):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, password: Annotated[str, Form()] = ""):
    if not security.check_password(password):
        return templates.TemplateResponse(
            request, "login.html", {"error": "비밀번호가 맞지 않습니다."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        security.SESSION_COOKIE,
        security.issue_session(),
        max_age=security.SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=os.environ.get("VOCAB_ENV") == "production",
    )
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(security.SESSION_COOKIE)
    return response


# --------------------------------------------------------------------------
# 홈
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def home(request: Request, session: DB):
    return templates.TemplateResponse(
        request, "home.html", {"progress": study.progress(session)}
    )


# --------------------------------------------------------------------------
# 복습
# --------------------------------------------------------------------------

QUESTION_SALT = "question"


def _issue(question: study.Question, now: dt.datetime) -> str:
    return security.sign(
        {
            "card_id": question.card_id,
            "stage": question.stage,
            "answer": question.answer,
            "occurrence_id": question.occurrence_id,
            "choices": question.choices,
            "shown_at": now.isoformat(),
        },
        salt=QUESTION_SALT,
    )


def _restore(session: Session, token: str) -> tuple[study.Question, dt.datetime]:
    """토큰과 DB 로 문제를 되살린다.

    정답과 보기는 토큰에 서명된 것을 그대로 쓴다. 다시 만들면 보기 순서가 달라지고,
    무엇보다 예문 순환 때문에 다른 문장이 뽑혀 채점 기준이 흔들린다.
    """
    data = security.unsign(token, salt=QUESTION_SALT, max_age=security.QUESTION_MAX_AGE)
    card = session.get(Card, data["card_id"])
    if card is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "카드를 찾을 수 없습니다.")

    occurrence = session.get(Occurrence, data["occurrence_id"]) if data["occurrence_id"] else None
    word = card.word
    question = study.Question(
        card_id=card.id,
        stage=data["stage"],
        kind=word.kind,
        display=word.display,
        meaning_kr=word.meaning_kr,
        prompt="",
        answer=data["answer"],
        choices=data["choices"],
        occurrence_id=occurrence.id if occurrence else None,
        sentence=occurrence.sentence if occurrence else None,
        source_label=(
            f"{occurrence.source_title} · {occurrence.occurred_on:%Y-%m-%d}" if occurrence else None
        ),
        source_url=occurrence.source_url if occurrence else None,
        leech=card.leech,
    )
    return question, dt.datetime.fromisoformat(data["shown_at"])


def _question_context(session: Session) -> dict[str, Any]:
    now = study.now_utc()
    card = study.next_card(session, now)
    if card is None:
        session.commit()
        return {"question": None, "progress": study.progress(session, now)}

    question = study.build_question(session, card)
    session.commit()
    return {
        "question": question,
        "token": _issue(question, now),
        "state": study.queue_state(session, now),
        "word_id": card.word_id,
    }


@app.get("/study", response_class=HTMLResponse)
def study_page(request: Request, session: DB):
    return templates.TemplateResponse(request, "study.html", _question_context(session))


@app.get("/study/card", response_class=HTMLResponse)
def study_card(request: Request, session: DB):
    return templates.TemplateResponse(request, "_card.html", _question_context(session))


@app.post("/study/answer", response_class=HTMLResponse)
def study_answer(
    request: Request,
    session: DB,
    token: Annotated[str, Form()],
    given: Annotated[str, Form()] = "",
    easy: Annotated[str, Form()] = "",
):
    now = study.now_utc()
    question, shown_at = _restore(session, token)
    response_ms = max(0, int((now - shown_at).total_seconds() * 1000))

    result = study.answer(
        session,
        question,
        given,
        response_ms=response_ms,
        self_easy=bool(easy),
        now=now,
    )
    session.commit()

    card = session.get(Card, question.card_id)
    return templates.TemplateResponse(
        request,
        "_feedback.html",
        {
            "result": result,
            "question": question,
            "given": given,
            "word_id": card.word_id,
            # 막힌 카드에만 기억술을 띄운다. 잘 나가는 카드에 붙이면 인출을 대신해 버린다.
            "mnemonic": card.word.mnemonic if card.leech else None,
        },
    )


# --------------------------------------------------------------------------
# 어휘
# --------------------------------------------------------------------------


@app.get("/words", response_class=HTMLResponse)
def words(
    request: Request,
    session: DB,
    q: Annotated[str, Query()] = "",
    band: Annotated[str, Query()] = "",
    page: Annotated[int, Query(ge=1)] = 1,
):
    per_page = 50
    query = select(Word).options(selectinload(Word.card))
    if q:
        needle = f"%{q.strip().lower()}%"
        query = query.where(
            or_(func.lower(Word.headword).like(needle), Word.meaning_kr.like(needle))
        )
    if band:
        query = query.where(Word.band == band)

    total = session.scalar(
        select(func.count()).select_from(query.order_by(None).subquery())
    ) or 0
    rows = session.scalars(
        query.order_by(Word.first_seen.desc(), Word.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()

    return templates.TemplateResponse(
        request,
        "words.html",
        {
            "words": rows,
            "q": q,
            "band": band,
            "page": page,
            "total": total,
            "pages": max(1, (total + per_page - 1) // per_page),
            "bands": (banding.BAND_CORE, banding.BAND_KNOWN, banding.BAND_RARE),
        },
    )


@app.get("/words/{word_id}", response_class=HTMLResponse)
def word_detail(request: Request, session: DB, word_id: int):
    word = session.get(Word, word_id)
    if word is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "어휘를 찾을 수 없습니다.")
    reviews = (
        session.scalars(
            select(ReviewLog)
            .where(ReviewLog.card_id == word.card.id)
            .order_by(ReviewLog.reviewed_at.desc())
            .limit(20)
        ).all()
        if word.card
        else []
    )
    return templates.TemplateResponse(
        request, "word.html", {"word": word, "reviews": reviews}
    )


@app.post("/words/{word_id}/known")
def toggle_known(request: Request, session: DB, word_id: int):
    """이미 아는 단어 표시. 빈도 밴드 판정을 사람이 덮어쓰는 장치다.

    카드까지 지우지는 않는다. 지금까지 쌓은 복습 기록이 날아가면 안 되므로 정지만 시킨다.
    """
    word = session.get(Word, word_id)
    if word is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "어휘를 찾을 수 없습니다.")
    word.known = not word.known
    if word.card:
        word.card.suspended = word.known
    session.commit()

    # 어휘 상세 화면에는 조각을 갈아 끼울 자바스크립트가 없다(study.js 는 #card 가
    # 있을 때만 붙는다). 조각을 그대로 돌려주면 CSS 도 내비게이션도 없는 맨 HTML 에
    # 사용자가 갇힌다. 평범한 POST-redirect-GET 으로 되돌린다.
    return RedirectResponse(f"/words/{word_id}", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------
# 주 1회 작문
# --------------------------------------------------------------------------


@app.get("/write", response_class=HTMLResponse)
def write_page(request: Request, session: DB):
    task = compose.ensure(session)
    session.commit()
    return templates.TemplateResponse(
        request, "write.html", {"task": task, "history": compose.history(session)}
    )


@app.post("/write", response_class=HTMLResponse)
def write_submit(request: Request, session: DB, text: Annotated[str, Form()] = ""):
    task = compose.ensure(session)
    if task is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "이번 주 과제가 없습니다.")
    try:
        compose.submit(session, task, text)
    except ValueError as e:
        session.rollback()
        return templates.TemplateResponse(
            request, "write.html",
            {"task": compose.ensure(session), "history": compose.history(session), "error": str(e)},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    session.commit()
    return RedirectResponse("/write", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------
# 통계
# --------------------------------------------------------------------------


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request, session: DB):
    now = study.now_utc()
    since = now - dt.timedelta(days=30)

    logs = session.scalars(select(ReviewLog).where(ReviewLog.reviewed_at >= since)).all()
    by_stage: dict[str, list[bool]] = {}
    for entry in logs:
        by_stage.setdefault(entry.stage, []).append(entry.correct)

    daily: dict[dt.date, list[bool]] = {}
    tz = study.timezone()
    for entry in logs:
        daily.setdefault(entry.reviewed_at.astimezone(tz).date(), []).append(entry.correct)

    band_rows = session.execute(
        select(Word.band, func.count()).group_by(Word.band)
    ).all()
    stage_rows = session.execute(
        select(Card.stage, func.count()).where(Card.suspended.is_(False)).group_by(Card.stage)
    ).all()

    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "progress": study.progress(session, now),
            "accuracy_overall": (sum(1 for e in logs if e.correct) / len(logs)) if logs else None,
            "target_retention": study.scheduler.build().name,
            "by_stage": {k: (sum(v) / len(v), len(v)) for k, v in sorted(by_stage.items())},
            "daily": sorted(
                ((day, sum(v) / len(v), len(v)) for day, v in daily.items()), reverse=True
            )[:14],
            "bands": dict(band_rows),
            "stages": dict(stage_rows),
            "reviews_total": session.scalar(select(func.count()).select_from(ReviewLog)) or 0,
        },
    )


# --------------------------------------------------------------------------
# 로컬 파이프라인 연동
# --------------------------------------------------------------------------


class EntryIn(BaseModel):
    display: str = Field(min_length=1, max_length=200)
    kind: str
    #: 비워서 보낼 수 있다. 수업 PC 는 전사문에서 후보를 고르는 데까지만 하고, 뜻은
    #: LLM 이 있는 맥이 `/api/tasks` 로 가져가 채운다. 뜻이 빌 동안 그 어휘는 카드가
    #: 되지 않으므로(`study._new_word_query`), 빈 문제가 출제될 일은 없다.
    meaning_kr: str = ""
    source_kind: str
    source_title: str = ""
    occurred_on: dt.date
    meaning_en: str | None = None
    usage_note: str | None = None
    sentence: str | None = None
    translation_kr: str | None = None
    source_url: str | None = None


class IngestIn(BaseModel):
    entries: list[EntryIn]


@app.post("/ingest")
def ingest(request: Request, session: DB, payload: IngestIn):
    """로컬이 만든 어휘를 받는다.

    멱등하다. 같은 것을 다시 밀어도 어휘는 (표제어, 종류) 로, 예문은 (어휘, 문장) 으로
    합쳐진다. 로컬이 매번 전체를 보내도 되므로 증분 추적 장치가 필요 없다.

    여섯 개 기계용 엔드포인트 중 좁은 토큰(`VOCAB_REMOTE_TOKEN`)을 받는 것은 여기뿐이다.
    수업 PC 가 가진 토큰으로 `/api/export` 를 부를 수 없어야 한다.
    """
    security.require_ingest_token(request, remote_ok=True)

    stats = collect.Stats()
    for item in payload.entries:
        collect.upsert(session, collect.Entry(**item.model_dump()), stats)
    session.commit()

    return {
        "received": len(payload.entries),
        "words_created": stats.words_created,
        "words_updated": stats.words_updated,
        "occurrences_created": stats.occurrences_created,
        "total_words": session.scalar(select(func.count()).select_from(Word)) or 0,
    }


@app.get("/api/progress")
def api_progress(request: Request, session: DB):
    """오늘 뭐가 남았는지. 로컬 cron 이 텔레그램 알림을 만들 때 부른다.

    사람 세션이 아니라 기계가 부르므로 ingest 토큰을 쓴다.
    """
    security.require_ingest_token(request)
    state = study.progress(session)
    return {
        "due": state.due,
        "new_available": state.new_available,
        "unresolved": state.unresolved,
        "reviewed_today": state.reviewed_today,
        "accuracy_today": state.accuracy_today,
        "total_cards": state.total_cards,
        "total_words": state.total_words,
        "next_due": state.next_due.isoformat() if state.next_due else None,
    }


def _dump(session: Session) -> dict:
    """전체 백업. 리뷰 기록은 서버에만 있는 유일한 데이터라 내려받을 길이 있어야 한다."""

    def rows(model):
        return [
            {c.name: _jsonable(getattr(row, c.name)) for c in model.__table__.columns}
            for row in session.scalars(select(model)).all()
        ]

    return {
        "exported_at": study.now_utc().isoformat(),
        "word": rows(Word),
        "occurrence": rows(Occurrence),
        "card": rows(Card),
        "review_log": rows(ReviewLog),
    }


@app.get("/export")
def export(session: DB):
    """사람이 브라우저에서 내려받는 백업."""
    return JSONResponse(
        _dump(session),
        headers={
            "Content-Disposition": f'attachment; filename="vocab-{study.now_utc():%Y%m%d}.json"'
        },
    )


class GlossIn(BaseModel):
    word_id: int
    meaning_kr: str = Field(min_length=1, max_length=500)
    meaning_en: str | None = Field(default=None, max_length=500)
    usage_note: str | None = Field(default=None, max_length=1000)


class MnemonicIn(BaseModel):
    word_id: int
    text: str = Field(min_length=1, max_length=2000)


class FeedbackIn(BaseModel):
    composition_id: int
    text: str = Field(min_length=1, max_length=20000)


class TaskResultIn(BaseModel):
    glosses: list[GlossIn] = Field(default_factory=list)
    mnemonics: list[MnemonicIn] = Field(default_factory=list)
    feedback: list[FeedbackIn] = Field(default_factory=list)


@app.get("/api/tasks")
def api_tasks(request: Request, session: DB):
    """로컬 워커가 가져갈 일감.

    LLM 은 로컬 프록시에만 있으므로 서버는 "이걸 해 달라" 고 쌓아 두기만 한다.
    """
    security.require_ingest_token(request)
    return {
        "glosses": [
            {"word_id": word.id, "display": word.display, "kind": word.kind,
             "examples": [o.sentence for o in word.occurrences[:3]]}
            for word in compose.words_without_gloss(session)
        ],
        "mnemonics": [
            {"word_id": word.id, "display": word.display, "meaning_kr": word.meaning_kr,
             "kind": word.kind,
             "examples": [o.sentence for o in word.occurrences[:3]]}
            for word in compose.leeches_without_mnemonic(session)
        ],
        "compositions": [
            {"composition_id": task.id, "week_start": task.week_start.isoformat(),
             "words": task.word_list, "text": task.text}
            for task in compose.pending_feedback(session)
        ],
    }


@app.post("/api/tasks")
def api_tasks_result(request: Request, session: DB, payload: TaskResultIn):
    """로컬 워커가 돌려주는 결과."""
    security.require_ingest_token(request)
    now = study.now_utc()
    applied = {"glosses": 0, "mnemonics": 0, "feedback": 0}

    for item in payload.glosses:
        word = session.get(Word, item.word_id)
        # 이미 뜻이 있으면 덮어쓰지 않는다. 그 사이에 수업 노트가 같은 표제어를 제대로
        # 채웠을 수 있고, 사람이 쓴 뜻이 생성된 뜻보다 낫다.
        if word is not None and not word.meaning_kr:
            word.meaning_kr = item.meaning_kr
            if item.meaning_en and not word.meaning_en:
                word.meaning_en = item.meaning_en
            if item.usage_note and not word.usage_note:
                word.usage_note = item.usage_note
            word.updated_at = now
            applied["glosses"] += 1

    for item in payload.mnemonics:
        word = session.get(Word, item.word_id)
        if word is not None:
            word.mnemonic = item.text
            applied["mnemonics"] += 1

    for item in payload.feedback:
        task = session.get(Composition, item.composition_id)
        if task is not None:
            task.feedback = item.text
            task.feedback_at = now
            applied["feedback"] += 1

    session.commit()
    return applied


@app.get("/api/handled")
def api_handled(request: Request, session: DB):
    """이미 학습 중이거나 안다고 표시한 표제어.

    로컬 파이프라인이 새 어휘 후보를 고를 때 뺄 목록이다. 카드가 서버로 옮겨간 뒤로
    로컬만 보고는 알 수 없게 됐다.

    좁은 토큰(수업 PC)도 받는다. 이걸 못 부르면 이미 외우는 중인 단어가 후보 자리를
    차지하는데, 그 실패가 조용해서 몇 주씩 이어질 수 있다. 나가는 것은 표제어 목록뿐
    으로 뜻도 복습 기록도 없고, 애초에 그 PC 가 보낸 어휘가 대부분이다.
    """
    security.require_ingest_token(request, remote_ok=True)
    rows = session.execute(
        select(Word.headword).where(
            Word.known.is_(True)
            | Word.band.in_((banding.BAND_KNOWN, banding.BAND_RARE))
            | Word.id.in_(select(Card.word_id))
        )
    ).all()
    return {"headwords": [row[0] for row in rows]}


@app.get("/api/export")
def api_export(request: Request, session: DB):
    """기계용 백업. `python -m vocab.optimize` 가 복습 기록을 가져갈 때 쓴다."""
    security.require_ingest_token(request)
    return _dump(session)


def _jsonable(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


# --------------------------------------------------------------------------
# 운영
# --------------------------------------------------------------------------


@app.get("/healthz", response_class=PlainTextResponse)
def healthz(session: DB):
    session.execute(select(1))
    return "ok"


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse(
        {
            "name": "stueng 단어",
            "short_name": "단어",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#f2f4f8",
            "theme_color": "#4338ca",
            "icons": [
                {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml",
                 "purpose": "any maskable"}
            ],
        },
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
def service_worker():
    """설치 가능하게 만드는 최소 워커.

    오프라인 캐싱은 하지 않는다. 복습은 서버 상태를 바꾸는 일이라 오프라인으로 풀면
    같은 카드를 두 번 세는 문제가 생긴다. 홈화면 추가만 되면 목적을 다한 것이다.
    """
    return PlainTextResponse(
        "self.addEventListener('fetch', () => {});\n",
        media_type="application/javascript",
    )
