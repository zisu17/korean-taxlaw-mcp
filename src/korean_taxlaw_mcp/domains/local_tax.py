"""지방세 조회 계층 — 한국지방세연구원(KILF) 지방세 법령정보시스템.

국세청(국세)과 나란히 놓이는 지방세 층이다. 담당 자료:

===============  ===================================  ==========================
kind             자료                                 권위 층위
===============  ===================================  ==========================
interpretation   행정안전부 유권해석 (지방세 예규)      local_ruling
moleg            법제처 유권해석                      local_ruling
tribunal         조세심판원 심판결정례                 adjudication
audit            감사원 심사결정례                    adjudication
court            법원 판례                            court_case
constitutional   헌법재판소 결정례                     court_case
===============  ===================================  ==========================

사이트의 문서번호 검색(``searchType=2``)은 일련번호 부분일치다. `924`를 넣으면
`부동산세제과-924`뿐 아니라 `지방세운영-4924`, `지방세정팀-2924`도 함께 온다(실측).
반환된 문서번호가 입력과 정확히 같을 때만 exact로 인정하고 나머지는 유사문서로
분리한다.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..codes import LOCAL_TAX_TYPE
from ..config import OLTA
from ..errors import ErrorCode, NtsError, not_found
from ..html_text import truncate
from ..local_doc_number import is_same_local_doc_number, parse_local_doc_number
from ..model import AUTHORITY_LABEL, AuthorityLevel
from ..olta_client import SOURCES, detail_html, detail_url, search_html
from ..olta_parse import DETAIL_SECTION_NAMES, parse_detail, parse_rows, parse_total
from ..cache import TTL

#: kind → 권위 층위. 유권해석은 국세청 예규와 같은 성격이므로 별도 층으로 둔다.
_AUTHORITY: dict[str, AuthorityLevel] = {
    "interpretation": AuthorityLevel.LOCAL_RULING,
    "moleg": AuthorityLevel.LOCAL_RULING,
    "tribunal": AuthorityLevel.ADJUDICATION,
    "audit": AuthorityLevel.ADJUDICATION,
    "court": AuthorityLevel.COURT_CASE,
    "constitutional": AuthorityLevel.COURT_CASE,
}

INTERPRETATION_KINDS = ("interpretation", "moleg")
DECISION_KINDS = ("tribunal", "audit", "court", "constitutional")

def _site_date(value: str | None, *, default: str, field: str) -> str:
    """YYYY-MM-DD / YYYYMMDD → 사이트가 받는 YYYY.MM.DD."""
    if not value:
        return default
    raw = str(value).strip()
    matched = re.fullmatch(r"(\d{4})(?:[.-]?(\d{2})(?:[.-]?(\d{2}))?)?", raw)
    if not matched:
        raise NtsError(
            ErrorCode.INVALID_INPUT,
            f"{field} 형식이 올바르지 않습니다: {value}",
            hints=["YYYY, YYYY-MM, YYYY-MM-DD 또는 구분자 없는 숫자 형식을 사용하세요."],
        )
    year, month, day = matched.group(1), matched.group(2) or "01", matched.group(3) or "01"
    try:
        date(int(year), int(month), int(day))
    except ValueError as exc:
        raise NtsError(
            ErrorCode.INVALID_INPUT,
            f"{field} 형식이 올바르지 않습니다: {value}",
            hints=["실제 달력에 존재하는 날짜를 사용하세요."],
        ) from exc
    return f"{year}.{month}.{day}"


def resolve_tax_codes(value: str | list[str] | None) -> tuple[list[str], list[str]]:
    """세목 입력(이름·별칭·코드) → 코드 목록과 인식 실패 목록."""
    from ..codes import LOCAL_TAX_TYPE_ALIAS

    if not value:
        return [], []
    raw = [value] if isinstance(value, str) else list(value)
    codes: list[str] = []
    unresolved: list[str] = []
    for item in raw:
        s = str(item).strip()
        if not s:
            continue
        if s in LOCAL_TAX_TYPE:
            codes.append(s)
            continue
        alias = LOCAL_TAX_TYPE_ALIAS.get(s) or LOCAL_TAX_TYPE_ALIAS.get(s.replace(" ", ""))
        if alias:
            codes.append(alias)
            continue
        by_name = next((c for c, n in LOCAL_TAX_TYPE.items() if n == s or s in n), None)
        if by_name:
            codes.append(by_name)
            continue
        unresolved.append(s)
    return list(dict.fromkeys(codes)), unresolved


def _summary(kind: str, row: dict[str, str]) -> dict[str, Any]:
    """목록 행 → 요약. 응답 최상위와 겹치는 상수 필드(source·taxLevel 등)는
    항목마다 반복하지 않는다. kind 는 상세 조회 입력이므로 남긴다."""
    out: dict[str, Any] = {
        "kind": kind,
        "documentType": SOURCES[kind].label,
        "documentNumber": row.get("documentNumber", ""),
        "title": row.get("title", ""),
        "taxType": row.get("taxType"),
        "registrationDate": row.get("registrationDate"),
        "summary": row.get("gist"),
        "documentId": row["num"],
        # 법원 판례는 상세 조회에 둘째 인수가 필요하다.
        "relationshipNum": row.get("relationshipNum"),
        "decisionResult": row.get("decisionResult"),
        "sourceUrl": detail_url(kind, row["num"], row.get("relationshipNum")),
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


async def search_local_documents(
    *,
    kinds: list[str],
    query: str | None = None,
    tax_type_codes: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    limit: int = 10,
    doc_number_mode: bool = False,
) -> dict[str, Any]:
    """자료 종류별 검색.

    사이트는 한 화면에 한 종류만 담으므로, 여러 종류를 요청하면 순차로 조회해 합친다.
    ``startCount`` 는 **오프셋**이라 페이지 번호에서 변환해 보낸다(국세청은 페이지 번호).
    """
    unknown = [k for k in kinds if k not in SOURCES]
    if unknown:
        raise NtsError(
            ErrorCode.INVALID_INPUT,
            f"지원하지 않는 지방세 자료 종류: {', '.join(unknown)}",
            hints=[f"가능한 값: {', '.join(SOURCES)}"],
        )
    if not kinds:
        raise NtsError(ErrorCode.INVALID_INPUT, "조회할 지방세 자료 종류가 없습니다.")

    page = max(1, page)
    limit = min(50, max(1, limit))
    # 사이트는 한 화면에 10건씩 낸다. 오프셋은 10의 배수로만 유효하다.
    offset = (page - 1) * 10

    start_date = _site_date(date_from, default="1970.01.01", field="date_from")
    end_date = _site_date(date_to, default="", field="date_to")

    per_kind: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    total = 0
    for kind in kinds:
        rows: list[dict[str, str]] = []
        next_offset = offset
        count = 0
        while True:
            html = await search_html(
                kind,
                query=query or "",
                tax_codes=tax_type_codes,
                offset=next_offset,
                date_from=start_date,
                date_to=end_date,
                doc_number_mode=doc_number_mode,
                ttl=TTL.SEARCH,
            )
            page_rows = parse_rows(html)
            if not rows:
                count = parse_total(html) or len(page_rows)
            rows.extend(page_rows)

            available = max(0, count - offset)
            target = min(limit, available)
            if not page_rows or len(rows) >= target:
                break
            next_offset += 10

        total += count
        per_kind.append({"kind": kind, "label": SOURCES[kind].label, "count": count})
        items.extend(_summary(kind, r) for r in rows)

    return {
        "total": total,
        "countsByKind": per_kind,
        "page": page,
        "limit": limit,
        "items": items[:limit],
    }


async def get_local_document(
    kind: str,
    document_id: str,
    *,
    relationship_num: str | None = None,
    fallback_document_number: str | None = None,
    include_full_text: bool = True,
    body_limit: int | None = None,
) -> dict[str, Any]:
    """상세 조회. ``document_id`` 는 목록 결과의 ``documentId``(사이트 내부 num)다."""
    if kind not in SOURCES:
        raise NtsError(ErrorCode.INVALID_INPUT, f"지원하지 않는 지방세 자료 종류: {kind}")
    doc_id = str(document_id or "").strip()
    if not doc_id.isdigit():
        raise NtsError(
            ErrorCode.INVALID_INPUT,
            f"documentId 형식이 아닙니다: {document_id}",
            hints=["documentId 는 검색 결과의 documentId 값(숫자)입니다."],
        )

    html = await detail_html(kind, doc_id, relationship_num=relationship_num, ttl=TTL.DETAIL)
    parsed = parse_detail(html)
    if not parsed or not (parsed.get("title") or parsed.get("fullText")):
        raise not_found(
            f"지방세 문서 {doc_id} 의 상세 내용을 가져오지 못했습니다.",
            ["documentId 가 검색 결과에서 가져온 값인지 확인하세요."],
        )

    limit = body_limit if body_limit is not None else OLTA.body_limit
    level = _AUTHORITY[kind]
    url = detail_url(kind, doc_id, relationship_num)
    # 법원 판례 상세 화면은 머리글에 문서번호를 싣지 않는다. 목록에서 읽은 값으로
    # 보완해야 출처 표기가 비지 않는다.
    document_number = str(parsed.get("documentNumber") or fallback_document_number or "")

    def cut(value: object) -> str | None:
        text = str(value or "").strip()
        return truncate(text, limit).text if text else None

    related = str(parsed.get("relatedLaws") or "").strip()
    related_list = [x.strip() for x in re.split(r"[,;\n]", related) if x.strip()] if related else []

    out: dict[str, Any] = {
        "taxLevel": "local",
        "domain": "interpretation" if kind in INTERPRETATION_KINDS else "decision",
        "kind": kind,
        "documentType": SOURCES[kind].label,
        "documentNumber": document_number,
        "title": str(parsed.get("title") or ""),
        "taxType": parsed.get("taxType"),
        "registrationDate": parsed.get("registrationDate"),
        "summary": cut(parsed.get("gist")),
        "question": cut(parsed.get("question")),
        "answer": cut(parsed.get("answer")),
        "reasoning": cut(parsed.get("reasoning")),
        "body": cut(parsed.get("body")),
        "preamble": cut(parsed.get("unsectioned")),
        "relatedLaws": related_list,
        "authorityLevel": str(level),
        "authorityNote": AUTHORITY_LABEL[level],
        "documentId": doc_id,
        "sourceUrl": url,
    }

    # 절 분해가 됐으면 fullText 를 싣지 않는다 — 절(+preamble)이 본문 전체를 담고
    # 있어서, 전문을 함께 실으면 같은 본문이 두 번 전달된다.
    full_text = str(parsed.get("fullText") or "")
    has_sections = any(parsed.get(name) for name in DETAIL_SECTION_NAMES)
    if include_full_text and full_text and not has_sections:
        full = truncate(full_text, limit)
        out["fullText"] = full.text
        if full.truncated:
            out["fullTextTruncated"] = True
            out["fullTextOriginalLength"] = full.original_length

    return {k: v for k, v in out.items() if v not in (None, "", [])}


async def lookup_local_by_document_number(
    raw: str,
    *,
    kinds: list[str] | None = None,
    include_full_text: bool = True,
    body_limit: int | None = None,
    similar_limit: int = 5,
) -> dict[str, Any]:
    """문서번호로 지방세 문서를 찾는다. **정확히 일치할 때만** found.

    사이트의 문서번호 검색은 일련번호 부분일치라서(`924` → `4924`·`2924` 도 매칭)
    반환값을 그대로 믿으면 다른 문서를 정답으로 내놓게 된다.
    """
    parsed = parse_local_doc_number(raw)
    targets = kinds or [*INTERPRETATION_KINDS, *DECISION_KINDS]
    similar: dict[str, dict[str, Any]] = {}
    tried: list[str] = []

    # 일련번호가 잡히면 문서번호 검색 모드, 아니면 통합검색으로 폴백한다.
    candidates: list[tuple[str, bool]] = []
    if parsed.serial:
        candidates.append((parsed.serial, True))
    candidates.append((parsed.canonical, False))

    for kind in targets:
        for candidate, doc_mode in candidates:
            tried.append(f"{kind}:{candidate}{'(문서번호)' if doc_mode else ''}")
            # 사이트는 10건/페이지 — 30을 요청하면 순차 3회 왕복이 된다.
            # exact 일치는 문서번호 검색 모드의 첫 페이지에 올라온다.
            result = await search_local_documents(
                kinds=[kind], query=candidate, limit=10, doc_number_mode=doc_mode
            )
            for item in result["items"]:
                if is_same_local_doc_number(item.get("documentNumber", ""), raw):
                    document = await get_local_document(
                        kind, item["documentId"],
                        relationship_num=item.get("relationshipNum"),
                        fallback_document_number=item.get("documentNumber"),
                        include_full_text=include_full_text, body_limit=body_limit,
                    )
                    # 진단 메타데이터(triedQueries 등)는 NOT_FOUND 전용 — 성공에는 싣지 않는다.
                    return {
                        "found": True,
                        "exactMatch": True,
                        "kind": kind,
                        "document": document,
                    }
                if item.get("documentNumber"):
                    similar.setdefault(item["documentId"], item)

    # relationshipNum 은 법원 판례 상세 조회의 필수 둘째 인수라 빼면 후속 조회가 막힌다.
    keep = (
        "kind", "documentType", "documentNumber", "title",
        "registrationDate", "documentId", "relationshipNum",
    )
    similar_documents = [
        {k: s[k] for k in keep if s.get(k)} for s in list(similar.values())[:similar_limit]
    ]
    out: dict[str, Any] = {
        "found": False,
        "exactMatch": False,
        "normalizedDocumentNumber": parsed.canonical,
        "inputInterpretation": parsed.interpretation(),
        "triedQueries": tried,
        "note": (
            "입력한 문서번호와 정확히 일치하는 지방세 문서가 없습니다. similarDocuments 는 "
            "일련번호가 부분적으로 겹치는 별개의 문서이며, 요청한 문서가 아닙니다. "
            "이 중 하나를 정답으로 제시하지 마세요."
            if similar_documents
            else "입력한 문서번호와 정확히 일치하는 지방세 문서도, 부분적으로 겹치는 문서도 찾지 못했습니다."
        ),
    }
    if similar_documents:
        out["similarDocuments"] = similar_documents
    return out
