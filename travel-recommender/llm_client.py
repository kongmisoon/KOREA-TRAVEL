"""LLM(OpenAI Chat Completions) 호출 담당 모듈.

이 모듈이 책임지는 것
    * OpenAI REST API 에 POST 요청을 보내고 응답 텍스트를 꺼내오는 일
    * 1단계: 여행지 추천을 **JSON 스키마에 맞춰** 받아오는 일 (파싱 실패 시 1회 재시도)
    * 3단계: 최종 리포트(Markdown) 텍스트를 받아오는 일

공식 SDK(openai 패키지) 대신 ``requests`` 로 직접 호출하는 이유는,
"REST API 요청/응답이 실제로 어떻게 생겼는지"를 눈으로 보기 위해서다(학습용).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import requests

from common import (
    DEFAULT_OPENAI_MODEL,
    ENV_OPENAI_API_KEY,
    ENV_OPENAI_MODEL,
    ERROR_API,
    ERROR_AUTH,
    ERROR_NETWORK,
    ERROR_PARSE,
    MAX_EVENT_COUNT,
    MAX_JSON_RETRY,
    MIN_EVENT_COUNT,
    OPENAI_CHAT_COMPLETIONS_URL,
    REQUEST_TIMEOUT_SECONDS,
    STEP_RECOMMEND,
    add_error,
    classify_http_status,
    describe_http_error,
    register_secret,
    truncate_for_log,
)

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

#: 1차 추천 JSON 이 반드시 가져야 하는 키 목록.
REQUIRED_RECOMMENDATION_KEYS = ("recommended_city", "weather", "events", "reason")

#: 추천 단계 온도. 값이 낮을수록 답이 일관적이라 JSON 형식을 잘 지킨다.
RECOMMENDATION_TEMPERATURE: float = 0.4

#: 리포트 단계 온도. 문장을 쓰는 단계라 조금 더 높게 준다.
REPORT_TEMPERATURE: float = 0.6

#: 응답 최대 토큰 수(리포트는 길어질 수 있으므로 넉넉히).
RECOMMENDATION_MAX_TOKENS: int = 800
REPORT_MAX_TOKENS: int = 2000

#: 파싱 실패 시 사용할 기본값(fallback). 프로그램이 멈추지 않게 하는 안전망.
FALLBACK_RECOMMENDATION: Dict[str, Any] = {
    "recommended_city": "서울",
    "weather": "날씨 정보 없음 (LLM 응답 파싱 실패)",
    "events": ["행사 정보 없음"],
    "reason": (
        "LLM 응답을 JSON 으로 파싱하지 못해 기본값으로 대체했습니다. "
        "추천 근거는 신뢰할 수 없으므로 참고용으로만 사용하세요."
    ),
}


def get_model_name() -> str:
    """사용할 LLM 모델 이름을 환경변수에서 읽어온다.

    Returns:
        ``OPENAI_MODEL`` 환경변수 값. 없으면 기본 모델 이름.
    """
    return os.getenv(ENV_OPENAI_MODEL) or DEFAULT_OPENAI_MODEL


# ---------------------------------------------------------------------------
# 저수준: REST API 호출
# ---------------------------------------------------------------------------


def call_chat_completion(
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    step: str,
    errors: List[Dict[str, str]],
) -> Optional[str]:
    """OpenAI Chat Completions API 에 POST 요청을 보내고 답변 텍스트를 돌려준다.

    POST 를 쓰는 이유: 프롬프트처럼 길고 구조화된 데이터를 "요청 본문(body)"에
    담아 보내야 하기 때문이다. GET 은 URL 쿼리스트링만 쓰므로 적합하지 않다.

    Args:
        system_prompt: 모델의 역할/규칙을 정의하는 system 메시지.
        user_prompt: 실제 요청 내용을 담은 user 메시지.
        temperature: 창의성 정도(0에 가까울수록 일관적).
        max_tokens: 응답 최대 길이.
        step: 오류 기록용 단계 이름.
        errors: 오류를 누적할 리스트.

    Returns:
        모델이 생성한 문자열. 실패하면 ``None`` (오류는 errors 에 기록된다).
    """
    api_key = os.getenv(ENV_OPENAI_API_KEY, "")
    # 키를 읽는 지점에서 곧바로 마스킹 대상으로 등록한다(로그 유출 방지 이중 안전장치).
    register_secret(api_key)
    headers = {
        # Bearer 토큰 방식 인증. 키는 절대 URL 이나 로그에 넣지 않고 헤더로만 보낸다.
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "model": get_model_name(),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        # timeout 을 반드시 지정한다. 없으면 서버가 응답하지 않을 때 영원히 멈춘다.
        response = requests.post(
            OPENAI_CHAT_COMPLETIONS_URL,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.Timeout as exc:
        add_error(errors, step, ERROR_NETWORK, f"LLM 요청 타임아웃: {truncate_for_log(exc)}")
        return None
    except requests.exceptions.RequestException as exc:
        # DNS 실패, 연결 거부, SSL 오류 등 네트워크 계열 예외를 한 번에 처리한다.
        add_error(errors, step, ERROR_NETWORK, f"LLM 네트워크 오류: {truncate_for_log(exc)}")
        return None

    if response.status_code != 200:
        error_type = classify_http_status(response.status_code)
        add_error(errors, step, error_type, describe_http_error(response.status_code, response.text))
        if error_type == ERROR_AUTH:
            print("  [!] 인증 실패. OPENAI_API_KEY 설정을 확인하세요.")
        return None

    # 여기서부터는 200 OK. 그래도 응답 구조가 예상과 다를 수 있으므로 방어적으로 접근한다.
    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        add_error(
            errors,
            step,
            ERROR_API,
            f"LLM 응답 구조가 예상과 다릅니다: {truncate_for_log(exc)}",
        )
        return None

    if not isinstance(content, str) or not content.strip():
        add_error(errors, step, ERROR_API, "LLM 이 빈 응답을 반환했습니다.")
        return None

    return content


# ---------------------------------------------------------------------------
# 1단계: 여행지 추천 JSON
# ---------------------------------------------------------------------------

RECOMMENDATION_SYSTEM_PROMPT = (
    "당신은 한국 국내 여행 전문 플래너입니다. "
    "사용자가 준 날짜에 가장 어울리는 국내 여행지를 추천합니다. "
    "당신은 오직 JSON 만 출력하는 API 처럼 동작해야 합니다."
)


def build_recommendation_prompt(date: str, strict: bool = False) -> str:
    """1단계 추천 요청 프롬프트를 만든다.

    Args:
        date: ``YYYY-MM-DD`` 형식의 여행 날짜.
        strict: 재시도 여부. True 면 "설명 없이 순수 JSON만" 이라는 더 강한 제약을 붙인다.

    Returns:
        LLM 에 보낼 user 프롬프트 문자열.
    """
    base_prompt = f"""여행 날짜: {date}

이 시기에 가기 좋은 한국 국내 여행지 한 곳을 추천하고, 아래 JSON 스키마를 정확히 지켜서 출력하세요.

{{
  "recommended_city": "제주",
  "weather": "3월 중순 평균 15도 내외, 바람이 있으나 온화함",
  "events": ["유채꽃 축제", "봄 시즌 지역 행사"],
  "reason": "추천 근거 2~4문장"
}}

규칙:
- recommended_city: 문자열. 도시/지역 이름 하나만.
- weather: 문자열. 해당 시기의 평균 기온과 체감 날씨를 한 문장으로.
- events: 문자열 배열. {MIN_EVENT_COUNT}개 이상 {MAX_EVENT_COUNT}개 이하.
- reason: 문자열. 추천 근거를 2~4문장으로.
- 위 4개 키 외의 키는 절대 추가하지 마세요.
- 마크다운 코드블록(```), 설명 문장, 인사말 없이 순수 JSON 만 출력하세요."""

    if not strict:
        return base_prompt

    # 재시도 프롬프트: 1차 응답이 파싱에 실패했다는 사실을 알려주고 제약을 더 강하게 건다.
    return (
        base_prompt
        + "\n\n[중요] 직전 응답은 JSON 파싱에 실패했습니다. "
        "설명, 주석, 코드블록 표시를 모두 제거하고 "
        "recommended_city, weather, events, reason 4개 키만 담긴 순수 JSON 으로 다시 출력하세요. "
        "응답의 첫 글자는 '{' 이고 마지막 글자는 '}' 여야 합니다."
    )


def extract_json_block(text: str) -> str:
    """LLM 응답 문자열에서 JSON 부분만 잘라낸다.

    "순수 JSON만 출력하라"고 지시해도 모델이 ```json 코드블록이나 인사말을
    덧붙이는 경우가 있다. 여기서 가장 바깥쪽 중괄호 구간만 뽑아내 파싱 성공률을 높인다.
    (재시도 횟수를 늘리는 것보다 이런 전처리가 훨씬 싸고 빠르다.)

    Args:
        text: LLM 원본 응답.

    Returns:
        JSON 으로 추정되는 부분 문자열. 못 찾으면 원본을 그대로 돌려준다.
    """
    stripped = text.strip()

    # 1) ```json ... ``` 코드블록 제거
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]  # 첫 줄(``` 또는 ```json) 제거
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    # 2) 앞뒤에 설명 문장이 붙은 경우, 첫 '{' 부터 마지막 '}' 까지만 사용
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return stripped[start : end + 1]
    return stripped


def validate_recommendation(data: Any) -> Dict[str, Any]:
    """1차 추천 JSON 의 구조와 타입을 검증하고 정규화한다.

    json.loads 가 성공해도 "키가 빠졌다 / 타입이 다르다"면 다음 단계가 깨진다.
    그래서 파싱과 별개로 스키마 검증을 반드시 거친다.

    Args:
        data: ``json.loads`` 결과 객체.

    Returns:
        검증을 통과한 추천 dict.

    Raises:
        ValueError: 스키마를 만족하지 않는 경우.
    """
    if not isinstance(data, dict):
        raise ValueError("최상위 값이 JSON 객체(dict)가 아닙니다.")

    missing = [key for key in REQUIRED_RECOMMENDATION_KEYS if key not in data]
    if missing:
        raise ValueError(f"필수 키 누락: {', '.join(missing)}")

    city = data["recommended_city"]
    weather = data["weather"]
    events = data["events"]
    reason = data["reason"]

    if not isinstance(city, str) or not city.strip():
        raise ValueError("recommended_city 는 비어 있지 않은 문자열이어야 합니다.")
    if not isinstance(weather, str) or not weather.strip():
        raise ValueError("weather 는 비어 있지 않은 문자열이어야 합니다.")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason 은 비어 있지 않은 문자열이어야 합니다.")
    if not isinstance(events, list) or not events:
        raise ValueError("events 는 1개 이상의 항목을 가진 배열이어야 합니다.")
    if not all(isinstance(event, str) and event.strip() for event in events):
        raise ValueError("events 의 모든 항목은 비어 있지 않은 문자열이어야 합니다.")

    # 개수 제한은 오류로 처리하지 않고 잘라서 사용한다(정상 추천을 버릴 이유가 없다).
    normalized_events = [event.strip() for event in events][:MAX_EVENT_COUNT]

    return {
        "recommended_city": city.strip(),
        "weather": weather.strip(),
        "events": normalized_events,
        "reason": reason.strip(),
    }


def parse_recommendation(text: str) -> Dict[str, Any]:
    """LLM 응답 문자열을 추천 dict 로 변환한다.

    Args:
        text: LLM 원본 응답 문자열.

    Returns:
        검증까지 통과한 추천 dict.

    Raises:
        ValueError: JSON 파싱 또는 스키마 검증에 실패한 경우.
    """
    json_text = extract_json_block(text)
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 파싱 실패: {exc.msg} (line {exc.lineno})") from exc
    return validate_recommendation(data)


def get_recommendation(date: str, errors: List[Dict[str, str]]) -> Dict[str, Any]:
    """1단계 - LLM 으로 여행지/날씨/행사 추천 JSON 을 얻는다.

    재시도 정책: 최초 1회 + 실패 시 강한 제약 프롬프트로 1회, 총 최대 2회만 호출한다.
    (무한 재시도는 비용과 지연을 통제할 수 없어 금지)

    Args:
        date: ``YYYY-MM-DD`` 형식의 여행 날짜.
        errors: 오류를 누적할 리스트.

    Returns:
        추천 dict. 두 번 모두 실패하면 ``FALLBACK_RECOMMENDATION`` 의 복사본.
    """
    total_attempts = 1 + MAX_JSON_RETRY  # 최초 시도 + 재시도 허용 횟수

    for attempt in range(total_attempts):
        is_retry = attempt > 0
        if is_retry:
            print("  - JSON 파싱에 실패해 더 강한 제약 프롬프트로 1회 재시도합니다.")

        content = call_chat_completion(
            system_prompt=RECOMMENDATION_SYSTEM_PROMPT,
            user_prompt=build_recommendation_prompt(date, strict=is_retry),
            temperature=RECOMMENDATION_TEMPERATURE,
            max_tokens=RECOMMENDATION_MAX_TOKENS,
            step=STEP_RECOMMEND,
            errors=errors,
        )
        if content is None:
            # 호출 자체가 실패한 경우(네트워크/인증 등). 오류는 이미 기록되었다.
            continue

        try:
            return parse_recommendation(content)
        except ValueError as exc:
            add_error(
                errors,
                STEP_RECOMMEND,
                ERROR_PARSE,
                f"{attempt + 1}차 응답 파싱 실패: {truncate_for_log(exc)}",
            )

    # 여기까지 왔다면 모든 시도가 실패한 것 → 기본값으로 진행한다(프로그램은 멈추지 않는다).
    add_error(
        errors,
        STEP_RECOMMEND,
        ERROR_PARSE,
        "추천 JSON 을 얻지 못해 기본값(fallback)으로 진행합니다.",
    )
    return dict(FALLBACK_RECOMMENDATION)


# ---------------------------------------------------------------------------
# 3단계: 최종 리포트 Markdown
# ---------------------------------------------------------------------------


def generate_report_markdown(
    system_prompt: str,
    user_prompt: str,
    errors: List[Dict[str, str]],
    step: str,
) -> Optional[str]:
    """3단계 - LLM 으로 최종 리포트 Markdown 을 생성한다.

    프롬프트 본문 조립은 ``report.build_report_prompt()`` 가 담당한다.
    (이 함수는 "호출"만 책임진다 - 역할 분리)

    Args:
        system_prompt: system 메시지.
        user_prompt: 리포트 생성 요청 프롬프트.
        errors: 오류를 누적할 리스트.
        step: 오류 기록용 단계 이름.

    Returns:
        Markdown 문자열. 실패하면 ``None``.
    """
    return call_chat_completion(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=REPORT_TEMPERATURE,
        max_tokens=REPORT_MAX_TOKENS,
        step=step,
        errors=errors,
    )
