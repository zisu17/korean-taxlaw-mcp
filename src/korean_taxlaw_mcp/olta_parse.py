"""지방세 법령정보시스템 HTML 파서.

이 사이트는 JSON API 가 없어 렌더된 HTML 을 읽어야 한다. 파싱은 이 모듈 한 곳에
몰아두고, 마크업이 바뀌면 여기만 고치면 되게 한다. 구조 변경은 fixture 기반
테스트가 먼저 잡는다.

목록 행 마크업(실측)::

    <li>
      <p><span class="part">재산세</span>부동산세제과-1794(2026.6.9.)호 (2026.06.09)</p>
      <p class="tt"><a … onclick="AddViewDocument('제목','javascript:authoritativePopUp(60099135)');">제목</a></p>
      <p class="txt"><a …>요지</a></p>
    </li>

`authoritativePopUp(60099135)` 의 인수가 상세 조회에 쓰는 내부 문서 번호(`num`)다.
"""

from __future__ import annotations

import html as html_module
import re

from .html_text import html_to_text

#: 총 건수. "검색총건수 … 3,379 … 건" 사이에 마크업이 끼어 있다.
_TOTAL = re.compile(r"검색총건수[\s\S]{0,200}?([\d,]+)\s*(?:</[^>]*>)?\s*건")

#: 목록 행 블록. 머리 `<p>` + 제목 `<p class="tt">` + 요지 `<p class="txt">`.
#:
#: 자료 종류마다 머리 `<p>` 의 내용이 다르다(실측):
#:   유권해석  <span class="part">재산세</span>부동산세제과-1794(2026.6.9.)호 (2026.06.09)
#:   심판·판례 <span class="part">취득세</span>조심2025지0592 (2026.04.30)<span class="label">재조사</span>
#: 머리 `<p>` 전체를 잡은 뒤 필요한 조각을 나눈다. 종류별 정규식은 두지 않는다.
#: 머리 `<p>` 는 반드시 `<span class="part">` 로 시작해야 한다. 이 앵커가 없으면
#: 페이지 상단 알림 스크립트 안의 `<li><p>` 템플릿 문자열까지 행으로 잡힌다(실측).
_ROW_BLOCK = re.compile(
    r"<li>\s*(<p><span class=\"part\">.*?</p>)\s*"
    r"<p class=\"tt\">(.*?)</p>\s*"
    r"<p class=\"txt\">(.*?)</p>",
    re.S,
)

_PART = re.compile(r'<span class="part">(.*?)</span>', re.S)
_LABEL = re.compile(r'<span class="label">(.*?)</span>', re.S)

#: 팝업 함수 인수. 법원 판례는 `decisionDtlpopUp(20002922, 60099210, null)` 처럼
#: 인수가 둘이다(첫째=num, 둘째=relationshipNum). 나머지는 하나뿐이다.
#:
#: 함수명이 `…PopUp` 으로 끝나야 한다. 이 제약이 없으면 같은 `<a>` 의
#: `href="javascript:void(0);"` 가 먼저 잡혀 문서 번호가 0 이 된다(실측).
#: 실제 함수: authoritativePopUp / legalPopUp / screenPopUp / evaluationPopUp
#: / constitutionPopUp / decisionDtlpopUp
_POPUP = re.compile(r"javascript:(\w*[Pp]op[Uu]p)\(\s*(\d+)\s*(?:,\s*(\d+))?[^)]*\)")

#: 머리글에서 등록일을 뗀다. "부동산세제과-1794(2026.6.9.)호 (2026.06.09)"
_META = re.compile(r"^(.*?)\s*\((\d{4})\.(\d{2})\.(\d{2})\)\s*$")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", html_module.unescape(re.sub(r"<[^>]+>", "", value))).strip()


def parse_total(html: str) -> int:
    """검색 총 건수. 못 찾으면 0 — 다만 행이 있으면 호출자가 행 수로 보정한다."""
    m = _TOTAL.search(html)
    if not m:
        return 0
    return int(m.group(1).replace(",", ""))


def parse_rows(html: str) -> list[dict[str, str]]:
    """목록 행 파싱. 문서번호·세목·제목·요지·결정유형·내부번호를 뽑는다."""
    rows: list[dict[str, str]] = []
    for block in _ROW_BLOCK.finditer(html):
        head, title_html, gist_html = block.group(1), block.group(2), block.group(3)

        popup = _POPUP.search(title_html) or _POPUP.search(gist_html)
        if not popup:
            continue

        part = _PART.search(head)
        label = _LABEL.search(head)

        # 머리 `<p>` 에서 span 들을 지우면 남는 것이 문서번호 + 등록일이다
        meta_html = _PART.sub("", _LABEL.sub("", head))
        meta = _clean(meta_html)

        document_number, registered = meta, None
        meta_match = _META.match(meta)
        if meta_match:
            document_number = meta_match.group(1).strip()
            registered = f"{meta_match.group(2)}-{meta_match.group(3)}-{meta_match.group(4)}"

        row: dict[str, str] = {
            "num": popup.group(2),
            "documentNumber": document_number,
            "title": _clean(title_html),
        }
        if popup.group(3):
            # 법원 판례의 둘째 인수. 상세 조회에 relationshipNum 으로 함께 보낸다.
            row["relationshipNum"] = popup.group(3)
        if part and _clean(part.group(1)):
            row["taxType"] = _clean(part.group(1))
        if label and _clean(label.group(1)):
            row["decisionResult"] = _clean(label.group(1))
        if registered:
            row["registrationDate"] = registered
        gist = _clean(gist_html)
        if gist:
            row["gist"] = gist
        rows.append(row)
    return rows


#: 상세 화면의 절 제목. 자료 종류마다 조금씩 다르다(실측).
_DETAIL_SECTIONS: list[tuple[str, re.Pattern[str]]] = [
    ("relatedLaws", re.compile(r"^관계법령$")),
    ("gist", re.compile(r"^(답변요지|결정요지|판결요지|요지)$")),
    ("body", re.compile(r"^(본문|내용|전문)$")),
    ("question", re.compile(r"^<?\s*질의(요지|내용)?\s*>?$")),
    ("answer", re.compile(r"^<?\s*회신(내용)?\s*>?$")),
    ("reasoning", re.compile(r"^<?\s*(이유|판단|심리\s*및\s*판단)\s*>?$")),
]

#: 절 이름 목록. fullText 생략 판단(get_local_document)이 이 어휘와 어긋나면
#: 본문이 이중 전송되거나 유실되므로, 이름을 여기서만 관리한다.
DETAIL_SECTION_NAMES: tuple[str, ...] = tuple(name for name, _ in _DETAIL_SECTIONS)

#: 상세 화면 머리글: "부동산세제과-1794(2026.6.9.)호(20260609) 재산세"
_HEAD = re.compile(r"^(.*?)\((\d{8})\)\s*(\S*)\s*$")

#: 화면 장식용 텍스트 — 본문에 섞이면 안 된다.
_CHROME = {
    "한국지방세연구원 - 지방세 법령정보시스템", "다운로드", "프린트", "닫기", "가",
    "자료보안을 위해 비실명자료로만 인쇄되며 한국지방세연구원의 워터마크가 들어갑니다.",
    "목록", "인쇄", "확대", "축소",
}


def parse_detail(html: str) -> dict[str, object]:
    """상세 화면 파싱.

    사이트가 조문 표기를 낱글자 단위로 감싸 놓아 태그를 지우면 "제 106 조제 1 항"
    처럼 공백이 끼어든다. 조문 표기의 공백은 :func:`_tighten_articles` 로 붙인다.
    """
    lines = [ln for ln in html_to_text(html).split("\n") if ln not in _CHROME]
    if not lines:
        return {}

    out: dict[str, object] = {}

    # 머리글에서 문서번호·등록일·세목
    for line in lines[:6]:
        m = _HEAD.match(line)
        if not m or not m.group(2):
            continue
        digits = m.group(2)
        out["documentNumber"] = m.group(1).strip()
        out["registrationDate"] = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
        if m.group(3):
            out["taxType"] = m.group(3)
        head_index = lines.index(line)
        # 머리글 다음 줄이 제목이다
        if head_index + 1 < len(lines):
            out["title"] = lines[head_index + 1]
        lines = lines[head_index + 2 :]
        break

    # 절 분해 — 정해진 어휘에 걸리는 줄만 경계로 본다
    marks: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if len(line) > 12:
            continue
        for name, pattern in _DETAIL_SECTIONS:
            if pattern.match(line):
                marks.append((index, name))
                break

    for position, (index, name) in enumerate(marks):
        end = marks[position + 1][0] if position + 1 < len(marks) else len(lines)
        body = "\n".join(lines[index + 1 : end]).strip()
        if not body:
            continue
        body = _tighten_articles(body)
        out[name] = f"{out[name]}\n\n{body}" if name in out else body

    # 절 구간은 첫 절 제목부터 끝까지 연속이므로, 절에 속하지 않은 줄은 첫 절 제목
    # 앞부분뿐이다. 절과 합치면 본문 전체가 복원되므로, 호출자는 fullText 를
    # 중복으로 싣지 않아도 내용을 잃지 않는다.
    if marks:
        unsectioned = "\n".join(lines[: marks[0][0]]).strip()
        if unsectioned:
            out["unsectioned"] = _tighten_articles(unsectioned)

    full = _tighten_articles("\n".join(lines).strip())
    if full:
        out["fullText"] = full
    return out


#: 「 지방세법 」 제 106 조제 1 항제 3 호  →  「지방세법」 제106조제1항제3호
_BRACKET_SPACE = re.compile(r"「\s*(.*?)\s*」")
_ARTICLE_SPACE = re.compile(r"제\s*(\d+)\s*(조|항|호|목|절|장|편|款)")
_NUM_SPACE = re.compile(r"(\d)\s+(\d)")
_WHITESPACE = re.compile(r"\s+")
_PAREN_OPEN_SPACE = re.compile(r"\(\s+")
_PAREN_CLOSE_SPACE = re.compile(r"\s+\)")
_PUNCT_SPACE = re.compile(r"\s+([,.·])")


def _strip_inner_spaces(match: re.Match[str]) -> str:
    r"""「 지방세법 」 → 「지방세법」.

    f-string 안에서 처리하지 않는 이유: 표현식에 백슬래시(`\s`)를 넣는 문법은
    Python 3.12(PEP 701)부터라서 3.11 에서 SyntaxError 가 난다.
    이 프로젝트는 3.11 을 지원한다(pyproject: requires-python >= 3.11).
    """
    return "「" + _WHITESPACE.sub("", match.group(1)) + "」"


def _tighten_articles(text: str) -> str:
    """조문·법령명 표기에 끼어든 공백을 붙인다.

    사이트가 글자 단위로 태그를 감싸 놓아 태그를 제거하면 공백이 남는다. 이 상태로
    반환하면 모델이 조문을 인용할 때 원문과 다른 문자열을 쓰게 된다.
    """
    text = _BRACKET_SPACE.sub(_strip_inner_spaces, text)
    text = _ARTICLE_SPACE.sub(lambda m: "제" + m.group(1) + m.group(2), text)
    text = _NUM_SPACE.sub(r"\1\2", text)
    # 여는·닫는 괄호 안쪽 공백
    text = _PAREN_OPEN_SPACE.sub("(", text)
    text = _PAREN_CLOSE_SPACE.sub(")", text)
    # 쉼표·마침표 앞 공백
    text = _PUNCT_SPACE.sub(r"\1", text)
    return text
