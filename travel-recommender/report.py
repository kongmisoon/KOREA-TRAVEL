"""최종 리포트(Markdown) 조립 담당 모듈.

이 모듈이 책임지는 것
    * 3단계 LLM 프롬프트 조립 (맛집 목록/추천 JSON 을 텍스트로 정리)
    * LLM 이 만든 Markdown 의 **필수 섹션 누락을 보정**하는 일
    * "오류 요약(errors)" 섹션을 프로그램이 직접 붙이는 일
    * LLM 호출이 완전히 실패했을 때 쓰는 **로컬 fallback 리포트** 생성

왜 오류 요약 섹션은 LLM 에게 맡기지 않는가?
    3단계 자체에서 발생한 오류(예: 리포트 생성 타임아웃)는 LLM 이 알 수 없다.
    또 오류 기록은 "사실"이라 창작이 개입하면 안 된다. 그래서 이 섹션만은
    errors 리스트를 기반으로 코드가 직접 렌더링한다.
"""

from __future__ import annotations

from typing import Any, Dict, List

from common import RESTAURANT_COUNT

# ---------------------------------------------------------------------------
# 상수: 리포트에 반드시 들어가야 하는 섹션 제목
# ---------------------------------------------------------------------------

#: LLM 이 생성해야 하는 섹션들(오류 요약 제외 - 그건 코드가 붙인다).
LLM_SECTION_TITLES = (
    "## 추천 지역",
    "## 추천 이유",
    "## 날씨 요약",
    "## 행사/축제",
    "## 맛집 추천",
    "## 1일 일정 제안",
)

#: 코드가 직접 렌더링하는 섹션 제목.
ERROR_SECTION_TITLE = "## 오류 요약(errors)"

#: 맛집 검색 결과가 0건일 때 반드시 사용할 문구.
NO_RESTAURANT_TEXT = "데이터 없음 (장소 검색 결과 0건)"

REPORT_SYSTEM_PROMPT = (
    "당신은 한국 국내 여행 리포트를 작성하는 전문 여행 에디터입니다. "
    "제공된 데이터만 사용해 Markdown 문서를 작성하며, 없는 정보를 지어내지 않습니다."
)


def format_restaurants_for_prompt(restaurants: List[Dict[str, Any]]) -> str:
    """맛집 리스트를 프롬프트에 넣기 좋은 텍스트로 변환한다.

    Args:
        restaurants: 정규화된 맛집 dict 리스트(0건일 수 있음).

    Returns:
        번호가 매겨진 맛집 목록 문자열. 0건이면 ``"(목록 비어 있음)"``.
    """
    if not restaurants:
        return "(목록 비어 있음)"

    lines: List[str] = []
    for index, restaurant in enumerate(restaurants, start=1):
        lines.append(
            f"{index}. 이름: {restaurant.get('name', '')} / "
            f"주소: {restaurant.get('address', '')} / "
            f"분류: {restaurant.get('category', '')} / "
            f"링크: {restaurant.get('url', '')}"
        )
    return "\n".join(lines)


def build_report_prompt(
    date: str,
    recommendation: Dict[str, Any],
    restaurants: List[Dict[str, Any]],
) -> str:
    """3단계 리포트 생성 프롬프트를 조립한다.

    맛집이 0건일 때 LLM 이 없는 식당을 지어내지 않도록(hallucination 방지)
    "제공된 목록에 있는 곳만 사용하라"는 제약을 명시적으로 넣는 것이 핵심이다.

    Args:
        date: 여행 날짜(``YYYY-MM-DD``).
        recommendation: 1단계 추천 JSON.
        restaurants: 2단계 맛집 리스트(0건 가능).

    Returns:
        LLM 에 보낼 user 프롬프트 문자열.
    """
    events_text = ", ".join(recommendation.get("events", [])) or "(정보 없음)"
    restaurants_text = format_restaurants_for_prompt(restaurants)

    return f"""아래 데이터를 바탕으로 국내 여행 리포트를 Markdown 으로 작성하세요.

[입력 데이터]
- 여행 날짜: {date}
- 추천 지역: {recommendation.get('recommended_city', '')}
- 날씨: {recommendation.get('weather', '')}
- 행사/축제: {events_text}
- 추천 이유: {recommendation.get('reason', '')}
- 맛집 목록({len(restaurants)}건):
{restaurants_text}

[반드시 지킬 규칙]
1. 아래 제목과 섹션을 정확히 이 순서대로, 이 표기 그대로 사용하세요.

# {date} 국내 여행 추천 리포트
{chr(10).join(LLM_SECTION_TITLES)}

2. 맛집 추천 섹션에는 위 "맛집 목록"에 있는 곳만 사용하세요.
   목록에 없는 식당 이름을 절대 새로 만들어내지 마세요.
3. 맛집 목록이 비어 있으면 맛집 추천 섹션 본문에 정확히 다음 한 줄만 쓰세요.
   {NO_RESTAURANT_TEXT}
4. 1일 일정 제안은 오전 / 오후 / 저녁 세 구간으로 나눠 작성하세요.
5. 날씨와 행사 정보는 위 입력 데이터 범위 안에서만 서술하세요.
6. "오류 요약" 섹션은 쓰지 마세요. 그 섹션은 프로그램이 직접 추가합니다.
7. 코드블록(```) 으로 전체를 감싸지 말고 Markdown 본문만 출력하세요."""


def render_errors_section(errors: List[Dict[str, str]]) -> str:
    """errors 리스트를 Markdown 섹션 문자열로 변환한다.

    Args:
        errors: 누적된 오류 dict 리스트.

    Returns:
        ``## 오류 요약(errors)`` 제목을 포함한 Markdown 조각.
    """
    if not errors:
        return f"{ERROR_SECTION_TITLE}\n없음\n"

    lines = [ERROR_SECTION_TITLE, "", "| 단계(step) | 유형(type) | 메시지(message) |", "| --- | --- | --- |"]
    for error in errors:
        # 표가 깨지지 않도록 파이프 문자를 이스케이프한다.
        message = str(error.get("message", "")).replace("|", "\\|")
        lines.append(f"| {error.get('step', '')} | {error.get('type', '')} | {message} |")
    lines.append("")
    return "\n".join(lines)


def render_restaurants_section_body(restaurants: List[Dict[str, Any]]) -> str:
    """맛집 섹션 본문을 코드로 직접 렌더링한다(fallback / 섹션 보정용).

    Args:
        restaurants: 정규화된 맛집 리스트.

    Returns:
        Markdown 목록 문자열. 0건이면 "데이터 없음" 문구.
    """
    if not restaurants:
        return NO_RESTAURANT_TEXT

    lines: List[str] = []
    for index, restaurant in enumerate(restaurants, start=1):
        url = restaurant.get("url") or ""
        link_suffix = f" - [카카오맵 링크]({url})" if url else ""
        lines.append(
            f"{index}. **{restaurant.get('name', '')}** ({restaurant.get('category', '')})\n"
            f"   - 주소: {restaurant.get('address', '')}{link_suffix}"
        )
    return "\n".join(lines)


def strip_error_section(markdown: str) -> str:
    """LLM 이 임의로 만든 "오류 요약" 섹션을 잘라낸다.

    프롬프트로 금지해도 모델이 섹션을 추가하는 경우가 있다. 중복을 막기 위해
    해당 제목이 나오는 지점부터 뒤를 모두 버리고, 코드가 만든 섹션을 붙인다.

    Args:
        markdown: LLM 이 생성한 Markdown.

    Returns:
        오류 요약 섹션이 제거된 Markdown.
    """
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith("## 오류 요약"):
            return "\n".join(lines[:index]).rstrip() + "\n"
    return markdown.rstrip() + "\n"


def strip_code_fence(markdown: str) -> str:
    """전체가 ```로 감싸진 경우 바깥 코드블록만 제거한다.

    Args:
        markdown: LLM 원본 응답.

    Returns:
        코드블록 표시가 제거된 Markdown.
    """
    text = markdown.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def ensure_required_sections(
    markdown: str,
    date: str,
    recommendation: Dict[str, Any],
    restaurants: List[Dict[str, Any]],
) -> str:
    """필수 섹션이 빠졌으면 코드가 채워 넣는다.

    LLM 출력은 100% 보장되지 않으므로, "형식 계약"은 코드가 마지막에 검사한다.
    누락된 섹션은 입력 데이터로 최소한의 본문을 만들어 붙인다.

    Args:
        markdown: LLM 이 생성한 Markdown(오류 요약 제외 상태).
        date: 여행 날짜.
        recommendation: 1단계 추천 JSON.
        restaurants: 2단계 맛집 리스트.

    Returns:
        모든 필수 섹션이 존재하는 Markdown.
    """
    text = markdown

    # 문서 제목(H1)이 없으면 맨 앞에 붙인다.
    title = f"# {date} 국내 여행 추천 리포트"
    if title not in text:
        text = f"{title}\n\n{text.lstrip()}"

    # 섹션별 대체 본문(누락된 경우에만 사용).
    fallback_bodies: Dict[str, str] = {
        "## 추천 지역": recommendation.get("recommended_city", "정보 없음"),
        "## 추천 이유": recommendation.get("reason", "정보 없음"),
        "## 날씨 요약": recommendation.get("weather", "정보 없음"),
        "## 행사/축제": "\n".join(
            f"- {event}" for event in recommendation.get("events", [])
        )
        or "- 정보 없음",
        "## 맛집 추천": render_restaurants_section_body(restaurants),
        "## 1일 일정 제안": (
            "- 오전: 정보 없음\n- 오후: 정보 없음\n- 저녁: 정보 없음"
        ),
    }

    missing_blocks: List[str] = []
    for section_title in LLM_SECTION_TITLES:
        if section_title not in text:
            missing_blocks.append(f"{section_title}\n{fallback_bodies[section_title]}\n")

    if missing_blocks:
        text = text.rstrip() + "\n\n" + "\n".join(missing_blocks)

    return text.rstrip() + "\n"


def finalize_report(
    markdown: str,
    date: str,
    recommendation: Dict[str, Any],
    restaurants: List[Dict[str, Any]],
    errors: List[Dict[str, str]],
) -> str:
    """LLM 원본 Markdown 을 최종 리포트로 마무리한다.

    처리 순서: 코드블록 제거 → LLM 오류 섹션 제거 → 필수 섹션 보정 → 오류 요약 추가.

    Args:
        markdown: LLM 이 생성한 Markdown 원본.
        date: 여행 날짜.
        recommendation: 1단계 추천 JSON.
        restaurants: 2단계 맛집 리스트.
        errors: 누적된 오류 리스트.

    Returns:
        저장 가능한 최종 Markdown 문자열.
    """
    text = strip_code_fence(markdown)
    text = strip_error_section(text)
    text = ensure_required_sections(text, date, recommendation, restaurants)
    return f"{text}\n{render_errors_section(errors)}"


def build_fallback_report(
    date: str,
    recommendation: Dict[str, Any],
    restaurants: List[Dict[str, Any]],
    errors: List[Dict[str, str]],
) -> str:
    """LLM 리포트 생성이 실패했을 때 코드로 직접 리포트를 만든다.

    3단계까지 실패해도 사용자는 최소한 1·2단계 결과를 파일로 받아볼 수 있어야 한다.
    그래서 LLM 없이도 같은 섹션 구조를 만족하는 리포트를 만들어 둔다.

    Args:
        date: 여행 날짜.
        recommendation: 1단계 추천 JSON.
        restaurants: 2단계 맛집 리스트(0건 가능).
        errors: 누적된 오류 리스트.

    Returns:
        최종 Markdown 문자열.
    """
    events = recommendation.get("events", [])
    events_body = "\n".join(f"- {event}" for event in events) or "- 정보 없음"
    city = recommendation.get("recommended_city", "정보 없음")

    body = f"""# {date} 국내 여행 추천 리포트

> ⚠️ LLM 리포트 생성에 실패해, 수집된 데이터만으로 자동 생성한 기본 리포트입니다.

## 추천 지역
{city}

## 추천 이유
{recommendation.get('reason', '정보 없음')}

## 날씨 요약
{recommendation.get('weather', '정보 없음')}

## 행사/축제
{events_body}

## 맛집 추천
{render_restaurants_section_body(restaurants)}

## 1일 일정 제안
- 오전: {city} 주요 명소 둘러보기
- 오후: 지역 행사/축제 또는 대표 관광지 방문
- 저녁: 위 맛집 목록에서 한 곳을 골라 식사 (목록이 비어 있으면 현지에서 직접 탐색)
"""
    return f"{body.rstrip()}\n\n{render_errors_section(errors)}"


def summarize_restaurants_for_console(restaurants: List[Dict[str, Any]]) -> str:
    """콘솔 진행 로그에 쓸 짧은 요약을 만든다.

    Args:
        restaurants: 정규화된 맛집 리스트.

    Returns:
        ``"5곳 확보: 가게A, 가게B ..."`` 형태의 한 줄 요약.
    """
    if not restaurants:
        return f"0곳 (최대 {RESTAURANT_COUNT}곳 중) - 리포트에는 '데이터 없음'으로 표기됩니다."
    names = ", ".join(restaurant.get("name", "") for restaurant in restaurants)
    return f"{len(restaurants)}곳 확보: {names}"
