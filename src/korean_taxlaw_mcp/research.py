"""tax_research — 세무 상담 근거를 층별로 모아 주는 체인 도구.

실무에서 하나의 쟁점을 검토할 때 보는 순서는 정해져 있다::

    세법 → 시행령 → 시행규칙 → 기본통칙 → 세법집행기준 → 국세청 해석례 → 불복·판례

이 도구는 **그 층들을 모아 오는 일만** 한다. 결론을 내지 않고, 층별로 원문 근거와
출처를 붙여 돌려준다. 법령 본문(법률·시행령·시행규칙)은 이 서버의 범위가 아니다 —
법제처 데이터를 쓰는 korean-law-mcp 가 이미 안정적으로 제공하므로 중복 구현하지
않고, 어떤 조문을 확인해야 하는지만 지목한다.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .codes import TAX_TYPE, TAX_TYPE_ALIAS
from .domains.documents import INTERPRETATION_CLASSES, search_documents
from .domains.guidance import EXECUTION_STANDARD_BOOKS, list_basic_ruling_laws, search_guidance
from .model import AuthorityLevel

DISCLAIMER = (
    "원문 검색 결과 모음이며 법률적 판단이 아닙니다. 층별 authorityLevel(법규·행정해석·"
    "결정례·판례)의 효력 차이를 구분하고, 적용시점을 별도 확인하세요. "
    "검색 결과에 없는 내용을 추론으로 채우지 마세요."
)

#: 조사·어미를 걷어내고 핵심 낱말만 남긴다. 사이트 검색은 공백=AND 라 과하면 0건이 된다.
_STOPWORDS = {
    "경우", "여부", "관련", "관하여", "대하여", "대해", "검토", "검토해줘", "알려줘",
    "하는지", "있는지", "되는지", "인지", "무엇", "어떻게", "어떤", "그리고", "또는", "및",
    "해당", "이런", "저런", "위의", "아래", "합니다", "하나요", "인가요", "발생하는지",
}
_PARTICLE = re.compile(r"(을|를|이|가|은|는|에|의|와|과|으로|로|에서|에게|부터|까지|보다|처럼|만|도)$")
_PUNCT = re.compile(r"[「」『』（）()\[\]{}<>,.?!·:;\"'~]")
_LAW_REF = re.compile(
    r"(?:「([^」]{2,40})」|([가-힣]{2,20}(?:법|법률|규정|특례제한법)))\s*(제\s*\d+조(?:의\s*\d+)?)?"
)


def _infer_tax_types(question: str, explicit: str | None) -> tuple[list[str], list[str]]:
    """질문에서 세목을 추정한다. 별칭이 여럿 걸리면 등장 순서가 이른 쪽을 앞세운다."""
    codes: list[str] = []
    if explicit:
        code = explicit if explicit in TAX_TYPE else TAX_TYPE_ALIAS.get(explicit.replace(" ", ""))
        if code:
            codes.append(code)
    if not codes:
        hits: list[tuple[int, str]] = []
        for alias, code in TAX_TYPE_ALIAS.items():
            if len(alias) < 2:
                continue
            at = question.find(alias)
            if at >= 0:
                hits.append((at, code))
        hits.sort()
        for _at, code in hits:
            if code not in codes:
                codes.append(code)
    top = codes[:2]
    return top, [TAX_TYPE[c] for c in top if c in TAX_TYPE]


def _extract_law_refs(question: str) -> list[dict[str, str]]:
    """질문에서 법령·조문 표현을 뽑는다. 없으면 빈 목록."""
    out: list[dict[str, str]] = []
    for m in _LAW_REF.finditer(question):
        law_name = (m.group(1) or m.group(2) or "").strip()
        if not law_name:
            continue
        entry: dict[str, str] = {"lawName": law_name}
        if m.group(3):
            entry["article"] = re.sub(r"\s+", "", m.group(3))
        if entry not in out:
            out.append(entry)
    return out


def _keywords(question: str, limit: int = 3) -> list[str]:
    out: list[str] = []
    for word in _PUNCT.sub(" ", question).split():
        w = _PARTICLE.sub("", word).strip()
        if len(w) < 2 or w in _STOPWORDS or w.isdigit():
            continue
        if w not in out:
            out.append(w)
        if len(out) >= limit:
            break
    return out


def _pick_guidance_target(
    pool: list[str], law_refs: list[dict[str, str]], tax_type_names: list[str]
) -> str | None:
    """통칙·집행기준 대상 후보 중 질문과 가장 가까운 것을 고른다. 못 고르면 None."""

    def norm(s: str) -> str:
        return re.sub(r"[\s「」]|및|의", "", s)

    for ref in law_refs:
        needle = norm(ref["lawName"])
        hit = next((p for p in pool if norm(p) == needle), None) or next(
            (p for p in pool if needle in norm(p) or norm(p) in needle), None
        )
        if hit:
            return hit
    for name in tax_type_names:
        needle = norm(name)
        hit = next((p for p in pool if needle in norm(p)), None)
        if hit:
            return hit
        # 세목명과 법령명이 다른 경우(상속증여세 ↔ 상속세 및 증여세법) 부분 대조
        stem = needle.rstrip("세")
        hit = next((p for p in pool if stem and stem in norm(p)), None)
        if hit:
            return hit
    return None


def _layer(name: str, level: AuthorityLevel, **extra: Any) -> dict[str, Any]:
    # authorityNote(층위 설명문)는 층마다 반복하지 않는다 — authorityLevel 로 충분하다.
    return {
        "layer": name,
        "authorityLevel": str(level),
        **extra,
    }


async def tax_research(
    *,
    question: str,
    tax_type: str | None = None,
    law: str | None = None,
    article: str | None = None,
    limit_per_layer: int = 5,
    include_guidance: bool = True,
) -> dict[str, Any]:
    codes, names = _infer_tax_types(question, tax_type)
    # 세목 필터는 **사용자가 명시했을 때만** 적용한다. 질문에서 추정한 세목을 필터로
    # 강제하면 거짓 부정이 난다: '상속' 은 상속증여세(308)로 추정되지만 공동상속주택
    # 관련 예규는 세목이 양도소득세(307)로 분류돼 있어 308 필터에 걸러진다.
    # 법적 근거 조사에서 누락은 잡음보다 위험하다.
    filter_codes = codes if tax_type else []
    keywords = _keywords(question)
    law_refs = (
        [{"lawName": law, **({"article": article} if article else {})}] if law else _extract_law_refs(question)
    )
    base_query = " ".join(keywords)
    layers: list[dict[str, Any]] = []

    # 1) 법령 층 — 이 서버가 다루지 않는다는 사실을 **한 층으로** 남긴다.
    #    법률·시행령·시행규칙을 세 층으로 반복하면 같은 안내문이 세 번 실린다.
    targets = ", ".join(f"{r['lawName']}{r.get('article', '')}" for r in law_refs)
    layers.append(
        _layer(
            "법률·시행령·시행규칙", AuthorityLevel.STATUTE,
            provider="external", status="not_covered",
            message=(
                "법령 본문은 korean-law-mcp(get_law_text / search_law)로 확인하세요."
                + (f" 확인 대상: {targets}" if targets else "")
            ),
        )
    )

    # 2) 기본통칙 / 세법집행기준 — 법령명이 필요하므로 추정된 대상이 있을 때만
    if include_guidance:
        try:
            ruling_laws = [x["ntstNm"] for x in await list_basic_ruling_laws()]
        except Exception:
            ruling_laws = []
        for kind, layer_name in (("basic_ruling", "기본통칙"), ("execution_standard", "세법집행기준")):
            pool = ruling_laws if kind == "basic_ruling" else [b["ntstNm"] for b in EXECUTION_STANDARD_BOOKS]
            target = _pick_guidance_target(pool, law_refs, names)
            if not target:
                layers.append(
                    _layer(
                        layer_name, AuthorityLevel.NTS_GUIDANCE, provider="NTS", status="not_covered",
                        message="대상 법령 미특정 — law 파라미터로 법령명을 지정하면 조회합니다.",
                    )
                )
                continue
            try:
                result = await search_guidance(
                    kind=kind, law_name=target,
                    query=keywords[0] if keywords else None, limit=limit_per_layer,
                )
                layers.append(
                    _layer(
                        layer_name, AuthorityLevel.NTS_GUIDANCE, provider="NTS",
                        status="found" if result.get("items") else "empty",
                        query=f"{target} / {keywords[0] if keywords else ''}",
                        total=result.get("total"), items=result.get("items"),
                    )
                )
            except Exception as exc:
                layers.append(
                    _layer(layer_name, AuthorityLevel.NTS_GUIDANCE, provider="NTS",
                           status="error", message=str(exc))
                )

    # 3~4) 국세청 해석례 + 불복 결정례 + 판례
    plan: list[tuple[str, list[str], AuthorityLevel]] = [
        ("국세청 해석례", list(INTERPRETATION_CLASSES), AuthorityLevel.NTS_RULING),
        ("불복 결정례(적부·이의·심사·심판)", ["05", "06", "07", "08"], AuthorityLevel.ADJUDICATION),
        ("법원 판례·헌재 결정", ["09", "10"], AuthorityLevel.COURT_CASE),
    ]

    async def run(classes: list[str]) -> dict[str, Any]:
        return await search_documents(
            doc_classes=classes,
            query=base_query or None,
            tax_type_codes=filter_codes,
            limit=limit_per_layer,
            sort="relevance" if base_query else "latest",
        )

    results = await asyncio.gather(*(run(c) for _n, c, _l in plan), return_exceptions=True)
    for (layer_name, _classes, level), result in zip(plan, results, strict=True):
        if isinstance(result, BaseException):
            layers.append(
                _layer(layer_name, level, provider="NTS", status="error",
                       query=base_query, message=str(result))
            )
            continue
        layers.append(
            _layer(
                layer_name, level, provider="NTS",
                status="found" if result["items"] else "empty",
                query=base_query, total=result["total"], items=result["items"],
            )
        )

    extracted: dict[str, Any] = {
        "taxTypeCodes": codes,
        "taxTypeNames": names,
        "keywords": keywords,
        "lawReferences": law_refs,
    }
    if filter_codes:
        extracted["taxTypeFilterApplied"] = filter_codes
    else:
        # 추정 세목을 필터로 강제하지 않았다는 사실만 짧게 알린다
        extracted["taxTypeNote"] = "세목 필터 미적용 — taxTypeCodes 는 참고용 추정치."

    return {
        "question": question,
        "extracted": {k: v for k, v in extracted.items() if v},
        "layers": layers,
        "disclaimer": DISCLAIMER,
    }
