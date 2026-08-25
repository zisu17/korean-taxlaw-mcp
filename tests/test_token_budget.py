"""LLM 컨텍스트 토큰 예산 회귀 테스트.

이 서버 응답은 LLM 컨텍스트에 그대로 실린다. 여기서 지키는 계약:

1. 검색 응답에는 본문이 실리지 않는다 (요약 후보 목록만).
2. 검색 기본 limit 은 10 이다 — 명시 요청 없이 20건씩 반환하지 않는다.
3. 상세 조회는 본문을 절(sections)로 반환하고, 절이 본문을 다 담으면
   fullText 를 중복으로 싣지 않는다. 내용은 유실되지 않는다.
4. 응답에 null / 빈 문자열 / 빈 배열을 싣지 않는다.
5. 성공 응답에 citation·triedQueries 같은 중복·진단 블록을 싣지 않는다.
6. JSON 은 들여쓰기 없이 직렬화한다.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from korean_taxlaw_mcp.action_client import ACTION_URL, close_client
from korean_taxlaw_mcp.cache import cache
from korean_taxlaw_mcp.html_text import parse_body_html
from korean_taxlaw_mcp.server import mcp

from .conftest import load, requires_fixtures

pytestmark = requires_fixtures


def _envelope(action_id: str, payload) -> dict:
    return {"status": "SUCCESS", "message": None, "data": {action_id: payload}}


class Upstream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.default_search: str | None = None
        self.detail_by_id: dict[str, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        action_id = form["actionId"]
        param = json.loads(form["paramData"])
        self.calls.append((action_id, param))
        if action_id == "ASIPDI002PR01":
            if self.default_search is None:
                return httpx.Response(200, json=_envelope(action_id, {"top": [], "body": []}))
            return httpx.Response(200, json=_envelope(action_id, load(self.default_search)))
        if action_id == "ASIQTB002PR01":
            name = self.detail_by_id.get(param["dcmDVO"]["ntstDcmId"])
            if name is None:
                return httpx.Response(200, json=_envelope(action_id, {"dcmDVO": None}))
            return httpx.Response(200, json=_envelope(action_id, load(name)))
        for aid, name in (
            ("ASISTD001MR01", "guidance_basic_ruling_laws"),
            ("ASISTD001MR03", "guidance_basic_ruling_years"),
            ("ASISTD001MR02", "guidance_basic_ruling_items"),
        ):
            if action_id == aid:
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


async def call(name: str, args: dict) -> tuple[str, dict]:
    from fastmcp import Client

    async with Client(mcp) as client:
        result = await client.call_tool(name, args)
    text = result.content[0].text
    label = text.split("]")[0].lstrip("[")
    return label, json.loads(text[text.index("\n") + 1 :])


def _walk(value, path=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, value


def assert_no_empty_values(payload: dict) -> None:
    for path, value in _walk(payload):
        assert value is not None, f"null 필드: {path}"
        assert value != "", f"빈 문자열 필드: {path}"
    for path, value in _iter_containers(payload):
        assert value != [] and value != {}, f"빈 컨테이너: {path}"


def _iter_containers(value, path=""):
    if isinstance(value, dict):
        yield path, value
        for k, v in value.items():
            yield from _iter_containers(v, f"{path}.{k}")
    elif isinstance(value, list):
        yield path, value
        for i, v in enumerate(value):
            yield from _iter_containers(v, f"{path}[{i}]")


# ─── 1. 검색 응답에 본문 없음 ─────────────────────────────────────────────────

async def test_search_results_carry_no_body(upstream) -> None:
    upstream.default_search = "search_written"
    label, data = await call("search_tax_interpretations", {"query": "분양권"})
    assert label == "OK"
    for item in data["items"]:
        for banned in ("fullText", "facts", "question", "reasoning", "conclusion",
                       "answer", "citation", "authorityNote"):
            assert banned not in item, f"검색 결과 항목에 {banned}"
        # 요약(요지)은 후보 선택에 필요하므로 허용하되 본문 길이가 아니어야 한다
        assert len(item.get("summary", "")) < 1_000


# ─── 2. 기본 limit ───────────────────────────────────────────────────────────

async def test_search_default_limit_matches_config(upstream) -> None:
    from korean_taxlaw_mcp.config import DEFAULT_SEARCH_LIMIT

    assert DEFAULT_SEARCH_LIMIT == 10  # 명시 요청 없이 20건씩 반환하지 않는다

    upstream.default_search = "search_written"
    await call("search_tax_interpretations", {"query": "상속"})
    _a, param = next(c for c in upstream.calls if c[0] == "ASIPDI002PR01")
    assert param["viewCount"] == DEFAULT_SEARCH_LIMIT

    upstream.calls.clear()
    upstream.default_search = "search_tribunal"
    await call("search_tax_decisions", {"query": "상속"})
    _a, param = next(c for c in upstream.calls if c[0] == "ASIPDI002PR01")
    assert param["viewCount"] == DEFAULT_SEARCH_LIMIT


# ─── 3. 상세 조회: 절 분해 + fullText 비중복 + 내용 무손실 ────────────────────

async def test_detail_sections_replace_fulltext(upstream) -> None:
    """절이 본문을 담으면 fullText 를 함께 싣지 않는다 — 같은 본문 이중 전송 방지."""
    upstream.detail_by_id["200000000000022584"] = "detail_written"
    label, data = await call("get_tax_document", {"ntst_dcm_id": "200000000000022584"})
    assert label == "OK"
    doc = data["document"]
    assert doc["facts"] and doc["question"] and doc["relatedLawsText"]
    assert doc["answer"]  # 회신은 별도 필드(ntstDcmCntn)에서 온다
    assert "fullText" not in doc, "절이 본문을 담고 있는데 fullText 가 중복으로 실렸다"


async def test_detail_keeps_fulltext_when_sections_missing(upstream) -> None:
    """판례처럼 번호 절이 없는 문서는 fullText 가 유일한 본문이므로 남긴다."""
    upstream.detail_by_id["100000000000021141"] = "detail_court"
    label, data = await call("get_tax_document", {"ntst_dcm_id": "100000000000021141"})
    assert label == "OK"
    doc = data["document"]
    fixture = load("detail_court")
    html = next(x for x in fixture["dcmHwpEditorDVOList"] if str(x["dcmFleTy"]).lower() == "html")
    parsed = parse_body_html(html["dcmFleByte"])
    if parsed.sections:
        assert "fullText" not in doc, "절이 있는데 fullText 가 중복으로 실렸다"
    else:
        assert doc.get("fullText"), "절이 없는 문서에서 fullText 마저 빠졌다 — 내용 유실"


def test_section_split_loses_no_content() -> None:
    """HTML 정리·절 분해가 본문 내용을 훼손하지 않는다: 절+서두 = 전체(제목 줄 제외)."""
    for name in ("detail_written", "detail_pre_assessment", "detail_tribunal", "detail_objection"):
        fixture = load(name)
        html = next(x for x in fixture["dcmHwpEditorDVOList"] if str(x["dcmFleTy"]).lower() == "html")
        parsed = parse_body_html(html["dcmFleByte"])
        heading_lines = {h["raw"] for h in parsed.headings}
        reassembled = set()
        for chunk in (parsed.preamble, *parsed.sections.values()):
            reassembled.update(ln for ln in chunk.split("\n") if ln)
        for line in parsed.text.split("\n"):
            if not line or line in heading_lines:
                continue
            assert line in reassembled, f"{name}: 절 분해에서 유실된 줄 — {line[:60]!r}"


# ─── 4~5. 빈 필드·중복 블록 없음 ─────────────────────────────────────────────

async def test_responses_carry_no_empty_or_null_fields(upstream) -> None:
    upstream.default_search = "search_written"
    upstream.detail_by_id["200000000000022584"] = "detail_written"

    _label, search = await call("search_tax_interpretations", {"query": "분양권"})
    assert_no_empty_values(search)

    _label, detail = await call("get_tax_document", {"ntst_dcm_id": "200000000000022584"})
    assert_no_empty_values(detail)
    doc = detail["document"]
    assert "citation" not in doc
    assert doc.get("summary") is not None
    assert "gist" not in doc, "summary 와 gist 는 같은 값이다 — 하나만 싣는다"


async def test_lookup_success_omits_diagnostics(upstream) -> None:
    upstream.default_search = "search_docnumber_exact"
    upstream.detail_by_id["200000000000022584"] = "detail_written"
    label, data = await call("lookup_tax_document", {"document_number": "서면-2026-법규재산-0119"})
    assert label == "OK"
    for banned in ("triedQueries", "searchedDomains", "inputInterpretation",
                   "normalizedDocumentNumber", "input"):
        assert banned not in data, f"성공 응답에 진단 필드 {banned}"


async def test_not_found_keeps_diagnostics_and_slim_similar(upstream) -> None:
    """NOT_FOUND 진단 정보는 유지하되, 유사문서는 식별 필드만 싣는다."""
    upstream.default_search = "search_docnumber_partial"
    label, data = await call("lookup_tax_document", {"document_number": "법규재산-0119"})
    assert label == "NOT_FOUND"
    detail = data["error"]["detail"]
    assert detail["triedQueries"] and detail["normalizedDocumentNumber"]
    assert detail["similarDocuments"]
    assert len(detail["similarDocuments"]) <= 5
    for similar in detail["similarDocuments"]:
        assert "summary" not in similar and "sourceUrl" not in similar
        assert similar["documentNumber"] and similar["ntstDcmId"]


# ─── 6. compact JSON ─────────────────────────────────────────────────────────

async def test_json_is_compact(upstream) -> None:
    from fastmcp import Client

    upstream.default_search = "search_written"
    async with Client(mcp) as client:
        result = await client.call_tool("search_tax_interpretations", {"query": "분양권"})
    text = result.content[0].text
    body = text[text.index("\n") + 1 :]
    assert "\n  " not in body, "응답 JSON 에 들여쓰기가 있다 — 토큰 낭비"


# ─── 7. detail="compact" 모드 ────────────────────────────────────────────────

async def test_compact_detail_keeps_conclusion_drops_body_sections(upstream) -> None:
    """compact 는 요지·회신·결론만 남긴다 — 사실관계·주장·이유 절은 생략."""
    upstream.detail_by_id["200000000000022584"] = "detail_written"
    label, data = await call(
        "get_tax_document", {"ntst_dcm_id": "200000000000022584", "detail": "compact"}
    )
    assert label == "OK"
    doc = data["document"]
    assert doc["summary"] and doc["answer"]
    assert doc["documentNumber"] and doc["sourceUrl"]
    for banned in ("facts", "question", "relatedLawsText", "reasoning", "fullText", "preamble"):
        assert banned not in doc, f"compact 응답에 {banned}"

    # 기본값(full)은 절 전체를 반환해야 한다
    label, data = await call("get_tax_document", {"ntst_dcm_id": "200000000000022584"})
    assert data["document"]["facts"]


async def test_compact_lookup_passthrough(upstream) -> None:
    upstream.default_search = "search_docnumber_exact"
    upstream.detail_by_id["200000000000022584"] = "detail_written"
    label, data = await call(
        "lookup_tax_document",
        {"document_number": "서면-2026-법규재산-0119", "detail": "compact"},
    )
    assert label == "OK"
    assert data["document"]["summary"]
    assert "facts" not in data["document"]


# ─── 8. sourceUrl 템플릿 ─────────────────────────────────────────────────────

async def test_search_items_use_url_template(upstream) -> None:
    upstream.default_search = "search_written"
    _label, data = await call("search_tax_interpretations", {"query": "분양권"})
    template = data["sourceUrlTemplate"]
    assert "{ntstDcmId}" in template
    for item in data["items"]:
        assert "sourceUrl" not in item
        # 템플릿에 ID 를 끼우면 유효한 상세 URL 이 된다
        assert template.replace("{ntstDcmId}", item["ntstDcmId"]).startswith(
            "https://taxlaw.nts.go.kr/"
        )


# ─── 9. guidance 파싱 캐시가 결과를 오염시키지 않는지 ────────────────────────

async def test_guidance_parse_cache_is_not_mutated_by_filters(upstream) -> None:
    """query 필터가 캐시된 전체 목록을 줄여 놓으면 다음 호출이 오염된다."""
    args = {"kind": "basic_ruling", "law_name": "상속세 및 증여세법"}
    _l, filtered = await call("search_tax_guidance", {**args, "query": "상속재산", "limit": 300})
    _l, full = await call("search_tax_guidance", {**args, "limit": 300})
    assert filtered["total"] < full["total"], "필터가 캐시 원본을 줄여 놓았다"
