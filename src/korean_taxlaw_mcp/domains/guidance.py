"""국세청 행정 해석기준: 기본통칙 · 세법집행기준 · 고시 · 훈령.

세무 상담에서는 법조문과 과세관청의 집행기준을 함께 확인해야 한다. 이 자료는 법규가
아니므로 모든 결과에 ``authorityLevel: "nts_guidance"``를 붙여 층위를 구분한다.

액션 구조(실측)::

    ASISTD001MR01 {}                                 → 기본통칙 보유 법령 15개
    ASISTD001MR03 {ntstBscId}                        → 기본통칙 개정연도 목록
    ASISTD001MR02 {ntstBscId, rgtYr}                 → 기본통칙 조항 + 본문
    ASISTE001MR03 {ntstBscId, ntstPlcnBkId}          → 집행기준 연도 목록
    ASISTE001MR02 {ntstBscId, rgtYr}                 → 집행기준 조항 목차
    ASISTF001MR01 {ntarClCd, ntstSjtClCd:"All", …}   → 고시(01)·훈령(03)

기본통칙은 조항 본문(``ntstTextCntn``)이 함께 오지만 집행기준은 목차만 온다
(실측: 483건 전부 ``ntstTextCntn`` 공백). 집행기준 본문은
연도별 PDF 로만 배포되므로, 여기서는 목차·조항명과 PDF 파일 식별자까지만
제공하고 본문을 지어내지 않는다.
"""

from __future__ import annotations

import re
from typing import Any

from ..action_client import call_action
from ..cache import TTL, cache
from ..config import NTS_ORIGIN
from ..errors import ErrorCode, NtsError, not_found
from ..html_text import html_to_text, truncate
from ..model import AUTHORITY_LABEL, AuthorityLevel
from ..query import format_date

GUIDANCE_LABEL: dict[str, str] = {
    "basic_ruling": "국세 기본통칙",
    "execution_standard": "세법집행기준",
    "notice": "국세청 고시",
    "directive": "국세청 훈령",
}

_URL: dict[str, str] = {
    "basic_ruling": f"{NTS_ORIGIN}/st/USESTD001M.do",
    "execution_standard": f"{NTS_ORIGIN}/st/USESTE002M.do",
    "notice": f"{NTS_ORIGIN}/st/USESTF001M.do",
    "directive": f"{NTS_ORIGIN}/st/USESTG001M.do",
}

EXECUTION_STANDARD_UNAVAILABLE = (
    "국세법령정보시스템은 세법집행기준의 조항 본문을 목록 응답으로 제공하지 않습니다"
    "(연도별 PDF 로만 배포). 조항명만 확인된 상태이며 본문을 추측하지 마세요."
)

#: 세법집행기준 대상 목록.
#:
#: 사이트는 이 목록을 정적 스크립트(``/js/common/common_st.js`` 의 ``exeBaseStttList``)에
#: 박아두고 있어 조회 액션이 없다. 그래서 실측값을 상수로 옮겨왔다.
#: ``ntstPlcnBkId`` 는 연도 목록 조회에 반드시 필요하다.
EXECUTION_STANDARD_BOOKS: list[dict[str, str]] = [
    {"ntstBscId": "100000000000001586", "ntstPlcnBkId": "511100000000000001", "ntstNm": "국세기본법 집행기준"},
    {"ntstBscId": "100000000000001585", "ntstPlcnBkId": "511100000000000002", "ntstNm": "국세징수법 집행기준"},
    {"ntstBscId": "100000000000001563", "ntstPlcnBkId": "511100000000000003", "ntstNm": "법인세 집행기준"},
    {"ntstBscId": "100000000000000603", "ntstPlcnBkId": "511100000000000004", "ntstNm": "국제조세 집행기준"},
    {"ntstBscId": "100000000000001565", "ntstPlcnBkId": "511100000000000005", "ntstNm": "종합소득세 집행기준"},
    {"ntstBscId": "200000000000001565", "ntstPlcnBkId": "511100000000000006", "ntstNm": "양도소득세 집행기준"},
    {"ntstBscId": "100000000000009873", "ntstPlcnBkId": "511100000000000007", "ntstNm": "종합부동산세 집행기준"},
    {"ntstBscId": "100000000000001561", "ntstPlcnBkId": "511100000000000008", "ntstNm": "상속증여세 집행기준"},
    {"ntstBscId": "100000000000001570", "ntstPlcnBkId": "511100000000000009", "ntstNm": "개별소비세 집행기준"},
    {"ntstBscId": "100000000000001568", "ntstPlcnBkId": "511100000000000009", "ntstNm": "인지세 집행기준"},
    {"ntstBscId": "100000000000001566", "ntstPlcnBkId": "511100000000000009", "ntstNm": "주세 집행기준"},
    {"ntstBscId": "100000000000013931", "ntstPlcnBkId": "511100000000000009", "ntstNm": "주류면허법 집행기준"},
    {"ntstBscId": "100000000000000621", "ntstPlcnBkId": "511100000000000010", "ntstNm": "증권거래세 집행기준"},
    {"ntstBscId": "100000000000001571", "ntstPlcnBkId": "510000000000000448", "ntstNm": "부가가치세 집행기준"},
    {"ntstBscId": "100000000000001584", "ntstPlcnBkId": "511100000000000011", "ntstNm": "조세특례제한법 집행기준"},
]


def _norm(s: str) -> str:
    return re.sub(r"[\s「」]|및|의", "", s)


def _header(kind: str) -> dict[str, Any]:
    """모든 항목에 공통인 값은 응답 최상위에 **한 번만** 싣는다.

    이전에는 kind·authorityLevel·sourceUrl·citation 을 항목마다 반복해서
    40건 조회면 같은 문장이 40번 컨텍스트에 실렸다.
    """
    return {
        "kind": kind,
        "kindLabel": GUIDANCE_LABEL[kind],
        "authorityLevel": str(AuthorityLevel.NTS_GUIDANCE),
        "authorityNote": AUTHORITY_LABEL[AuthorityLevel.NTS_GUIDANCE],
        "sourceUrl": _URL[kind],
    }


async def list_basic_ruling_laws() -> list[dict[str, str]]:
    """기본통칙을 가진 법령 목록."""
    payload = await call_action("ASISTD001MR01", {}, ttl=TTL.STATIC)
    return [
        {"ntstBscId": str(r.get("ntstBscId")), "ntstNm": str(r.get("ntstNm") or "").strip()}
        for r in (payload or {}).get("bscExrDVOList") or []
    ]


def _pick(pool: list[dict[str, str]], law_name: str) -> dict[str, str]:
    """법령명으로 대상을 고른다. 별칭·부분일치를 허용한다."""
    needle = _norm(law_name)
    exact = next((x for x in pool if _norm(x["ntstNm"]) == needle), None)
    if exact:
        return exact
    partial = [
        x for x in pool if needle and (needle in _norm(x["ntstNm"]) or _norm(x["ntstNm"]) in needle)
    ]
    if partial:
        return partial[0]
    raise not_found(
        f"'{law_name}' 에 해당하는 대상을 찾지 못했습니다.",
        [f"조회 가능한 대상: {', '.join(x['ntstNm'] for x in pool)}"],
    )


def _years(rows: list[dict[str, Any]]) -> list[str]:
    seen = {str(r.get("rgtYr") or "").strip() for r in rows}
    return sorted((y for y in seen if y), key=lambda y: -int(y))


def _parse_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """조항 행 → ``{title, itemId, text?}``. 캐시에 실리는 값이므로 호출자는
    이 목록과 항목 dict 를 **변경하지 않고** 필터·슬라이스로만 써야 한다."""
    items: list[dict[str, Any]] = []
    for r in rows:
        title = re.sub(r"\s+", " ", str(r.get("ntstTextNm") or "")).strip()
        text = html_to_text(str(r.get("ntstTextCntn") or ""))
        entry: dict[str, Any] = {"title": title}
        if r.get("ntstExrBaseSn"):
            entry["itemId"] = str(r["ntstExrBaseSn"])
        if text:
            entry["text"] = truncate(text).text
        items.append(entry)
    return items


async def get_basic_rulings(
    *, law_name: str, revision_year: str | None = None, query: str | None = None, limit: int = 40
) -> dict[str, Any]:
    """기본통칙 조회. 연도를 생략하면 가장 최근 개정연도를 쓴다."""
    law = _pick(await list_basic_ruling_laws(), law_name)

    years_payload = await call_action(
        "ASISTD001MR03", {"ntstBscId": law["ntstBscId"]}, ttl=TTL.GUIDANCE
    )
    available = _years((years_payload or {}).get("bscExrDVOList") or [])
    if not available:
        raise not_found(f"'{law['ntstNm']}' 의 기본통칙 개정연도 목록이 비어 있습니다.")
    if revision_year and revision_year not in available:
        raise not_found(
            f"'{law['ntstNm']}' 기본통칙에 {revision_year}년 개정본이 없습니다.",
            [f"가능한 연도: {', '.join(available)}"],
        )
    year = revision_year or available[0]

    # 조항 수백 건의 HTML→텍스트 변환은 싸지 않다. HTTP 응답은 call_action 이
    # 캐시하지만 파싱은 호출마다 반복됐으므로, **파싱 결과**를 같은 TTL 로 캐시한다.
    # get_tax_guidance 처럼 전체를 받아 1건만 고르는 호출이 반복돼도 재파싱하지 않는다.
    async def produce() -> list[dict[str, Any]]:
        payload = await call_action(
            "ASISTD001MR02", {"ntstBscId": law["ntstBscId"], "rgtYr": year}, ttl=TTL.GUIDANCE
        )
        return _parse_items((payload or {}).get("bscExrDVOList") or [])

    items = await cache.wrap(
        f"parsed:basic_ruling:{law['ntstBscId']}:{year}", TTL.GUIDANCE, produce
    )

    if query:
        needle = query.strip()
        items = [i for i in items if needle in i["title"] or needle in i.get("text", "")]

    return {
        **_header("basic_ruling"),
        "lawName": law["ntstNm"],
        "revisionYear": year,
        "availableYears": available,
        "total": len(items),
        "items": items[: max(1, min(300, limit))],
    }


async def get_execution_standards(
    *, law_name: str, revision_year: str | None = None, query: str | None = None, limit: int = 60
) -> dict[str, Any]:
    """세법집행기준 목차 조회. 본문은 원본이 주지 않으므로 조항명까지만."""
    book = _pick(EXECUTION_STANDARD_BOOKS, law_name)

    years_payload = await call_action(
        "ASISTE001MR03",
        {"ntstBscId": book["ntstBscId"], "ntstPlcnBkId": book["ntstPlcnBkId"]},
        ttl=TTL.GUIDANCE,
    )
    year_rows = (years_payload or {}).get("exeBaseDVOList") or []
    available = _years(year_rows)
    if not available:
        raise not_found(f"'{book['ntstNm']}' 의 개정연도 목록이 비어 있습니다.")
    if revision_year and revision_year not in available:
        raise not_found(
            f"'{book['ntstNm']}' 에 {revision_year}년 판이 없습니다.",
            [f"가능한 연도: {', '.join(available)}"],
        )
    year = revision_year or available[0]

    async def produce() -> list[dict[str, Any]]:
        payload = await call_action(
            "ASISTE001MR02", {"ntstBscId": book["ntstBscId"], "rgtYr": year}, ttl=TTL.GUIDANCE
        )
        return _parse_items((payload or {}).get("exeBaseDVOList") or [])

    items = await cache.wrap(
        f"parsed:execution_standard:{book['ntstBscId']}:{year}", TTL.GUIDANCE, produce
    )

    if query:
        needle = query.strip()
        items = [i for i in items if needle in i["title"]]
    items = items[: max(1, min(300, limit))]

    out: dict[str, Any] = {
        **_header("execution_standard"),
        "lawName": book["ntstNm"],
        "revisionYear": year,
        "availableYears": available,
        "total": len(items),
        "items": items,
    }
    # 본문 부재 사유는 항목마다 반복하지 않고 응답에 한 번만 싣는다.
    # 최종 반환 items 기준으로 판단해야 '필터 결과가 전부 본문 없음' 인 경우를 놓치지 않는다.
    if any("text" not in i for i in items):
        out["textUnavailableReason"] = EXECUTION_STANDARD_UNAVAILABLE
    return out


async def get_notices_or_directives(
    *, kind: str, query: str | None = None, page: int = 1, limit: int = 20
) -> dict[str, Any]:
    """고시·훈령 목록. 키워드는 사이트가 서버측에서 처리한다."""
    page = max(1, page)
    limit = min(100, max(1, limit))

    payload = await call_action(
        "ASISTF001MR01",
        {
            "ntarClCd": "01" if kind == "notice" else "03",
            # 'All' 을 빼면 목록이 0건으로 온다(실측). 사이트가 초기화 시 넣는 값이다.
            "ntstSjtClCd": "All",
            "searchKeyword": query or "",
            "pageIndex": page,
            "recordCountPerPage": limit,
        },
        referer=_URL[kind],
        ttl=TTL.GUIDANCE,
    )

    items: list[dict[str, Any]] = []
    for r in (payload or {}).get("notcFeldDVOList") or []:
        entry: dict[str, Any] = {"title": str(r.get("ntarNm") or "").strip()}
        if r.get("ntarBscId"):
            entry["noticeId"] = str(r["ntarBscId"])
        promulgated = format_date(r.get("ntarPmgDt"))
        if promulgated:
            entry["promulgationDate"] = promulgated
        if r.get("ntstJrsdDnoNm"):
            entry["jurisdiction"] = str(r["ntstJrsdDnoNm"]).strip()
        if r.get("ntstSjtClNm"):
            entry["taxSubject"] = str(r["ntstSjtClNm"]).strip()
        items.append(entry)

    return {
        **_header(kind),
        "total": int((payload or {}).get("recordCount") or 0),
        "page": page,
        "limit": limit,
        "items": items,
    }


async def search_guidance(
    *,
    kind: str,
    law_name: str | None = None,
    revision_year: str | None = None,
    query: str | None = None,
    page: int = 1,
    limit: int | None = None,
) -> dict[str, Any]:
    """통합 진입점 — 도구 층이 kind 만 넘기면 알맞은 조회로 보낸다."""
    if kind in ("notice", "directive"):
        return await get_notices_or_directives(
            kind=kind, query=query, page=page, limit=limit or 20
        )
    if not law_name:
        pool = (
            await list_basic_ruling_laws()
            if kind == "basic_ruling"
            else EXECUTION_STANDARD_BOOKS
        )
        raise NtsError(
            ErrorCode.INVALID_INPUT,
            f"{GUIDANCE_LABEL[kind]} 조회에는 lawName 이 필요합니다.",
            hints=[f"조회 가능한 대상: {', '.join(x['ntstNm'] for x in pool)}"],
        )
    if kind == "basic_ruling":
        return await get_basic_rulings(
            law_name=law_name, revision_year=revision_year, query=query, limit=limit or 40
        )
    return await get_execution_standards(
        law_name=law_name, revision_year=revision_year, query=query, limit=limit or 60
    )
