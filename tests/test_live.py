"""실제 국세법령정보시스템 대상 통합 테스트.

`NTS_LIVE=1 pytest` 로만 돈다. 두 가지를 확인한다.

요구 사양의 검증 케이스가 실제 데이터에서 통과하는지 확인한다. fixture만으로는 알 수
없는 사이트 응답 구조의 변경도 여기서 잡는다.
"""

from __future__ import annotations

import json

import pytest

from korean_taxlaw_mcp.action_client import close_client
from korean_taxlaw_mcp.codes import DECISION_RESULT, DOC_CLASS, ISSUING_AGENCY, TAX_TYPE
from korean_taxlaw_mcp.domains.documents import (
    DECISION_CLASSES,
    INTERPRETATION_CLASSES,
    search_documents,
)
from korean_taxlaw_mcp.query import SORT
from korean_taxlaw_mcp.server import mcp

from .conftest import live

pytestmark = live


@pytest.fixture(autouse=True)
async def _close():
    yield
    await close_client()


async def call(name: str, args: dict) -> tuple[str, dict | None]:
    from fastmcp import Client

    async with Client(mcp) as client:
        result = await client.call_tool(name, args)
    text = result.content[0].text
    label = text.split("]")[0].lstrip("[")
    try:
        return label, json.loads(text[text.index("\n") + 1 :])
    except ValueError:
        return label, None


# ─── 요구 사양의 검증 케이스 ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    "document_number,expected_type,expected_domain",
    [
        ("서면-2026-법규재산-0119", "질의회신", "interpretation"),
        ("서면-2026-법규재산-0109", "질의회신", "interpretation"),
        ("적부-국세청-2026-0119", "과세적부", "decision"),
    ],
)
async def test_required_exact_lookups(document_number, expected_type, expected_domain) -> None:
    label, data = await call(
        "lookup_tax_document", {"document_number": document_number, "body_limit": 2000}
    )
    assert label == "OK", data
    assert data["found"] is True and data["exactMatch"] is True
    assert data["domain"] == expected_domain
    doc = data["document"]
    assert doc["documentNumber"] == document_number
    assert doc["documentType"] == expected_type
    assert doc["title"]
    assert doc["sourceUrl"].startswith("https://taxlaw.nts.go.kr/")
    # 본문이 실제로 붙어 있어야 한다 — 이게 korean-law-mcp 로 안 되는 부분이다.
    # 절 분해가 되면 본문은 절 필드로, 안 되면 fullText 로 온다(중복 전송 없음).
    body_fields = ("fullText", "facts", "question", "reasoning", "conclusion",
                   "claimantView", "agencyView", "relatedLawsText", "answer", "preamble")
    assert any(doc.get(f) for f in body_fields), "본문이 비었다"


async def test_required_variants_resolve_to_same_document() -> None:
    """표기 편차를 실제 서버 조회로 확인한다."""
    seen = set()
    for raw in [
        "서면-2026-법규재산-0119",
        "서면 2026 법규재산 0119",
        "서면2026법규재산0119",
        "질의회신 서면-2026-법규재산-0119",
        "국세청 서면-2026-법규재산-0119",
    ]:
        label, data = await call("lookup_tax_document", {"document_number": raw, "body_limit": 600})
        assert label == "OK", (raw, data)
        seen.add(data["document"]["ntstDcmId"])
    assert len(seen) == 1, f"같은 문서로 수렴하지 않았다: {seen}"


async def test_partial_number_is_never_answered_as_exact() -> None:
    """'법규재산-0119' — 사이트는 여러 건을 주지만 exact 는 없다."""
    label, data = await call("lookup_tax_document", {"document_number": "법규재산-0119"})
    assert label == "NOT_FOUND"
    detail = data["error"]["detail"]
    assert detail["exactMatch"] is False
    assert "document" not in detail
    assert detail["similarDocuments"], "유사문서가 있어야 한다(사이트가 부분일치를 준다)"


async def test_required_keyword_searches() -> None:
    for query in ["법규재산", "상속 공동상속주택"]:
        label, data = await call("search_tax_interpretations", {"query": query, "limit": 5})
        assert label == "OK", (query, data)
        assert data["total"] > 0
        assert data["items"]


# ─── 검색 문법이 사이트에서 실제로 그 의미인지 ─────────────────────────────────

async def test_and_or_not_set_algebra_holds_upstream() -> None:
    """AND/OR/NOT 이 집합 연산과 정확히 맞아야 한다.

    OR 를 broken bar(¦)로 잘못 구현하면 AND 와 같은 값이 나와 이 검증이 깨진다.
    """
    classes = list(INTERPRETATION_CLASSES)

    async def total(**kwargs) -> int:
        return (await search_documents(doc_classes=classes, limit=1, **kwargs))["total"]

    a = await total(query="상속")
    b = await total(query="증여")
    both_and = await total(query="상속 증여", match="all")
    both_or = await total(query="상속 증여", match="any")
    a_not_b = await total(query="상속", exclude=["증여"])

    assert both_and < min(a, b), "AND 가 교집합이 아니다"
    assert both_or == a + b - both_and, f"OR 가 합집합이 아니다: {both_or} != {a}+{b}-{both_and}"
    assert a_not_b == a - both_and, f"NOT 이 차집합이 아니다: {a_not_b} != {a}-{both_and}"


@pytest.mark.parametrize("sort_name", sorted(SORT))
async def test_all_sort_tokens_still_return_rows(sort_name: str) -> None:
    """허용되지 않는 정렬 필드는 오류가 아니라 조용한 0건을 낳는다."""
    result = await search_documents(
        doc_classes=list(INTERPRETATION_CLASSES), query="상속", sort=sort_name, limit=3
    )
    assert result["total"] > 0, f"sort={sort_name} 에서 0건 — 정렬 토큰이 무효해졌다"
    assert result["items"]


async def test_pagination_returns_distinct_pages() -> None:
    """startCount 는 페이지 번호다. 오프셋으로 오해하면 같은 결과가 반복된다."""
    first = await search_documents(doc_classes=["02"], page=1, limit=5, sort="latest")
    second = await search_documents(doc_classes=["02"], page=2, limit=5, sort="latest")
    ids1 = {i["ntstDcmId"] for i in first["items"]}
    ids2 = {i["ntstDcmId"] for i in second["items"]}
    assert ids1 and ids2
    assert not (ids1 & ids2), "1·2 페이지가 겹친다"


# ─── 모든 문서구분이 여전히 조회되는지 ─────────────────────────────────────────

@pytest.mark.parametrize("doc_class", list(INTERPRETATION_CLASSES) + list(DECISION_CLASSES))
async def test_every_document_class_is_reachable(doc_class: str) -> None:
    result = await search_documents(doc_classes=[doc_class], limit=2, sort="latest")
    assert result["total"] > 0, f"{doc_class}({DOC_CLASS[doc_class]}) 0건"
    assert result["items"]
    assert result["items"][0]["documentNumber"]


async def test_mixing_collections_is_rejected() -> None:
    """해석례와 결정례는 컬렉션이 달라 한 요청에 섞을 수 없다."""
    from korean_taxlaw_mcp.errors import ErrorCode, NtsError

    with pytest.raises(NtsError) as exc:
        await search_documents(doc_classes=["02", "08"], query="상속")
    assert exc.value.code == ErrorCode.INVALID_INPUT


# ─── 상세 조회 · 절 분해가 실제 데이터에서 유지되는지 ──────────────────────────

@pytest.mark.parametrize(
    "document_number,required_sections",
    [
        ("서면-2026-법규재산-0119", {"facts", "question"}),
        ("적부-국세청-2026-0119", {"facts", "claimantView", "agencyView", "reasoning", "conclusion"}),
        ("조심-2025-인-4460", {"facts", "reasoning", "conclusion"}),
    ],
)
async def test_section_split_survives_on_live_data(document_number, required_sections) -> None:
    label, data = await call(
        "lookup_tax_document", {"document_number": document_number, "body_limit": 60_000}
    )
    assert label == "OK", data
    doc = data["document"]
    missing = {s for s in required_sections if not doc.get(s)}
    assert not missing, f"{document_number}: 절 누락 {missing}"


# ─── 행정 해석기준 · 서식 ──────────────────────────────────────────────────────

async def test_basic_ruling_live() -> None:
    label, data = await call(
        "search_tax_guidance",
        {"kind": "basic_ruling", "law_name": "상속세 및 증여세법", "query": "상속재산", "limit": 5},
    )
    assert label == "OK", data
    assert data["items"]
    assert any(i.get("text") for i in data["items"]), "기본통칙 본문이 사라졌다"


async def test_execution_standard_live_still_lacks_body() -> None:
    """원본이 본문을 주기 시작하면 안내 문구를 고쳐야 한다."""
    label, data = await call(
        "search_tax_guidance",
        {"kind": "execution_standard", "law_name": "상속증여세 집행기준", "limit": 5},
    )
    assert label == "OK", data
    assert data["items"]
    assert all("text" not in i for i in data["items"])


@pytest.mark.parametrize("kind", ["notice", "directive"])
async def test_notice_directive_live(kind: str) -> None:
    label, data = await call("search_tax_guidance", {"kind": kind, "limit": 5})
    assert label == "OK", data
    assert data["total"] > 0
    assert data["items"]


async def test_forms_live() -> None:
    label, data = await call("search_tax_forms", {"query": "상속세", "limit": 5})
    assert label == "OK", data
    assert data["items"]
    assert data["items"][0]["formName"]


# ─── 코드표가 원본과 여전히 일치하는지 ─────────────────────────────────────────

async def test_code_tables_still_match_upstream() -> None:
    """코드표를 상수로 고정했으므로 원본이 바뀌면 즉시 알아야 한다."""
    from korean_taxlaw_mcp.action_client import call_action

    groups_raw = await call_action(
        "ACMCMA001MR01",
        {"cmCodeDVOList": [{"cmnClsfCd": c} for c in ("19378", "19387", "19425", "19375")]},
    )
    groups = {g["cmnClsfCd"]: g["cmCodeDVOList"] for g in groups_raw if g}
    for group_id, table in (
        ("19378", DOC_CLASS), ("19425", ISSUING_AGENCY), ("19375", DECISION_RESULT),
    ):
        live_codes = {c["cdVval"]: c["cdVvalKrnNm"] for c in groups[group_id]}
        for code, label in table.items():
            assert live_codes.get(code) == label, f"{group_id}/{code}: 상수={label} 원본={live_codes.get(code)}"
    live_tax = {c["cdVval"]: c["cdVvalKrnNm"] for c in groups["19387"]}
    for code, label in TAX_TYPE.items():
        if code == "999":
            continue
        assert live_tax.get(code) == label, f"세목 {code}: 상수={label} 원본={live_tax.get(code)}"


# ─── 층별 근거 수집 ────────────────────────────────────────────────────────────

async def test_tax_research_live() -> None:
    label, data = await call(
        "tax_research",
        {"question": "부모가 자녀에게 시가보다 낮은 가격으로 아파트를 양도하면 증여세가 발생하는지",
         "limit_per_layer": 3},
    )
    assert label == "OK", data
    layers = {L["layer"]: L for L in data["layers"]}
    # 법령 층(법률·시행령·시행규칙은 한 층으로 병합)은 범위 밖임을 명시해야 한다
    statute = layers["법률·시행령·시행규칙"]
    assert statute["status"] == "not_covered"
    assert "korean-law-mcp" in statute["message"]
    # 국세청 자료 층은 실제로 결과가 있어야 한다
    assert layers["국세청 해석례"]["status"] == "found"
    assert layers["국세청 해석례"]["total"] > 0
    # 판단을 만들지 않는다는 경고가 실려야 한다
    assert "법률적 판단" in data["disclaimer"]


# ─────────────────────────────────────────────────────────────────────────────
# 지방세 (한국지방세연구원 지방세 법령정보시스템)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _close_olta():
    yield
    from korean_taxlaw_mcp.olta_client import close_client as close_olta

    await close_olta()


LOCAL_KINDS = ["interpretation", "moleg", "tribunal", "audit", "court", "constitutional"]


@pytest.mark.parametrize("kind", LOCAL_KINDS)
async def test_local_every_source_is_reachable(kind: str) -> None:
    """6종 전부 검색이 되고 행이 파싱돼야 한다.

    menuNo 누락 시 법원 판례가 500 을 내는 등 종류별 함정이 있어 전부 확인한다.
    """
    from korean_taxlaw_mcp.domains.local_tax import search_local_documents

    result = await search_local_documents(kinds=[kind], query="취득세", limit=5)
    assert result["total"] > 0, f"{kind}: 0건"
    assert result["items"], f"{kind}: 행 파싱 실패"
    assert all(item["documentNumber"] for item in result["items"])


async def test_local_tax_filter_actually_filters() -> None:
    """세목 필터는 `taxTitleStr` 로만 걸린다. 체크박스 이름을 보내면 무동작이다.

    부분합이 합계와 맞는지로 필터가 실제로 동작하는지 확인한다.
    """
    from korean_taxlaw_mcp.domains.local_tax import search_local_documents

    async def total(codes: list[str] | None) -> int:
        result = await search_local_documents(
            kinds=["interpretation"], query="신탁", tax_type_codes=codes, limit=1
        )
        return result["total"]

    acquisition = await total(["11100"])   # 취득세
    property_tax = await total(["24000"])  # 재산세
    both = await total(["11100", "24000"])
    assert acquisition > 0 and property_tax > 0
    assert both == acquisition + property_tax, f"{both} != {acquisition}+{property_tax}"


async def test_local_search_operators_behave_as_documented() -> None:
    """공백=AND, `|`=OR, `!`=NOT. 문서와 어긋나면 검색이 조용히 틀린다."""
    from korean_taxlaw_mcp.domains.local_tax import search_local_documents

    async def total(query: str) -> int:
        result = await search_local_documents(kinds=["interpretation"], query=query, limit=1)
        return result["total"]

    a = await total("취득세")
    b = await total("신탁")
    both_and = await total("취득세 신탁")
    explicit_and = await total("취득세&신탁")
    both_or = await total("취득세|신탁")

    assert both_and == explicit_and, "공백이 AND 가 아니다"
    assert both_and < min(a, b), "AND 가 교집합이 아니다"
    assert both_or > max(a, b), "OR 가 합집합이 아니다"


async def test_local_exact_document_number_lookup() -> None:
    from korean_taxlaw_mcp.domains.local_tax import lookup_local_by_document_number

    for raw in ["부동산세제과-1794(2026.6.9.)호", "부동산세제과-1794", "부동산세제과 1794"]:
        outcome = await lookup_local_by_document_number(
            raw, kinds=["interpretation"], include_full_text=False, body_limit=1000
        )
        assert outcome["found"] is True, raw
        assert outcome["exactMatch"] is True, raw
        document = outcome["document"]
        assert document["documentNumber"].startswith("부동산세제과-1794")
        assert document["authorityLevel"] == "local_ruling"
        assert document["sourceUrl"].startswith("https://www.olta.re.kr/")


async def test_local_partial_serial_is_never_answered_as_exact() -> None:
    """`924` 는 사이트가 `4924`·`2924` 까지 준다. exact 로 답하면 오답이다."""
    from korean_taxlaw_mcp.domains.local_tax import lookup_local_by_document_number

    outcome = await lookup_local_by_document_number(
        "924", kinds=["interpretation"], include_full_text=False
    )
    assert outcome["found"] is False
    assert outcome["exactMatch"] is False
    assert "document" not in outcome
    assert outcome["similarDocuments"], "유사문서가 있어야 한다"


@pytest.mark.parametrize("kind", LOCAL_KINDS)
async def test_local_list_to_detail_round_trip(kind: str) -> None:
    """목록에서 얻은 ID 로 상세가 실제로 열려야 한다.

    법원 판례는 `relationshipNum` 과 `srchWrd` 가 함께 가야 열린다(없으면 500).
    """
    from korean_taxlaw_mcp.domains.local_tax import get_local_document, search_local_documents

    result = await search_local_documents(kinds=[kind], query="취득세", limit=1)
    assert result["items"], kind
    row = result["items"][0]
    document = await get_local_document(
        kind,
        row["documentId"],
        relationship_num=row.get("relationshipNum"),
        fallback_document_number=row.get("documentNumber"),
        body_limit=2000,
    )
    assert document["documentNumber"], kind
    # 절 분해가 되면 본문은 절 필드로, 안 되면 fullText 로 온다(중복 전송 없음).
    body_fields = ("fullText", "summary", "question", "answer", "reasoning", "body", "preamble")
    assert any(document.get(f) for f in body_fields), f"{kind}: 본문 없음"
    assert document["sourceUrl"].startswith("https://www.olta.re.kr/")


async def test_local_tools_via_mcp_client() -> None:
    """지방세 도구가 MCP 경로로 동작하는지."""
    label, data = await call(
        "search_local_tax_interpretations", {"query": "신탁", "tax_type": "취득세", "limit": 3}
    )
    assert label == "OK", data
    assert data["taxLevel"] == "local"
    assert data["items"]
    # 검색 요약은 후보 목록 — 본문·상수 boilerplate 를 싣지 않는다
    assert all("fullText" not in item and item.get("kind") for item in data["items"])

    label, data = await call("lookup_local_tax_document", {"document_number": "924"})
    assert label == "NOT_FOUND"
    assert data["error"]["detail"]["exactMatch"] is False
    assert data["error"]["detail"]["similarDocuments"]
    assert "추측" in data["guardrail"]
