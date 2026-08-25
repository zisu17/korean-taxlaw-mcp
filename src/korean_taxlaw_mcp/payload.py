"""응답 payload 공용 헬퍼.

응답을 만드는 규칙(빈 값 제거, 유사문서 축소)이 도메인마다 조금씩 다른 관용구로
흩어지면 한쪽만 고쳐지는 드리프트가 생긴다. 그래서 여기 한 곳에 모은다.

- 빈 값 판정 기준: ``None`` / ``""`` / ``[]`` / ``{}`` 만 제거한다.
  ``False`` 와 ``0`` 은 의미 있는 값이므로 남긴다.
"""

from __future__ import annotations

from typing import Any

#: 제거 대상 빈 값. `in` 비교는 `0 == False` 함정이 있어 identity+equality 로 따진다.
_EMPTY = (None, "", [], {})


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    # bool 은 int 의 하위형이라 `value == 0` 류 비교에 휩쓸리지 않게 먼저 통과시킨다
    if isinstance(value, bool):
        return False
    return value == "" or value == [] or value == {}


def drop_empty(mapping: dict[str, Any]) -> dict[str, Any]:
    """한 단계 빈 값 제거 — 응답 항목·문서 dict 를 마무리할 때 쓴다."""
    return {k: v for k, v in mapping.items() if not _is_empty(v)}


def prune(value: Any) -> Any:
    """재귀 빈 값 제거 — 도구 응답 전체에 마지막으로 한 번 건다.

    각 빌더가 걸러내기를 잊어도 빈 필드가 LLM 컨텍스트로 새지 않게 하는
    응답 수준의 불변식이다.
    """
    if isinstance(value, dict):
        pruned = {k: prune(v) for k, v in value.items()}
        return {k: v for k, v in pruned.items() if not _is_empty(v)}
    if isinstance(value, list):
        return [prune(v) for v in value]
    return value


def slim(item: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """유사문서 축소 projection.

    NOT_FOUND 의 유사문서는 '이런 별개 문서가 있다'는 신호이므로 식별 필드만 남긴다.
    국세·지방세가 같은 함수를 쓰게 해 두 응답의 형태가 따로 놀지 않게 한다.
    """
    return {k: item[k] for k in keys if not _is_empty(item.get(k))}
