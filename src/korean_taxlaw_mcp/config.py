"""환경변수 설정. 모두 선택 항목이며 기본값만으로 동작한다.

이 서버는 서로 다른 두 기관의 공개 시스템을 조회하므로 설정을 공통값과 사이트별
재정의로 나눈다.

    TAXLAW_*   ← 두 사이트에 공통 적용
    NTS_*      ← 국세(국세청)만 재정의
    OLTA_*     ← 지방세(한국지방세연구원)만 재정의

우선순위는 ``사이트별 > 공통 > 기본값``이다. 국세만 타임아웃을 늘리려면
``NTS_TIMEOUT_MS`` 를, 양쪽 다 늘리려면 ``TAXLAW_TIMEOUT_MS`` 를 준다.

이름과 적용 범위를 일치시키는 것이 요점이다. 이전에는 ``NTS_USER_AGENT`` 가 지방세
요청에도 적용돼 이름만 보고는 범위를 알 수 없었다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# ─── 출처 주소 (환경변수 대상 아님) ──────────────────────────────────────────

#: 국세 — 국세청 국세법령정보시스템
NTS_ORIGIN = "https://taxlaw.nts.go.kr"
ACTION_URL = f"{NTS_ORIGIN}/action.do"

#: 지방세 — 한국지방세연구원(KILF) 지방세 법령정보시스템
OLTA_ORIGIN = "https://www.olta.re.kr"


# ─── 환경변수 읽기 ───────────────────────────────────────────────────────────

def _env_int(*names: str, default: int, minimum: int = 1) -> int:
    """앞선 이름이 우선한다. 사이트별 → 공통 순서로 넘긴다.

    값이 숫자가 아니거나 최솟값보다 작으면 그 이름은 건너뛴다 — 잘못 준 값 때문에
    서버가 못 뜨는 것보다 다음 후보나 기본값으로 도는 편이 낫다.
    """
    for name in names:
        raw = os.environ.get(name)
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value >= minimum:
            return value
    return default


def _env_str(*names: str, default: str) -> str:
    for name in names:
        raw = os.environ.get(name)
        if raw:
            return raw
    return default


_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ─── 사이트별 설정 ───────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SiteConfig:
    """한 출처에 대한 조회 정책.

    사이트마다 별도 객체를 두는 이유는 두 곳이 다른 기관의 서버라서다. 한쪽 조회가
    다른 쪽의 요청 한도를 깎거나 타임아웃 정책을 끌고 가면 안 된다.
    """

    #: 로그·오류 메시지에 쓰는 이름
    name: str
    origin: str
    timeout_seconds: float
    retries: int
#: 지수 백오프 기준값. 대기 시간이 업스트림 왕복(0.3~1초)보다 지나치게 길지 않게 잡았다.
    retry_base_seconds: float
    rate_per_min: int
    rate_burst: int
    #: 본문 응답 상한(글자). 도구의 body_limit 인수가 없을 때 쓰는 기본값.
    body_limit: int
    user_agent: str


def _site(name: str, origin: str, prefix: str, *, default_body_limit: int) -> SiteConfig:
    """`<PREFIX>_*` → `TAXLAW_*` → 기본값 순으로 읽어 사이트 설정을 만든다."""
    return SiteConfig(
        name=name,
        origin=origin,
        timeout_seconds=_env_int(
            f"{prefix}_TIMEOUT_MS", "TAXLAW_TIMEOUT_MS", default=20_000
        ) / 1000,
        retries=_env_int(f"{prefix}_RETRIES", "TAXLAW_RETRIES", default=3, minimum=0),
        retry_base_seconds=_env_int(
            f"{prefix}_RETRY_BASE_MS", "TAXLAW_RETRY_BASE_MS", default=300
        ) / 1000,
        rate_per_min=_env_int(f"{prefix}_RATE_PER_MIN", "TAXLAW_RATE_PER_MIN", default=60),
        rate_burst=_env_int(f"{prefix}_RATE_BURST", "TAXLAW_RATE_BURST", default=20),
        body_limit=_env_int(
            f"{prefix}_BODY_LIMIT", "TAXLAW_BODY_LIMIT",
            default=default_body_limit, minimum=500,
        ),
        user_agent=_env_str(f"{prefix}_USER_AGENT", "TAXLAW_USER_AGENT", default=_DEFAULT_USER_AGENT),
    )


#: 국세청. 본문이 최대 1.4MB(심사청구 일부)까지 나오므로 상한이 필요하다.
NTS = _site("국세법령정보시스템", NTS_ORIGIN, "NTS", default_body_limit=30_000)

#: 한국지방세연구원. 본문이 국세청보다 작지만 같은 기본값으로 둔다.
OLTA = _site("지방세 법령정보시스템", OLTA_ORIGIN, "OLTA", default_body_limit=30_000)


# ─── 공통 설정 ───────────────────────────────────────────────────────────────

#: 두 사이트가 캐시 객체 하나를 공유하며 키에 출처를 담아 구분한다.
#: 그래서 사이트별 재정의가 성립하지 않는다. `NTS_CACHE_MAX` 는 이전 이름 호환용.
CACHE_MAX_ENTRIES: int = _env_int("TAXLAW_CACHE_MAX", "NTS_CACHE_MAX", default=600)

#: :func:`korean_taxlaw_mcp.html_text.truncate` 가 호출자에게 상한을 못 받았을 때의
#: 마지막 안전망. 정상 경로에서는 도메인 계층이 사이트별 `body_limit` 을 넘긴다.
FALLBACK_BODY_LIMIT: int = min(NTS.body_limit, OLTA.body_limit)


# ─── 응답 크기 정책 (LLM 컨텍스트 토큰 예산) ─────────────────────────────────
# 검색 결과는 LLM 이 후보를 고르는 목록이다. 기본값을 낮게 잡고, 더 필요하면
# 호출자가 limit 을 명시한다. 값을 조정할 때는 여기 한 곳만 바꾼다.

#: 검색 도구(해석례·결정례·지방세·서식)의 기본 페이지 크기.
DEFAULT_SEARCH_LIMIT: int = 10

#: 기본통칙·집행기준·고시·훈령 조회의 기본 반환 조항 수.
DEFAULT_GUIDANCE_LIMIT: int = 20

#: NOT_FOUND 응답에 담는 유사문서 최대 개수.
DEFAULT_SIMILAR_LIMIT: int = 5
