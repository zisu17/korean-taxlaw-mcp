"""MCP 도구 통합 테스트.

업스트림을 fixture로 대신해 네트워크 없이 도구 계약을 검증한다.
남의 공개 서비스를 매 테스트마다 두드리지 않고도 회귀를 잡을 수 있어야 한다.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from korean_taxlaw_mcp.action_client import ACTION_URL, close_client
from korean_taxlaw_mcp.cache import cache
from korean_taxlaw_mcp.server import TOOL_NAMES, mcp

from .conftest import load, requires_fixtures

#: 업스트림을 fixture 로 대신하므로 fixture 가 있어야 한다
pytestmark = requires_fixtures


def _envelope(action_id: str, payload) -> dict:
    return {"status": "SUCCESS", "message": None, "data": {action_id: payload}}


class Upstream:
    """action.do 를 fixture 로 응답한다. actionId·paramData 를 보고 골라준다."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.search_by_query: dict[str, str] = {}
        self.detail_by_id: dict[str, str] = {}
        #: fixture 를 손봐서 돌려주고 싶을 때(본문 제거 등) 쓰는 직접 주입 경로
        self.detail_payload: dict[str, dict] = {}
        self.default_search: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        action_id = form["actionId"]
        param = json.loads(form["paramData"])
        self.calls.append((action_id, param))

        if action_id == "ASIPDI002PR01":
            include = param.get("icldVcbCtl") or []
            key = " ".join(include)
            name = self.search_by_query.get(key, self.default_search)
            if name is None:
                return httpx.Response(200, json=_envelope(action_id, {"top": [], "body": []}))
            return httpx.Response(200, json=_envelope(action_id, load(name)))

        if action_id == "ASIQTB002PR01":
            doc_id = param["dcmDVO"]["ntstDcmId"]
            if doc_id in self.detail_payload:
                return httpx.Response(200, json=_envelope(action_id, self.detail_payload[doc_id]))
            name = self.detail_by_id.get(doc_id)
            if name is None:
                return httpx.Response(200, json=_envelope(action_id, {"dcmDVO": None}))
            return httpx.Response(200, json=_envelope(action_id, load(name)))

        for prefix, fixture_name in (
            ("ASISTD001MR01", "guidance_basic_ruling_laws"),
            ("ASISTD001MR03", "guidance_basic_ruling_years"),
            ("ASISTD001MR02", "guidance_basic_ruling_items"),
            ("ASISTE001MR03", "guidance_exec_years"),
            ("ASISTE001MR02", "guidance_exec_items"),
            ("ASIAFB001MR01", "forms_search"),
        ):
            if action_id == prefix:
                return httpx.Response(200, json=_envelope(action_id, load(fixture_name)))
        if action_id == "ASISTF001MR01":
            name = "guidance_notice" if param.get("ntarClCd") == "01" else "guidance_directive"
            return httpx.Response(200, json=_envelope(action_id, load(name)))

        return httpx.Response(200, json=_envelope(action_id, {}))


@pytest.fixture
async def upstream():
    cache.clear()
    up = Upstream()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(ACTION_URL).mock(side_effect=up.handler)
        yield up
    cache.clear()
    await close_client()


async def call(name: str, args: dict) -> tuple[str, dict | None]:
    """도구를 부르고 (라벨, JSON) 을 돌려준다."""
    from fastmcp import Client

    async with Client(mcp) as client:
        result = await client.call_tool(name, args)
    text = result.content[0].text
    label = text.split("]")[0].lstrip("[")
    try:
        return label, json.loads(text[text.index("\n") + 1 :])
    except ValueError:
        return label, None


# ─── 도구 목록 계약 ────────────────────────────────────────────────────────────

async def test_tools_are_registered_and_bounded() -> None:
    """도구 폭발을 막는다 — 자료 종류마다 도구를 만들면 모델이 선택을 헷갈린다."""
    from fastmcp import Client

    async with Client(mcp) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert names == set(TOOL_NAMES)
    assert len(names) <= 12, "도구 수가 늘었다 — 통합 도구 설계를 다시 보라"
    for tool in tools:
        assert tool.description and len(tool.description) > 60
        assert tool.inputSchema["type"] == "object"


# ─── lookup: exact 만 정답 ─────────────────────────────────────────────────────

async def test_lookup_returns_exact_match(upstream) -> None:
    upstream.search_by_query["서면-2026-법규재산-0119"] = "search_docnumber_exact"
    upstream.detail_by_id["200000000000022584"] = "detail_written"

    label, data = await call("lookup_tax_document", {"document_number": "서면-2026-법규재산-0119"})
    assert label == "OK"
    assert data["found"] is True and data["exactMatch"] is True
    doc = data["document"]
    assert doc["documentNumber"] == "서면-2026-법규재산-0119"
    assert doc["documentType"] == "질의회신"
    assert doc["authorityLevel"] == "nts_ruling"
    assert doc["sourceUrl"].endswith("ntstDcmId=200000000000022584")
    assert "조세특례제한법 제71조의2" in doc["relatedLaws"]
    # 출처 중복 블록(citation)과 성공 시 진단 메타데이터는 싣지 않는다 (토큰 예산)
    assert "citation" not in doc
    assert "triedQueries" not in data and "inputInterpretation" not in data


async def test_lookup_normalizes_all_input_variants(upstream) -> None:
    """표기가 달라도 같은 문서에 도달해야 한다."""
    upstream.default_search = "search_docnumber_exact"
    upstream.detail_by_id["200000000000022584"] = "detail_written"

    for raw in ["서면 2026 법규재산 0119", "서면2026법규재산0119",
                "질의회신 서면-2026-법규재산-0119", "국세청 서면-2026-법규재산-0119"]:
        label, data = await call("lookup_tax_document", {"document_number": raw})
        assert label == "OK", raw
        assert data["exactMatch"] is True, raw
        assert data["document"]["documentNumber"] == "서면-2026-법규재산-0119", raw


async def test_partial_number_returns_not_found_with_similar_separated(upstream) -> None:
    """부분일치를 정답처럼 반환하면 세무 상담에서 곧바로 오답이 된다."""
    upstream.default_search = "search_docnumber_partial"

    label, data = await call("lookup_tax_document", {"document_number": "법규재산-0119"})
    assert label == "NOT_FOUND"
    assert data["ok"] is False
    detail = data["error"]["detail"]
    assert detail["exactMatch"] is False
    # 유사 문서는 반드시 분리된 자리에 있어야 한다
    assert detail["similarDocuments"]
    assert "document" not in detail
    numbers = {s["documentNumber"] for s in detail["similarDocuments"]}
    assert "서면-2026-법규재산-0119" in numbers  # 있지만 정답으로 제시되지 않는다
    # 모델에게 지어내지 말라고 못 박는 문구가 실려야 한다
    assert "추측" in data["guardrail"] and "정답으로 제시하지" in data["guardrail"]


async def test_lookup_missing_document_is_not_found(upstream) -> None:
    upstream.default_search = None  # 검색 결과 없음
    label, data = await call("lookup_tax_document", {"document_number": "서면-2026-법규재산-9999"})
    assert label == "NOT_FOUND"
    assert data["error"]["detail"]["similarDocuments"] == []


# ─── 검색 도구 ────────────────────────────────────────────────────────────────

async def test_search_interpretations_shape(upstream) -> None:
    upstream.default_search = "search_written"
    label, data = await call("search_tax_interpretations", {"query": "공동상속주택"})
    assert label == "OK"
    assert data["domain"] == "interpretation"
    assert data["items"]
    # 항목별 URL 대신 응답에 템플릿 하나 — ntstDcmId 를 끼우면 상세 화면 주소가 된다
    assert "{ntstDcmId}" in data["sourceUrlTemplate"]
    for item in data["items"]:
        # 검색 결과는 후보 목록이다 — 본문·상수 boilerplate 를 싣지 않는다
        for banned in ("fullText", "facts", "reasoning", "citation", "authorityLevel", "sourceUrl"):
            assert banned not in item, f"검색 결과에 {banned} 가 실렸다"
        assert item["ntstDcmId"]


async def test_search_sends_and_semantics_to_upstream(upstream) -> None:
    upstream.default_search = "search_written"
    await call("search_tax_interpretations", {"query": "상속 공동상속주택"})
    _action, param = next(c for c in upstream.calls if c[0] == "ASIPDI002PR01")
    assert param["icldVcbCtl"] == ["상속", "공동상속주택"]


async def test_search_sends_or_semantics_to_upstream(upstream) -> None:
    upstream.default_search = "search_written"
    await call("search_tax_interpretations", {"query": "상속 증여", "match": "any"})
    _action, param = next(c for c in upstream.calls if c[0] == "ASIPDI002PR01")
    assert param["icldVcbCtl"] == ["상속|증여"]


async def test_search_sends_exclude_as_not(upstream) -> None:
    upstream.default_search = "search_written"
    await call("search_tax_interpretations", {"query": "상속", "exclude": ["증여"]})
    _action, param = next(c for c in upstream.calls if c[0] == "ASIPDI002PR01")
    assert param["exclVcbCtl"] == ["증여"]


async def test_page_is_page_number_not_offset(upstream) -> None:
    """startCount 는 오프셋이 아니라 1부터 시작하는 페이지 번호다."""
    upstream.default_search = "search_written"
    await call("search_tax_interpretations", {"query": "상속", "page": 3, "limit": 10})
    _action, param = next(c for c in upstream.calls if c[0] == "ASIPDI002PR01")
    assert param["startCount"] == 3
    assert param["viewCount"] == 10


async def test_interpretation_and_decision_use_different_collections(upstream) -> None:
    """컬렉션을 잘못 주면 0건이 온다."""
    upstream.default_search = "search_written"
    await call("search_tax_interpretations", {"query": "상속"})
    _a, interp_param = next(c for c in upstream.calls if c[0] == "ASIPDI002PR01")
    assert interp_param["collectionName"] == "question,question_gr"

    upstream.calls.clear()
    upstream.default_search = "search_tribunal"
    await call("search_tax_decisions", {"query": "상속"})
    _a, dec_param = next(c for c in upstream.calls if c[0] == "ASIPDI002PR01")
    assert dec_param["collectionName"] == "precedent,precedent_gr"


async def test_decision_type_selects_single_class(upstream) -> None:
    upstream.default_search = "search_tribunal"
    await call("search_tax_decisions", {"query": "상속", "type": "tribunal"})
    _a, param = next(c for c in upstream.calls if c[0] == "ASIPDI002PR01")
    assert param["dcmClCdCtl"] == ["001_08"]


async def test_decision_result_filter_is_translated_to_codes(upstream) -> None:
    upstream.default_search = "search_court"
    await call("search_tax_decisions", {"query": "상속", "result": ["국승", "일부국패"]})
    _a, param = next(c for c in upstream.calls if c[0] == "ASIPDI002PR01")
    assert set(param["prtsDcsTypeClCtl_2"]) == {"10", "12"}


async def test_decision_result_can_be_the_only_search_condition(upstream) -> None:
    upstream.default_search = "search_review"
    label, _data = await call("search_tax_decisions", {"result": ["기각"]})
    assert label == "OK"
    _action, param = upstream.calls[-1]
    assert param["prtsDcsTypeClCtl_2"] == ["05"]


async def test_unknown_decision_result_is_invalid_input(upstream) -> None:
    label, data = await call("search_tax_decisions", {"query": "상속", "result": ["승소"]})
    assert label == "INVALID_INPUT"
    assert "승소" in data["error"]["message"]
    assert upstream.calls == []


async def test_invalid_date_does_not_fall_back_to_unfiltered_search(upstream) -> None:
    label, data = await call("search_tax_interpretations", {"date_from": "not-a-date"})
    assert label == "INVALID_INPUT"
    assert data["error"]["code"] == "INVALID_INPUT"
    assert upstream.calls == []


async def test_search_with_no_criteria_is_rejected(upstream) -> None:
    label, data = await call("search_tax_interpretations", {})
    assert label == "INVALID_INPUT"
    assert data["ok"] is False


async def test_document_number_in_search_triggers_exact_lookup(upstream) -> None:
    upstream.default_search = "search_docnumber_exact"
    upstream.detail_by_id["200000000000022584"] = "detail_written"
    label, data = await call(
        "search_tax_interpretations", {"document_number": "서면-2026-법규재산-0119"}
    )
    assert label == "OK"
    assert data["exactMatch"] is True


async def test_empty_search_result_is_not_found_with_hints(upstream) -> None:
    upstream.default_search = None
    label, data = await call("search_tax_interpretations", {"query": "존재하지않는키워드조합"})
    assert label == "NOT_FOUND"
    assert any("AND" in h for h in data["error"]["hints"])


# ─── 상세 조회 ────────────────────────────────────────────────────────────────

async def test_get_document_by_id_splits_sections(upstream) -> None:
    upstream.detail_by_id["200000000000021979"] = "detail_pre_assessment"
    label, data = await call("get_tax_document", {"ntst_dcm_id": "200000000000021979"})
    assert label == "OK"
    doc = data["document"]
    assert doc["documentNumber"] == "적부-국세청-2026-0119"
    assert doc["decisionResult"] == "불채택"
    assert doc["authorityLevel"] == "adjudication"
    for section in ("facts", "claimantView", "agencyView", "reasoning", "conclusion"):
        assert doc[section], f"{section} 누락"


async def test_get_document_rejects_bad_id(upstream) -> None:
    label, data = await call("get_tax_document", {"ntst_dcm_id": "abc"})
    assert label == "INVALID_INPUT"


async def test_missing_body_reports_detail_not_available_not_not_found(upstream) -> None:
    """본문 부재를 '자료 없음' 으로 내리면 실재하는 문서를 없다고 답하게 된다."""
    stripped = load("detail_written")
    stripped["dcmHwpEditorDVOList"] = [
        x for x in stripped["dcmHwpEditorDVOList"] if str(x["dcmFleTy"]).lower() != "html"
    ]
    upstream.detail_payload["200000000000022584"] = stripped

    label, data = await call("get_tax_document", {"ntst_dcm_id": "200000000000022584"})
    assert label == "DETAIL_NOT_AVAILABLE"
    assert data["error"]["code"] == "DETAIL_NOT_AVAILABLE"
    # 문서 메타데이터는 함께 주되, 본문을 지어내지 말라고 명시한다
    assert data["error"]["detail"]["document"]["documentNumber"] == "서면-2026-법규재산-0119"
    assert "추측" in data["guardrail"]


# ─── 행정 해석기준 · 서식 ──────────────────────────────────────────────────────

async def test_basic_ruling_returns_body_and_authority_warning(upstream) -> None:
    label, data = await call(
        "search_tax_guidance",
        {"kind": "basic_ruling", "law_name": "상속세 및 증여세법", "query": "상속재산"},
    )
    assert label == "OK"
    assert data["items"]
    assert any(i.get("text") for i in data["items"])
    # 권위 층위는 응답에 한 번만 싣는다 — 항목마다 반복하지 않는다
    assert data["authorityLevel"] == "nts_guidance"
    assert all("authorityLevel" not in i for i in data["items"])
    assert "법규가 아닌" in data["authorityWarning"]


async def test_execution_standard_declares_missing_body(upstream) -> None:
    """집행기준 본문은 원본이 주지 않는다 — 지어내지 않고 사유를 밝힌다."""
    label, data = await call(
        "search_tax_guidance", {"kind": "execution_standard", "law_name": "상속증여세 집행기준"}
    )
    assert label == "OK"
    assert data["items"]
    assert all("text" not in i for i in data["items"])
    # 본문 부재 사유는 항목마다 반복하지 않고 응답에 한 번만 싣는다
    assert "PDF" in data["textUnavailableReason"]
    assert all("textUnavailableReason" not in i for i in data["items"])


async def test_notice_requires_all_subject_code(upstream) -> None:
    """ntstSjtClCd='All' 을 빼면 사이트가 0건을 준다."""
    label, data = await call("search_tax_guidance", {"kind": "notice"})
    assert label == "OK"
    _a, param = next(c for c in upstream.calls if c[0] == "ASISTF001MR01")
    assert param["ntstSjtClCd"] == "All"


async def test_guidance_without_law_name_is_rejected(upstream) -> None:
    label, data = await call("search_tax_guidance", {"kind": "basic_ruling"})
    assert label == "INVALID_INPUT"
    assert any("조회 가능한 대상" in h for h in data["error"]["hints"])


async def test_forms_use_search_prefixed_param(upstream) -> None:
    """파라미터가 ntstBscId 가 아니라 searchNtstBscId 다 — 틀리면 0건이 온다."""
    label, data = await call("search_tax_forms", {"query": "상속세"})
    assert label == "OK"
    _a, param = next(c for c in upstream.calls if c[0] == "ASIAFB001MR01")
    assert "searchNtstBscId" in param
    # 공통값은 응답 최상위에 한 번만 — 항목마다 반복하지 않는다
    assert data["authorityLevel"] == "enforcement_rule"
    assert "POST" in data["downloadNote"]
    assert all("downloadNote" not in i and "citation" not in i for i in data["items"])


# ─── 통합 검색 ────────────────────────────────────────────────────────────────

async def test_search_taxlaw_strips_routing_words(upstream) -> None:
    upstream.default_search = "search_written"
    label, data = await call("search_taxlaw", {"query": "국세청 예규 공동상속주택"})
    assert label == "OK"
    assert data["searchQuery"] == "공동상속주택"
    # 세목을 명시하지 않으면 필터를 걸지 않고, 그 사실을 필드 부재로 나타낸다
    assert "taxTypeFilterApplied" not in data


async def test_search_taxlaw_prefers_exact_document_number(upstream) -> None:
    upstream.default_search = "search_docnumber_exact"
    upstream.detail_by_id["200000000000022584"] = "detail_written"
    label, data = await call("search_taxlaw", {"query": "서면-2026-법규재산-0119 요지 알려줘"})
    assert label == "OK"
    assert data["resolvedBy"] == "documentNumber"
    assert data["exactMatch"] is True


# ─── 오류 전달 ────────────────────────────────────────────────────────────────

async def test_upstream_failure_is_not_reported_as_missing_data() -> None:
    """장애를 부존재로 번역하면 실재하는 예규를 없다고 답하게 된다."""
    cache.clear()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(ACTION_URL).mock(return_value=httpx.Response(503, text="down"))
        label, data = await call("search_tax_interpretations", {"query": "상속"})
    assert label == "UPSTREAM_ERROR"
    assert data["error"]["code"] == "UPSTREAM_ERROR"
    assert "부존재로 단정하지 말고" in data["guardrail"]
    await close_client()


async def test_exact_lookup_upstream_failure_is_not_reported_as_not_found() -> None:
    cache.clear()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(ACTION_URL).mock(return_value=httpx.Response(503, text="down"))
        label, data = await call(
            "lookup_tax_document", {"document_number": "서면-2026-법규재산-0119"}
        )
    assert label == "UPSTREAM_ERROR"
    assert data["error"]["code"] == "UPSTREAM_ERROR"
    await close_client()


async def test_integrated_search_all_failures_are_not_reported_as_not_found() -> None:
    cache.clear()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(ACTION_URL).mock(return_value=httpx.Response(503, text="down"))
        label, data = await call("search_taxlaw", {"query": "상속 공동상속주택"})
    assert label == "UPSTREAM_ERROR"
    assert data["error"]["code"] == "UPSTREAM_ERROR"
    await close_client()


async def test_maintenance_html_page_is_upstream_error_not_parse_success() -> None:
    """점검 페이지는 200 + HTML 로 온다. JSON 인 척 넘어가면 안 된다."""
    cache.clear()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(ACTION_URL).mock(
            return_value=httpx.Response(200, text="<html><body>점검중</body></html>")
        )
        label, data = await call("search_tax_interpretations", {"query": "상속"})
    assert label == "UPSTREAM_ERROR"
    await close_client()
