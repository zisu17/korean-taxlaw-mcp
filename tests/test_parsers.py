"""실제 국세청 응답 fixture 기반 파서 테스트.

fixture 는 사이트에서 그대로 받아 저장한 것이라, 사이트가 응답 구조를 바꾸면
`NTS_LIVE=1` 통합 테스트가 먼저 깨지고, 여기 테스트는 우리 파서의 회귀를 잡는다.
"""

from __future__ import annotations

import pytest

from korean_taxlaw_mcp.codes import DECISION_RESULT, DOC_CLASS, ISSUING_AGENCY, TAX_TYPE
from korean_taxlaw_mcp.html_text import html_to_text, parse_body_html, truncate

from .conftest import requires_fixtures

#: 이 모듈은 전부 실측 응답 fixture 에 기댄다
pytestmark = requires_fixtures

# ─── 검색 응답 구조 ────────────────────────────────────────────────────────────

SEARCH_FIXTURES = [
    ("search_advance", "01", "사전답변"),
    ("search_written", "02", "질의회신"),
    ("search_advisory", "03", "과세기준자문"),
    ("search_notice_written", "04", "고시서면질의"),
    ("search_pre_assessment", "05", "과세적부"),
    ("search_objection", "06", "이의신청"),
    ("search_review", "07", "심사청구"),
    ("search_tribunal", "08", "심판청구"),
    ("search_court", "09", "판례"),
    ("search_constitutional", "10", "헌재"),
]


@pytest.mark.parametrize("name,doc_class,label", SEARCH_FIXTURES)
def test_search_envelope_shape(fixture, name, doc_class, label) -> None:
    """검색 응답은 top/body/wnSessionUuid 구조다. 이게 바뀌면 검색이 전부 깨진다."""
    data = fixture(name)
    assert "body" in data and "top" in data
    rows = data["body"]
    assert rows, f"{name}: body 가 비었다"
    dcm = rows[0]["dcm"]
    # 파서가 의존하는 필드가 모두 있어야 한다
    for field in ("DOC_ID", "NTST_DCM_DSCM_CNTN", "TTL", "MAIN_ID", "NTST_DCM_CL_CD"):
        assert field in dcm, f"{name}: {field} 누락"
    assert dcm["NTST_DCM_CL_CD"] == doc_class
    assert DOC_CLASS[doc_class] == label


@pytest.mark.parametrize("name,doc_class,_l", SEARCH_FIXTURES)
def test_search_rows_map_to_summary(fixture, name, doc_class, _l) -> None:
    from korean_taxlaw_mcp.domains.documents import DECISION_CLASSES, _row_to_summary

    for row in fixture(name)["body"]:
        item = _row_to_summary(row["dcm"])
        assert item["ntstDcmId"].isdigit()
        assert item["documentNumber"]
        assert item["title"]
        # 검색 요약에 본문·상수 boilerplate 가 실리면 안 된다 (토큰 예산).
        # URL 은 응답 최상위 sourceUrlTemplate 로 한 번만 나간다.
        for banned in ("fullText", "source", "domain", "citation", "sourceUrl"):
            assert banned not in item, f"검색 요약에 {banned} 가 실렸다"
        # 결정례는 불복 결정과 판례가 섞이므로 권위 층위를 남기고, 해석례는 생략한다
        if doc_class in DECISION_CLASSES:
            assert item["authorityLevel"] in {"adjudication", "court_case"}
        else:
            assert "authorityLevel" not in item
        # 하이라이트 마커가 새어 나가면 문서번호 비교가 깨진다
        assert "<!HS>" not in item["documentNumber"]
        assert "<!HE>" not in item["title"]


def test_document_number_index_contains_both_forms(fixture) -> None:
    """사이트 색인(DOCU_NO_STR1)은 하이픈형과 압축형을 함께 담는다 — 조회 전략의 근거."""
    from korean_taxlaw_mcp.html_text import strip_highlight

    dcm = fixture("search_docnumber_exact")["body"][0]["dcm"]
    # 질의가 맞은 구간은 <!HS>..<!HE> 로 감싸여 오므로 마커를 먼저 벗긴다
    index = strip_highlight(dcm["DOCU_NO_STR1"])
    assert "서면-2026-법규재산-0119" in index
    assert "서면2026법규재산0119" in index


def test_partial_number_search_returns_multiple_unrelated_docs(fixture) -> None:
    """'법규재산-0119' 부분검색은 서로 다른 문서를 섞어 준다.

    이 fixture 가 exact lookup 을 절대 첫 건으로 답해선 안 되는 이유의 증거다.
    """
    from korean_taxlaw_mcp.doc_number import is_same_doc_number

    rows = fixture("search_docnumber_partial")["body"]
    numbers = [r["dcm"]["NTST_DCM_DSCM_CNTN"].replace("<!HS>", "").replace("<!HE>", "") for r in rows]
    assert len(numbers) > 1
    exact = [n for n in numbers if is_same_doc_number(n, "법규재산-0119")]
    assert exact == [], f"부분 입력에 exact 판정이 나왔다: {exact}"


# ─── 상세 응답 + 본문 절 분해 ──────────────────────────────────────────────────

DETAIL_FIXTURES = [
    ("detail_written", "서면-2026-법규재산-0119", "02", {"facts", "question", "relatedLaws"}),
    ("detail_pre_assessment", "적부-국세청-2026-0119", "05", {"facts", "claimantView", "agencyView", "reasoning", "conclusion"}),
    ("detail_tribunal", "조심-2025-인-4460", "08", {"facts", "claimantView", "reasoning", "conclusion"}),
    ("detail_review", "심사-부가-2026-0018", "07", {"facts", "claimantView", "agencyView", "reasoning"}),
    ("detail_objection", "이의-광주청-2026-0024", "06", {"facts", "claimantView", "agencyView", "reasoning"}),
]


@pytest.mark.parametrize("name,docno,doc_class,expected_sections", DETAIL_FIXTURES)
def test_detail_envelope_and_sections(fixture, name, docno, doc_class, expected_sections) -> None:
    data = fixture(name)
    dvo = data["dcmDVO"]
    assert dvo["ntstDcmDscmCntn"] == docno
    assert dvo["ntstDcmClCd"] == doc_class

    hwp = data["dcmHwpEditorDVOList"]
    html = next(x for x in hwp if str(x["dcmFleTy"]).lower() == "html")
    parsed = parse_body_html(html["dcmFleByte"])

    assert parsed.text, f"{name}: 본문 텍스트 추출 실패"
    # 태그·엔티티가 남으면 파서가 깨진 것이다
    assert "<p" not in parsed.text and "&nbsp;" not in parsed.text
    missing = expected_sections - set(parsed.sections)
    assert not missing, f"{name}: 절 분해 누락 {missing} (실제: {sorted(parsed.sections)})"


def test_court_case_body_has_no_numbered_sections(fixture) -> None:
    """판례 본문은 번호 절 구조가 아니다. 억지로 절을 만들어내면 안 된다."""
    data = fixture("detail_court")
    html = next(x for x in data["dcmHwpEditorDVOList"] if str(x["dcmFleTy"]).lower() == "html")
    parsed = parse_body_html(html["dcmFleByte"])
    assert parsed.text
    # 결론 정도만 잡히거나 아무것도 안 잡히는 게 정상 — 없는 절을 지어내지 않는다
    assert set(parsed.sections) <= {"conclusion", "reasoning", "facts"}


def test_related_laws_extracted(fixture) -> None:
    rows = fixture("detail_written")["dcmRltnStttList"]
    names = [r["ntstTextNm"] for r in rows]
    assert "조세특례제한법 제71조의2" in names
    assert "조세특례제한법 시행령 제68조의2" in names


def test_answer_text_lives_in_dcm_cntn(fixture) -> None:
    """회신(답변) 본문은 본문 HTML 이 아니라 ntstDcmCntn 필드에 있다."""
    dvo = fixture("detail_written")["dcmDVO"]
    assert dvo["ntstDcmCntn"].strip()
    assert dvo["ntstDcmGistCntn"].strip()


def test_oversized_body_is_truncated_with_notice(fixture) -> None:
    """1.4MB 본문을 그대로 실으면 클라이언트 컨텍스트를 삼킨다."""
    data = fixture("detail_review")
    html = next(x for x in data["dcmHwpEditorDVOList"] if str(x["dcmFleTy"]).lower() == "html")
    assert len(html["dcmFleByte"]) > 1_000_000
    result = truncate(html_to_text(html["dcmFleByte"]), 5_000)
    assert result.truncated is True
    assert result.original_length > 5_000
    # 조용히 자르면 모델이 뒷부분 부재를 '문서에 없음' 으로 오해한다
    assert "본문이" in result.text and "sourceUrl" in result.text


# ─── 행정 해석기준 · 서식 ──────────────────────────────────────────────────────

def test_basic_ruling_items_include_body_text(fixture) -> None:
    rows = fixture("guidance_basic_ruling_items")["bscExrDVOList"]
    assert rows
    with_text = [r for r in rows if str(r.get("ntstTextCntn") or "").strip()]
    assert with_text, "기본통칙은 조항 본문이 함께 와야 한다"
    assert any("상속재산의 범위" in r["ntstTextNm"] for r in rows)


def test_execution_standard_items_have_no_body_text(fixture) -> None:
    """집행기준은 목차만 온다. 이 사실이 바뀌면 안내 문구를 고쳐야 한다."""
    rows = fixture("guidance_exec_items")["exeBaseDVOList"]
    assert rows
    assert all(not str(r.get("ntstTextCntn") or "").strip() for r in rows)


def test_execution_standard_years_expose_pdf_file(fixture) -> None:
    rows = fixture("guidance_exec_years")["exeBaseDVOList"]
    assert rows and rows[0]["fleId"]


@pytest.mark.parametrize("name,ntar", [("guidance_notice", "01"), ("guidance_directive", "03")])
def test_notice_directive_shape(fixture, name, ntar) -> None:
    data = fixture(name)
    assert data["ntarClCd"] == ntar
    rows = data["notcFeldDVOList"]
    assert rows, f"{name}: ntstSjtClCd='All' 없이는 0건이 온다"
    for field in ("ntarBscId", "ntarNm", "ntarPmgDt"):
        assert field in rows[0]


def test_forms_shape(fixture) -> None:
    rows = fixture("forms_search")["stttFrmlDVOList"]
    assert rows
    for field in ("ntstAtFrmlNm", "ntstAtFrmlSn", "fleId", "ntstPmgDt"):
        assert field in rows[0]


# ─── 코드표가 상수와 일치하는지 ────────────────────────────────────────────────

def test_code_tables_match_constants(fixture) -> None:
    """코드표를 상수로 고정했으므로 원본과 어긋나면 즉시 알아야 한다."""
    groups = {g["cmnClsfCd"]: g["cmCodeDVOList"] for g in fixture("codes") if g}
    checks = [("19378", DOC_CLASS), ("19425", ISSUING_AGENCY), ("19375", DECISION_RESULT)]
    for group_id, table in checks:
        live = {c["cdVval"]: c["cdVvalKrnNm"] for c in groups[group_id]}
        for code, label in table.items():
            assert live.get(code) == label, f"{group_id} 코드 {code}: 상수={label} 원본={live.get(code)}"


def test_tax_type_constants_match(fixture) -> None:
    groups = {g["cmnClsfCd"]: g["cmCodeDVOList"] for g in fixture("codes") if g}
    live = {c["cdVval"]: c["cdVvalKrnNm"] for c in groups["19387"]}
    for code, label in TAX_TYPE.items():
        if code == "999":  # 999(기타)는 문서용 가상 코드
            continue
        assert live.get(code) == label, f"세목 {code}: 상수={label} 원본={live.get(code)}"
