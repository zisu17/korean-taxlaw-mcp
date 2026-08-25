"""세법해석례·판례결정례 공용 조회 계층.

사이트는 두 영역을 같은 액션으로 처리한다 — 검색은 ``ASIPDI002PR01``, 상세는
``ASIQTB002PR01`` 하나뿐이고 문서구분(``dcmClCdCtl``)과 컬렉션(``collectionName``)만
갈린다. 그래서 여기도 한 구현으로 묶고, 도구 층에서만 해석례/결정례로 나눈다.
"""

from __future__ import annotations

from typing import Any

from ..action_client import call_action, detail_url
from ..cache import TTL
from ..codes import (
    CASE_KIND,
    DECISION_RESULT,
    DISCLOSURE,
    DOC_CLASS,
    ISSUING_AGENCY,
    TAX_TYPE,
    collection_for,
    decode_or_none,
    is_blank_code,
)
from ..config import NTS_ORIGIN
from ..errors import ErrorCode, NtsError, not_found
from ..html_text import attach_full_text, parse_body_html, strip_highlight, truncate
from ..model import AUTHORITY_LABEL, authority_for_doc_class
from ..payload import drop_empty
from ..query import SORT, build_vocab, format_date, to_site_date

SEARCH_ACTION = "ASIPDI002PR01"
DETAIL_ACTION = "ASIQTB002PR01"

#: 해석례 문서구분(01~04)과 결정례 문서구분(05~10).
INTERPRETATION_CLASSES = ("01", "02", "03", "04")
DECISION_CLASSES = ("05", "06", "07", "08", "09", "10")

_DECISION_SET = set(DECISION_CLASSES)


def _to_dcm_cl_cd_ctl(classes: list[str]) -> list[str]:
    """문서구분 → dcmClCdCtl 값. ``002_01`` 은 세법해석정비 계열이다."""
    return ["002_01" if c == "31" else f"001_{c}" for c in classes]


def _domain_for(doc_class: str) -> str:
    return "decision" if doc_class in _DECISION_SET else "interpretation"


def _kind_for(doc_class: str) -> str:
    return "precedent" if _domain_for(doc_class) == "decision" else "question"


def _row_to_summary(dcm: dict[str, Any]) -> dict[str, Any]:
    """검색 행(대문자 스네이크 필드) → 요약 문서.

    검색 결과는 **후보 목록**이다 — 본문은 싣지 않고, 상수 boilerplate(source·domain)도
    항목마다 반복하지 않는다. LLM 이 후보를 고르는 데 필요한 것:
    문서번호·제목·세목·요지·날짜·상세조회 ID.
    """

    def get(key: str) -> str:
        return strip_highlight(dcm.get(key, "")).strip()

    doc_class = get("NTST_DCM_CL_CD") or (get("MAIN_ID").split("_")[-1] if "_" in get("MAIN_ID") else "")
    doc_id = get("DOC_ID")
    disclosure = get("STTT_INFR_CL_NM") or decode_or_none(DISCLOSURE, get("STTT_INFP_CL_CD"))

    out: dict[str, Any] = {
        "documentType": get("LBL1_TTL") or DOC_CLASS.get(doc_class) or get("NTST_DCM_CL_NM"),
        "documentNumber": get("NTST_DCM_DSCM_CNTN"),
        "title": get("TTL"),
        "taxType": get("NTST_TLAW_CL_NM") or decode_or_none(TAX_TYPE, get("NTST_TLAW_CL_CD")),
        "issuingAgency": decode_or_none(ISSUING_AGENCY, get("NTST_DCM_SRCS_ORGN_CL_CD")),
        "decisionResult": get("NTST_DCM_DCS_CL_NM")
        or decode_or_none(DECISION_RESULT, get("NTST_DCM_DCS_CL_CD")),
        "attributionYear": get("ATTR_YR") or None,
        "registrationDate": format_date(get("NTST_DCM_RGT_DT") or get("DCM_RGT_DTM_S")),
        "summary": get("GIST_CNTN") or None,
        # '공개'가 기본이므로 비공개·부분공개일 때만 표시한다
        "disclosure": disclosure if disclosure and disclosure != "공개" else None,
        # 결정례 검색(05~10)은 불복 결정과 판례가 섞이므로 권위 층위를 항목에 남긴다.
        # 해석례(01~04)는 전부 nts_ruling 이라 생략한다.
        "authorityLevel": str(authority_for_doc_class(doc_class))
        if doc_class in _DECISION_SET
        else None,
        # sourceUrl 은 항목마다 싣지 않는다 — 검색 응답 최상위의 sourceUrlTemplate 에
        # ntstDcmId 를 끼우면 같은 주소가 나온다 (항목당 ~70자 절감).
        "ntstDcmId": doc_id,
    }
    return drop_empty(out)


async def search_documents(
    *,
    doc_classes: list[str],
    query: str | None = None,
    match: str | None = None,
    exclude: list[str] | None = None,
    tax_type_codes: list[str] | None = None,
    issuing_agency_codes: list[str] | None = None,
    decision_result_codes: list[str] | None = None,
    attribution_year: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    date_basis: str = "registration",
    sort: str | None = None,
    page: int = 1,
    limit: int = 20,
) -> dict[str, Any]:
    """목록 검색.

    ``startCount`` 는 오프셋이 아니라 **1부터 시작하는 페이지 번호**다(실측:
    viewCount=5 로 startCount 1 과 2 를 부르면 서로 다른 5건이 온다). 오프셋으로
    오해하면 2페이지를 요청했는데 1페이지가 돌아온다.
    """
    classes = [c for c in doc_classes if c in DOC_CLASS]
    if not classes:
        raise NtsError(ErrorCode.INVALID_INPUT, "조회할 문서구분이 없습니다.")

    # 컬렉션이 갈리므로 해석례와 결정례를 한 요청에 섞을 수 없다.
    collections = {collection_for(c) for c in classes}
    if len(collections) > 1:
        raise NtsError(
            ErrorCode.INVALID_INPUT,
            "세법해석례(01~04)와 판례·결정례(05~10)는 사이트 검색 컬렉션이 달라 "
            "한 번에 조회할 수 없습니다. 도구를 나누어 호출하세요.",
        )

    page = max(1, page)
    limit = min(100, max(1, limit))
    include, excluded = build_vocab(query, match, exclude)
    sort_name = sort or ("relevance" if include else "latest")

    start_date = to_site_date(date_from)
    end_date = to_site_date(date_to)
    if date_from and not start_date:
        raise NtsError(
            ErrorCode.INVALID_INPUT,
            f"date_from 형식이 올바르지 않습니다: {date_from}",
            hints=["YYYY, YYYY-MM, YYYY-MM-DD 또는 구분자 없는 숫자 형식을 사용하세요."],
        )
    if date_to and not end_date:
        raise NtsError(
            ErrorCode.INVALID_INPUT,
            f"date_to 형식이 올바르지 않습니다: {date_to}",
            hints=["YYYY, YYYY-MM, YYYY-MM-DD 또는 구분자 없는 숫자 형식을 사용하세요."],
        )

    param: dict[str, Any] = {
        "startCount": page,
        "viewCount": limit,
        "schDtBase": "FRS_RGT_DTM" if date_basis == "firstRegistration" else "DCM_RGT_DTM",
        "bltnStrtDt": start_date,
        "bltnEndDt": end_date,
        "collectionName": next(iter(collections)),
        "dcmClCdCtl": _to_dcm_cl_cd_ctl(classes),
        "icldVcbCtl": include,
        "exclVcbCtl": excluded,
        "ntstTlawClCdList": tax_type_codes or [],
        "sortField": SORT.get(sort_name, SORT["latest"]),
    }
    if issuing_agency_codes:
        param["dcsThanPrdcOrgnClCtl"] = issuing_agency_codes
    if decision_result_codes:
        param["prtsDcsTypeClCtl_2"] = decision_result_codes
    if attribution_year:
        param["prtsAttrYrCtl"] = [attribution_year]

    payload = await call_action(
        SEARCH_ACTION,
        param,
        referer=f"{NTS_ORIGIN}/qt/USEQTA001M.do?ntstDcmClCd={classes[0]}",
        ttl=TTL.SEARCH,
    )

    top = (payload or {}).get("top") or []
    category_map = (top[0].get("categoryMap") if top else None) or {}
    categories = category_map.get("SUB_ID_CATEGORY") or []
    wanted = set(_to_dcm_cl_cd_ctl(classes))

    counts: list[dict[str, Any]] = []
    total = 0
    for entry in categories:
        name = entry.get("name", "")
        if name not in wanted:
            continue
        code = name.split("_")[-1]
        count = int(entry.get("count") or 0)
        total += count
        # 0건인 문서구분은 항목으로 반복하지 않는다 — total 로 충분하다
        if count:
            counts.append({"code": code, "label": DOC_CLASS.get(code, code), "count": count})

    body = (payload or {}).get("body") or []
    items = [_row_to_summary(row["dcm"]) for row in body if row.get("dcm")]

    return {
        "total": total,
        "countsByDocClass": counts,
        "page": page,
        "limit": limit,
        # 항목별 URL 대신 템플릿 1개 — 컬렉션이 단일 종류로 강제되므로 응답 내 동일하다
        "sourceUrlTemplate": detail_url("{ntstDcmId}", _kind_for(classes[0])),
        "items": items,
    }


async def get_document(
    ntst_dcm_id: str, *, include_full_text: bool = True, body_limit: int | None = None
) -> dict[str, Any]:
    """상세 조회. 문서 종류에 상관없이 액션 하나로 처리된다(01~10 전부 실측 확인).

    본문이 없으면 ``bodyUnavailable`` 을 표시한다 — 절대 "자료 없음"으로 내리지
    않는다. 문서는 존재하는데 본문만 못 얻은 상황을 부존재로 번역하면 안 된다.
    """
    doc_id = str(ntst_dcm_id or "").strip()
    if not doc_id.isdigit() or len(doc_id) < 6:
        raise NtsError(
            ErrorCode.INVALID_INPUT,
            f"ntstDcmId 형식이 아닙니다: {ntst_dcm_id}",
            hints=["ntstDcmId 는 검색 결과의 ntstDcmId 필드 값(숫자 18자리)입니다."],
        )

    payload = await call_action(
        DETAIL_ACTION,
        {"dcmDVO": {"ntstDcmId": doc_id}},
        referer=f"{NTS_ORIGIN}/qt/USEQTA002P.do?ntstDcmId={doc_id}",
        ttl=TTL.DETAIL,
    )

    dvo = (payload or {}).get("dcmDVO")
    if not dvo or not dvo.get("ntstDcmId"):
        raise not_found(
            f"ntstDcmId {doc_id} 에 해당하는 문서를 국세법령정보시스템에서 찾지 못했습니다.",
            ["ntstDcmId 가 검색 결과에서 가져온 값인지 확인하세요."],
        )

    doc_class = str(dvo.get("ntstDcmClCd") or "").strip()
    kind = _kind_for(doc_class)
    document_number = strip_highlight(dvo.get("ntstDcmDscmCntn") or dvo.get("dsbdHpnnNo") or "")
    url = detail_url(doc_id, kind)

    # 본문: HWP 원본을 HTML 로 변환해둔 항목
    hwp_list = (payload or {}).get("dcmHwpEditorDVOList") or []
    html_part = next(
        (x for x in hwp_list if str(x.get("dcmFleTy", "")).lower() == "html"), None
    )
    raw_html = html_part.get("dcmFleByte") if html_part else None
    parsed = parse_body_html(raw_html) if isinstance(raw_html, str) and raw_html else None
    sections = parsed.sections if parsed else {}

    related_laws = list(
        dict.fromkeys(
            name
            for r in (payload or {}).get("dcmRltnStttList") or []
            if (name := str(r.get("ntstTextNm") or "").strip())
        )
    )

    related_documents = [
        strip_highlight(r.get("ntstDcmDscmCntn"))
        for r in ((payload or {}).get("dcmRfrnPrtsList") or [])
        + ((payload or {}).get("dcmQutPrtsList") or [])
        if strip_highlight(r.get("ntstDcmDscmCntn"))
    ]

    matter = dvo.get("ntstDcmMatrCntn")
    keywords = (
        [w.strip() for w in str(matter).replace(",", ";").split(";") if w.strip()]
        if isinstance(matter, str) and matter.strip()
        else None
    )

    def cut(value: str | None) -> str | None:
        return truncate(value, body_limit).text if value else None

    gist = dvo.get("ntstDcmGistCntn")
    gist_text = gist.strip() if isinstance(gist, str) and gist.strip() else None
    answer = dvo.get("ntstDcmCntn")
    answer_text = cut(answer.strip()) if isinstance(answer, str) and answer.strip() else None

    level = authority_for_doc_class(doc_class)
    detail: dict[str, Any] = {
        "domain": _domain_for(doc_class),
        "documentType": DOC_CLASS.get(doc_class) or str(dvo.get("ntstDcmClNm") or ""),
        "documentNumber": document_number,
        "title": strip_highlight(dvo.get("ntstDcmTtl") or ""),
        "taxType": decode_or_none(TAX_TYPE, dvo.get("ntstTlawClCd")),
        "issuingAgency": decode_or_none(ISSUING_AGENCY, dvo.get("ntstDcmSrcsOrgnClCd")),
        "decisionResult": decode_or_none(DECISION_RESULT, dvo.get("ntstDcmDcsClCd")),
        "attributionYear": None if is_blank_code(dvo.get("attrYr")) else str(dvo.get("attrYr")),
        "registrationDate": format_date(dvo.get("ntstDcmRgtDt")),
        "productionDate": format_date(dvo.get("frsRgtDtm")),
        "caseKind": decode_or_none(CASE_KIND, dvo.get("ntstLwsClCd")),
        "lowerCaseNumber": None
        if is_blank_code(dvo.get("ntstPrdgHpnnNoCntn"))
        else strip_highlight(dvo.get("ntstPrdgHpnnNoCntn")),
        "disclosure": decode_or_none(DISCLOSURE, dvo.get("stttInfpClCd")),
        "keywords": keywords,
        "summary": gist_text,
        "answer": answer_text,
        "preamble": cut(parsed.preamble) if parsed else None,
        "relatedLaws": related_laws,
        "relatedDocuments": related_documents,
        "authorityLevel": str(level),
        "authorityNote": AUTHORITY_LABEL[level],
        "ntstDcmId": doc_id,
        "sourceUrl": url,
    }

    # 파서가 인식한 절을 **전부** 응답에 싣는다. 절 이름을 여기서 다시 열거하면
    # 파서 어휘(_SECTION_PATTERNS)에 절이 추가될 때 응답에서 조용히 빠진다.
    for name, text in sections.items():
        key = "relatedLawsText" if name == "relatedLaws" else name
        detail[key] = cut(text)

    # 절이 하나라도 분해됐으면 fullText 를 싣지 않는다 — 절(+preamble)이 제목 줄을
    # 제외한 본문 전체를 담으므로, 전문을 함께 실으면 같은 본문이 두 번 전달된다.
    if parsed and include_full_text and not parsed.sections:
        attach_full_text(detail, parsed.text, body_limit)

    if not parsed:
        detail["bodyUnavailable"] = True
        detail["bodyUnavailableReason"] = (
            "국세법령정보시스템이 이 문서의 본문(HTML)을 제공하지 않았습니다. "
            "메타데이터만 확인된 상태이며, 본문은 sourceUrl 원문에서 확인해야 합니다."
        )

    return drop_empty(detail)
