#!/usr/bin/env python3
"""MCP 응답을 국세법령정보시스템 원본 응답과 직접 대조한다.

MCP 코드를 우회한 순수 HTTP 호출과 MCP 도구 호출을 나란히 실행해 결과가 같은지 본다.
사이트 개편이나 파서 회귀를 눈으로 확인할 때 쓴다.

    python scripts/compare_with_site.py
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx
from fastmcp import Client

from korean_taxlaw_mcp.action_client import ACTION_URL, close_client
from korean_taxlaw_mcp.config import NTS, NTS_ORIGIN
from korean_taxlaw_mcp.html_text import strip_highlight
from korean_taxlaw_mcp.server import mcp


async def raw(action_id: str, param: dict) -> dict:
    """MCP 코드를 거치지 않는 순수 HTTP 호출 — 대조의 기준값."""
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(
            ACTION_URL,
            data={"actionId": action_id, "paramData": json.dumps(param, ensure_ascii=False)},
            headers={
                "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                "user-agent": NTS.user_agent,
                "x-requested-with": "XMLHttpRequest",
                "origin": NTS_ORIGIN,
                "referer": f"{NTS_ORIGIN}/index.do",
            },
        )
    return response.json()["data"][action_id]


def search_param(classes: list[str], terms: list[str], collection: str, limit: int = 5) -> dict:
    return {
        "startCount": 1, "viewCount": limit, "schDtBase": "DCM_RGT_DTM",
        "bltnStrtDt": "", "bltnEndDt": "", "collectionName": collection,
        "dcmClCdCtl": [f"001_{c}" for c in classes],
        "icldVcbCtl": terms, "exclVcbCtl": [], "ntstTlawClCdList": [],
        "sortField": "SCORE/DESC" if terms else "DCM_RGT_DTM/DESC",
    }


def totals(payload: dict, classes: list[str]) -> int:
    top = payload.get("top") or [{}]
    cats = (top[0].get("categoryMap") or {}).get("SUB_ID_CATEGORY") or []
    want = {f"001_{c}" for c in classes}
    return sum(int(c["count"]) for c in cats if c["name"] in want)


async def tool(client: Client, name: str, args: dict) -> tuple[str, dict | None]:
    result = await client.call_tool(name, args)
    text = result.content[0].text
    label = text.split("]")[0].lstrip("[")
    try:
        return label, json.loads(text[text.index("\n") + 1 :])
    except ValueError:
        return label, None


SEARCH_CASES = [
    ("세법해석례 '법규재산'", ["법규재산"], ["01", "02", "03", "04"], "question,question_gr",
     "search_tax_interpretations", {"query": "법규재산", "limit": 5}),
    ("세법해석례 '상속 공동상속주택'", ["상속", "공동상속주택"], ["01", "02", "03", "04"],
     "question,question_gr", "search_tax_interpretations",
     {"query": "상속 공동상속주택", "limit": 5}),
    ("심판청구 '공동상속주택'", ["공동상속주택"], ["08"], "precedent,precedent_gr",
     "search_tax_decisions", {"query": "공동상속주택", "type": "tribunal", "limit": 5}),
]

DETAIL_CASES = [
    ("서면-2026-법규재산-0119", "200000000000022584"),
    ("적부-국세청-2026-0119", "200000000000021979"),
    ("조심-2025-인-4460", "200000000000022509"),
]


async def main() -> int:
    passed = failed = 0

    def check(condition: bool, message: str) -> None:
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"   ✓ {message}")
        else:
            failed += 1
            print(f"   ✗ {message}")

    async with Client(mcp) as client:
        for title, terms, classes, collection, tool_name, args in SEARCH_CASES:
            print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
            site = await raw("ASIPDI002PR01", search_param(classes, terms, collection))
            site_total = totals(site, classes)
            site_numbers = [
                strip_highlight(row["dcm"]["NTST_DCM_DSCM_CNTN"]) for row in (site.get("body") or [])
            ]
            label, data = await tool(client, tool_name, args)
            print(f"  원본 total = {site_total:>8}   MCP total = {data['total']:>8}")
            print(f"  원본 문서번호: {site_numbers}")
            print(f"  MCP  문서번호: {[i['documentNumber'] for i in data['items']]}")
            check(label == "OK", "MCP 응답 라벨 OK")
            check(site_total == data["total"], "건수 일치")
            check(site_numbers == [i["documentNumber"] for i in data["items"]], "문서번호·순서 일치")

        for document_number, doc_id in DETAIL_CASES:
            print(f"\n{'=' * 78}\n상세 대조: {document_number}\n{'=' * 78}")
            site = await raw("ASIQTB002PR01", {"dcmDVO": {"ntstDcmId": doc_id}})
            dvo = site["dcmDVO"]
            site_laws = [
                x["ntstTextNm"].strip()
                for x in (site.get("dcmRltnStttList") or [])
                if x.get("ntstTextNm")
            ]
            html = next(
                (x for x in site["dcmHwpEditorDVOList"] if str(x["dcmFleTy"]).lower() == "html"), None
            )
            label, data = await tool(
                client, "lookup_tax_document",
                {"document_number": document_number, "body_limit": 200_000},
            )
            doc = data["document"]
            print(f"  문서번호  원본={dvo['ntstDcmDscmCntn']}  MCP={doc['documentNumber']}")
            print(f"  제목      원본={dvo['ntstDcmTtl'][:50]}")
            print(f"            MCP ={doc['title'][:50]}")
            print(f"  관련법령  원본={site_laws}")
            print(f"            MCP ={doc.get('relatedLaws', [])}")
            print(f"  본문      원본 HTML {len(html['dcmFleByte']) if html else 0:,}bytes"
                  f"  →  MCP 텍스트 {sum(len(str(doc.get(f) or '')) for f in ('fullText', 'facts', 'question', 'reasoning', 'conclusion', 'relatedLawsText', 'preamble')):,}자")
            check(doc["documentNumber"] == dvo["ntstDcmDscmCntn"], "문서번호 일치")
            check(doc["title"] == dvo["ntstDcmTtl"], "제목 일치")
            check(doc.get("relatedLaws", []) == list(dict.fromkeys(site_laws)), "관련법령 일치")
            check(doc["ntstDcmId"] == dvo["ntstDcmId"], "ntstDcmId 일치")
            check(doc.get("summary", "") == (dvo.get("ntstDcmGistCntn") or "").strip(), "요지 일치")
            check(any(doc.get(f) for f in ("fullText", "facts", "question", "reasoning", "conclusion", "relatedLawsText", "preamble")), "본문 추출됨")

    print(f"\n{'=' * 78}\n대조 결과: 통과 {passed} / 실패 {failed}\n{'=' * 78}")
    await close_client()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
