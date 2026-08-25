"""자연어 → 국세청 자료 라우팅.

"국세청 예규 찾아줘" 같은 표현이나 문서번호 접두를 보고 조회 영역을 고른다.
라우팅은 검색할 곳만 정하며 내용은 판단하지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .codes import TAX_TYPE_ALIAS
from .doc_number import looks_like_document_number, parse_doc_number

_DECISION_CLASSES = {"05", "06", "07", "08", "09", "10"}

#: 자연어 표현 → 도메인. 사용자가 실제로 쓰는 말만 담는다.
_PHRASE_TO_DOMAIN: list[tuple[re.Pattern[str], str, tuple[str, ...]]] = [
    (re.compile(r"사전\s*답변"), "interpretation", ("01",)),
    (re.compile(r"서면\s*질의|질의\s*회신|질의회신|서면질의"), "interpretation", ("02",)),
    # 맨 '서면'·'사전' 만 써도 실무에서는 서면질의·사전답변을 가리킨다.
    # 문서번호 첫 마디가 그대로 그 약칭이다(서면-…=질의회신, 사전-…=사전답변).
    (re.compile(r"(?<![가-힣])서면(?![가-힣])"), "interpretation", ("02",)),
    (re.compile(r"(?<![가-힣])사전(?![가-힣])"), "interpretation", ("01",)),
    (re.compile(r"과세\s*기준\s*자문|기준\s*자문"), "interpretation", ("03",)),
    (re.compile(r"고시\s*서면\s*질의"), "interpretation", ("04",)),
    (re.compile(r"예규|국세청\s*해석|세법\s*해석|법령해석"), "interpretation", ()),
    (re.compile(r"과세\s*적부|적부"), "decision", ("05",)),
    (re.compile(r"이의\s*신청"), "decision", ("06",)),
    (re.compile(r"심사\s*청구"), "decision", ("07",)),
    (re.compile(r"심판\s*청구|조세심판|심판례|조심"), "decision", ("08",)),
    (re.compile(r"판례|대법원|고등법원|행정법원"), "decision", ("09",)),
    (re.compile(r"헌재|헌법재판소"), "decision", ("10",)),
    (re.compile(r"불복"), "decision", ()),
    (re.compile(r"기본\s*통칙|통칙"), "guidance", ()),
    (re.compile(r"세법\s*집행\s*기준|집행\s*기준"), "guidance", ()),
    (re.compile(r"훈령|고시"), "guidance", ()),
    (re.compile(r"서식|별표"), "form", ()),
]

#: 국세청 자료임을 강하게 시사하는 표현.
_NTS_MARKERS = re.compile(r"국세청|국세|세무|세법|예규|과세|납세|세목|국세법령")

#: 검색어에서 걷어낼 자료 종류·기관·일반 세무 표현.
#:
#: 세목 낱말(상속·양도 등)은 걷어내지 않는다. 세목은 문서의 분류값이지 본문
#: 낱말과 1:1 이 아니어서(예: '공동상속주택' 관련 예규가 세목상 양도소득세로 분류된다)
#: 세목을 필터로 강제하면 정작 맞는 문서가 빠진다. 그래서 세목은 추정 결과만
#: 보고하고 필터로 쓰지 않으며, 낱말은 검색어에 그대로 남긴다.
_STRIP_FOR_SEARCH = [
    "국세법령정보시스템", "국세법령정보", "납세자보호위원회",
    "과세기준자문", "고시서면질의", "질의회신", "서면질의", "사전답변", "기준자문",
    "세법해석", "법령해석", "과세적부", "이의신청", "심사청구", "심판청구", "조세심판",
    "헌법재판소", "행정법원", "고등법원", "지방법원", "대법원",
    "집행기준", "기본통칙", "국세청", "예규", "통칙", "훈령", "판례", "심판례", "결정례",
    "적부", "불복", "헌재", "서식", "별표", "찾아줘", "알려줘", "검토해줘",
    # 짧은 약칭은 반드시 맨 뒤 — 앞의 긴 표현("서면질의")이 먼저 지워져야 한다
    "서면", "사전",
]

_DOCNUM_SHAPE = re.compile(r"[가-힣]{2,6}\s*-\s*[0-9가-힣]+\s*-\s*[0-9가-힣]+(\s*-\s*[0-9,]+)?")


def _to_content_query(query: str) -> str:
    """자료 종류·기관 표현을 걷어낸 내용 키워드.

    사이트 검색은 공백 구분 낱말을 AND 로 묶으므로 "국세청 예규 공동상속주택" 을
    그대로 던지면 '국세청'·'예규' 까지 본문에 있어야 해서 정작 관련 문서가 떨어진다.
    """
    # 문서번호를 먼저 걷어낸다. 순서를 뒤집으면 '적부-국세청-2026-0119' 에서
    # '적부'·'국세청' 만 지워져 '- -2026-0119' 같은 잔해가 검색어로 남는다.
    s = _DOCNUM_SHAPE.sub(" ", query)
    for word in _STRIP_FOR_SEARCH:
        s = s.replace(word, " ")
    # 마디를 지우고 남은 외톨이 구분자 정리
    s = re.sub(r"(?<!\S)[-·,]+(?!\S)", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.strip("-·, ")


@dataclass(slots=True)
class RouteHint:
    #: 국세청 자료로 보내야 하는 질의인지.
    is_nts_query: bool
    domains: list[str]
    #: 자료 종류·기관 표현을 걷어낸 내용 키워드.
    content_query: str
    #: 사용자가 라우팅 판단 과정을 확인할 수 있도록 근거를 남긴다.
    reasons: list[str] = field(default_factory=list)
    doc_classes: list[str] = field(default_factory=list)
    document_number: str | None = None
    #: 질의에서 추정한 세목. 필터로 강제하지 않는다.
    tax_type_codes: list[str] = field(default_factory=list)


def route_query(query: str | None) -> RouteHint:
    q = str(query or "")
    reasons: list[str] = []
    domains: list[str] = []
    doc_classes: list[str] = []

    def add_domain(name: str) -> None:
        if name not in domains:
            domains.append(name)

    # 문서번호가 있으면 가장 먼저 반영한다.
    document_number: str | None = None
    for token in [*q.split(), q]:
        if not looks_like_document_number(token):
            continue
        parsed = parse_doc_number(token)
        if not parsed.structured:
            continue
        document_number = parsed.canonical
        if parsed.inferred_doc_class:
            if parsed.inferred_doc_class not in doc_classes:
                doc_classes.append(parsed.inferred_doc_class)
            add_domain("decision" if parsed.inferred_doc_class in _DECISION_CLASSES else "interpretation")
        reasons.append(f"문서번호 패턴 '{parsed.canonical}' 인식 (레이아웃 {parsed.layout})")
        break

    # 표현 매칭
    for pattern, domain, classes in _PHRASE_TO_DOMAIN:
        if not pattern.search(q):
            continue
        add_domain(domain)
        for code in classes:
            if code not in doc_classes:
                doc_classes.append(code)
        reasons.append(f"표현 '{pattern.pattern}' → {domain}")

    # 세목은 참고용으로만 추정한다.
    tax_codes: list[str] = []
    for alias, code in TAX_TYPE_ALIAS.items():
        if len(alias) >= 2 and alias in q and code not in tax_codes:
            tax_codes.append(code)
    if tax_codes:
        reasons.append(f"세목 추정(참고용): {','.join(tax_codes)}")

    # 세목 낱말만 있는 질의("저가 양도하면 증여세")도 세무 질의로 본다.
    is_nts = bool(document_number) or bool(domains) or bool(_NTS_MARKERS.search(q)) or bool(tax_codes)
    if is_nts and not domains:
        add_domain("interpretation")
        add_domain("decision")
        reasons.append("세무 질의로 보이나 자료 종류 미특정 → 해석례·결정례 모두 조회")

    return RouteHint(
        is_nts_query=is_nts,
        domains=domains,
        content_query=_to_content_query(q),
        reasons=reasons,
        doc_classes=doc_classes,
        document_number=document_number,
        tax_type_codes=tax_codes,
    )
