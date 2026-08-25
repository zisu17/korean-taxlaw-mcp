"""문서 본문 HTML → 정제 텍스트 + 절 분해.

본문은 국세청이 HWP 원본을 HTML 로 변환해 넣어둔 것(``dcmHwpEditorDVOList`` 중
``dcmFleTy == "html"``)이라 메뉴·푸터·내비게이션이 없다. 그래도 표 레이아웃과
인라인 스타일이 많아 태그를 그냥 지우면 줄이 뭉개지므로 블록 태그를 개행으로
바꾼 뒤 정리한다.

절 분해는 **관측된 제목 어휘**로만 한다. 추론으로 절을 만들어내면 원문에 없는
구조를 지어내는 셈이라, 정해진 어휘에 걸리지 않으면 절을 비우고 전체 텍스트만 준다.
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass, field

from .config import FALLBACK_BODY_LIMIT

_HIGHLIGHT = re.compile(r"<!H[SE]>")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.S | re.I)
_CELL_END = re.compile(r"</(td|th)\s*>", re.I)
_BLOCK_END = re.compile(
    r"</(p|div|tr|li|h[1-6]|table|thead|tbody|blockquote|section)\s*>", re.I
)
_BR = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")
#: 폭 조절용으로 낱글자 사이에 박힌 각종 공백류
_SPACES = re.compile(r"[   -​　\t]+")


def strip_highlight(value: object) -> str:
    """강조 마커만 제거 (문서번호·제목 비교 전처리)."""
    if value is None:
        return ""
    return _HIGHLIGHT.sub("", str(value))


def html_to_text(source: str | None) -> str:
    """HTML → 줄 단위 텍스트. 표 셀은 공백으로, 블록 경계는 개행으로 접는다."""
    if not source:
        return ""
    s = strip_highlight(source)
    s = _COMMENT.sub("", s)
    s = _SCRIPT_STYLE.sub("", s)
    s = _CELL_END.sub(" ", s)
    s = _BLOCK_END.sub("\n", s)
    s = _BR.sub("\n", s)
    s = _TAG.sub("", s)
    s = html_module.unescape(s)

    lines: list[str] = []
    for raw_line in s.split("\n"):
        line = _SPACES.sub(" ", raw_line).strip()
        if not line:
            continue
        # 표 변환에서 생기는 연속 중복 줄 제거
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return "\n".join(lines)


#: 정규 절 이름. 문서 종류가 달라도 같은 의미면 같은 이름으로 모은다 —
#: facts(사실관계·처분개요) / question(질의내용) / claimantView(청구인 주장) /
#: agencyView(처분청 의견) / relatedLaws(관련법령) / issue(쟁점) /
#: reasoning(심리 및 판단) / conclusion(결론·주문)
#:
#: 절 제목 어휘. 사이트 실측 본문에서 관측한 표현만 담는다. 순서가 우선순위다.
_SECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("facts", re.compile(r"^(사실관계(\s*및\s*(과세예고통지\s*내용|처분내용))?|처분\s*개요)$")),
    ("question", re.compile(r"^(질의\s*내용|질의사항|질의\s*요지)$")),
    # 주체 뒤에 '의/들의/등의' 가 붙는 표기가 흔하다("신청법인의 주장", "청구인들의 주장").
    ("claimantView", re.compile(r"^(청구인|청구법인|신청인|신청법인|원고|청구인들|신청인들)(들|등)?(의)?\s*주장.*$")),
    ("agencyView", re.compile(r"^(처분청|통지관서|조사청|피고|과세관청|조사관서)(의)?\s*의견.*$")),
    ("relatedLaws", re.compile(r"^(관련법령(\s*및\s*관련사례)?|관련\s*법령|관련조문)$")),
    ("issue", re.compile(r"^(쟁점|쟁점사항|다툼의\s*대상)$")),
    ("reasoning", re.compile(r"^(심리\s*및\s*판단|판단|이유|심리|당원의\s*판단|법원의\s*판단)$")),
    ("conclusion", re.compile(r"^(결\s*론|주\s*문|결론\s*및\s*주문)$")),
]

#: "1. 사실관계" 같은 최상위 번호 제목만 절 경계로 인정한다.
_TOP_HEADING = re.compile(r"^(\d{1,2})\s*[.)]\s*(.{1,40})$")


def _classify_heading(title: str) -> str | None:
    t = re.sub(r"\s+", " ", title).strip().rstrip(":：")
    for name, pattern in _SECTION_PATTERNS:
        if pattern.match(t):
            return name
    # "2. 청구인 주장 및 처분청 의견" 처럼 두 주체가 한 절에 묶인 경우
    if re.search(r"청구인.*주장", t):
        return "claimantView"
    return None


@dataclass(slots=True)
class SplitBody:
    #: 정제된 전체 텍스트. 절 분해가 실패해도 이건 항상 있다.
    text: str
    #: 인식된 절. 어휘에 걸린 것만 담기며, 없으면 빈 dict.
    sections: dict[str, str] = field(default_factory=dict)
#: 분해 과정을 확인할 수 있도록 원문에서 읽은 절 제목을 그대로 남긴다.
    headings: list[dict[str, str]] = field(default_factory=list)
    #: 첫 절 제목 앞에 있던 텍스트. 절이 하나라도 잡히면 절 구간이 첫 제목부터
    #: 끝까지 연속이므로, preamble + 절 제목 줄 + sections = 전체 텍스트다.
    #: 이 불변식 덕에 절이 분해된 문서는 fullText 를 중복으로 싣지 않아도 된다.
    preamble: str = ""


def split_sections(text: str) -> SplitBody:
    """본문 텍스트를 최상위 번호 제목 기준으로 절 분해한다.

    분해 규칙은 두 겹이다.

    1. **어휘로 먼저 거른다.** 정해진 절 제목 어휘에 걸리는 줄만 후보로 본다.
       본문 중간의 조문 열거("1. 상속개시일 전 10년 이내에 …")는 어휘에 걸리지 않으니
       애초에 절로 승격되지 않는다.
    2. **번호가 커지는 순서만 받는다.** 결정례 본문은 절 안에 다시 1·2·3… 을 쓰기 때문에
       (이의신청 실측: "2. 신청법인의 주장" 아래에 "1)~6)" 이 다시 붙는다) 번호를 그냥
       세면 중첩 항목을 절로 오인한다. 이미 받은 절보다 번호가 큰 것만 받아서
       중첩을 걸러낸다.

    번호 연속(1,2,3,4)을 강제하지 않는 이유: 어휘에 없는 절이 중간에 하나 끼면
    그 뒤 절이 모두 탈락해 버린다. 실제로 그 규칙 때문에 이의신청 문서에서
    청구인주장·처분청의견·판단이 통째로 빠졌다.
    """
    lines = text.split("\n")
    kept: list[tuple[int, str, str, int]] = []

    for index, line in enumerate(lines):
        m = _TOP_HEADING.match(line.strip())
        if not m:
            continue
        name = _classify_heading(m.group(2))
        if name is None:
            continue
        order = int(m.group(1))
        if kept and order <= kept[-1][3]:
            # 이미 받은 절보다 번호가 작거나 같다 → 중첩 항목이다
            continue
        kept.append((index, line.strip(), name, order))

    sections: dict[str, str] = {}
    for position, (index, _raw, name, _order) in enumerate(kept):
        start = index + 1
        end = kept[position + 1][0] if position + 1 < len(kept) else len(lines)
        body = "\n".join(lines[start:end]).strip()
        if not body:
            continue
        sections[name] = f"{sections[name]}\n\n{body}" if name in sections else body

    preamble = "\n".join(lines[: kept[0][0]]).strip() if kept else ""

    return SplitBody(
        text=text,
        sections=sections,
        headings=[{"raw": raw, "name": name} for _i, raw, name, _o in kept],
        preamble=preamble,
    )


def parse_body_html(source: str) -> SplitBody:
    return split_sections(html_to_text(source))


@dataclass(slots=True)
class Truncated:
    text: str
    truncated: bool
    original_length: int


def truncate(text: str, limit: int | None = None) -> Truncated:
    """응답 크기 제한.

    실측으로 본문 HTML 이 1.4MB(심사청구 일부)까지 나온다. MCP 응답에 그대로 실으면
    클라이언트 컨텍스트를 통째로 삼키므로 자른다. **잘렸다는 사실을 반드시 표시**한다 —
    조용히 자르면 모델이 뒷부분이 없는 걸 문서에 없는 내용으로 오해한다.
    """
    cap = FALLBACK_BODY_LIMIT if limit is None else limit
    original = len(text)
    if original <= cap:
        return Truncated(text, False, original)
    note = (
        f"\n\n…[본문이 {original:,}자 중 {cap:,}자까지만 표시되었습니다. "
        "나머지는 sourceUrl 원문에서 확인하세요.]"
    )
    return Truncated(text[:cap] + note, True, original)


def attach_full_text(out: dict[str, object], text: str, limit: int | None) -> None:
    """``fullText``(+절단 표식)를 응답 dict 에 싣는 단일 규칙.

    국세·지방세 상세가 같은 계약을 쓴다: 잘리지 않았으면 플래그를 싣지 않는다.
    """
    full = truncate(text, limit)
    out["fullText"] = full.text
    if full.truncated:
        out["fullTextTruncated"] = True
        out["fullTextOriginalLength"] = full.original_length
