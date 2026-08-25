"""별표·법령서식.

액션(실측): ``ASIAFB001MR02 {}`` → 서식을 가진 법령 1,748건,
``ASIAFB001MR01 {searchNtstBscId, ...}`` → 서식 34,487건.
파라미터 이름은 ``ntstBscId``가 아니라 ``searchNtstBscId``다
(``ntstBscId`` 로 주면 0건이 온다).

파일 실물 다운로드는 사이트가 POST 폼(``/downloadStorFile.do``)으로 렌더링해
내려주는 방식이라 안정적인 GET URL이 없다. MCP는 바이너리 대신 서식 메타데이터와
조회 화면 URL만 제공한다.
"""

from __future__ import annotations

import re
from typing import Any

from ..action_client import call_action
from ..cache import TTL
from ..config import NTS_ORIGIN
from ..model import AuthorityLevel
from ..payload import drop_empty
from ..query import format_date

FORMS_URL = f"{NTS_ORIGIN}/af/USEAFB001M.do"

DOWNLOAD_NOTE = (
    "서식 파일 실물은 국세법령정보시스템이 POST 폼 기반으로만 내려주므로 이 서버는 "
    "바이너리를 제공하지 않습니다. sourceUrl 화면에서 내려받으세요."
)


def _norm(s: str) -> str:
    return re.sub(r"[\s「」]", "", s)


async def list_form_laws() -> list[dict[str, str]]:
    """서식을 보유한 법령 목록."""
    payload = await call_action("ASIAFB001MR02", {}, ttl=TTL.STATIC)
    return [
        {
            "ntstBscId": str(r.get("ntstBscId")),
            "ntstNm": str(r.get("ntstNm") or "").strip(),
            "ntstSysClCd": str(r.get("ntstSysClCd") or ""),
        }
        for r in (payload or {}).get("tlawDVOList") or []
    ]


async def search_forms(
    *,
    query: str | None = None,
    law_name: str | None = None,
    page: int = 1,
    limit: int = 20,
) -> dict[str, Any]:
    page = max(1, page)
    limit = min(100, max(1, limit))

    search_id = "stttAll"
    matched_law: str | None = None
    if law_name and law_name.strip():
        laws = await list_form_laws()
        needle = _norm(law_name)
        hit = next((l for l in laws if _norm(l["ntstNm"]) == needle), None) or next(
            (l for l in laws if needle and needle in _norm(l["ntstNm"])), None
        )
        if hit:
            search_id = hit["ntstBscId"]
            matched_law = hit["ntstNm"]

    payload = await call_action(
        "ASIAFB001MR01",
        {
            "searchNtstBscId": search_id,
            "searchFrmlNm": query or "",
            "pageIndex": page,
            "recordCountPerPage": limit,
        },
        referer=FORMS_URL,
        ttl=TTL.GUIDANCE,
    )

    rows = (payload or {}).get("stttFrmlDVOList") or []
    items: list[dict[str, Any]] = []
    for r in rows:
        name = re.sub(r"\s+", " ", str(r.get("ntstAtFrmlNm") or "")).strip()
        # 항목에는 서식을 특정하는 값만 싣는다. authorityLevel·sourceUrl·downloadNote
        # 처럼 모든 서식에 동일한 값은 응답 최상위에 한 번만 둔다.
        entry: dict[str, Any] = {
            "formName": name,
            "formSerial": str(r["ntstAtFrmlSn"]) if r.get("ntstAtFrmlSn") else None,
            "lawName": (str(r["ntstNm"]).strip() if r.get("ntstNm") else matched_law),
            "revisionDate": format_date(r.get("ntstPmgDt")),
        }
        items.append(drop_empty(entry))

    out: dict[str, Any] = {
        "authorityLevel": str(AuthorityLevel.ENFORCEMENT_RULE),
        "sourceUrl": FORMS_URL,
        "downloadNote": DOWNLOAD_NOTE,
        "total": int((payload or {}).get("recordCount") or 0),
        "page": page,
        "limit": limit,
        "items": items,
    }
    if law_name and not matched_law:
        out["note"] = (
            f"'{law_name}' 에 해당하는 법령을 서식 보유 법령 목록에서 찾지 못해 "
            "전체 서식을 대상으로 검색했습니다."
        )
    return out
