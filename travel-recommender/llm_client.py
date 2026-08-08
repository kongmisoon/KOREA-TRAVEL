"""LLM(Google Gemini - generateContent) 호출 담당 모듈.

이 모듈이 책임지는 것
    * Gemini REST API 에 POST 요청을 보내고 응답 텍스트를 꺼내오는 일
    * 1단계: 여행지 추천을 **JSON 스키마에 맞춰** 받아오는 일 (파싱 실패 시 1회 재시도)
    * 3단계: 최종 리포트(Markdown) 텍스트를 받아오는 일

공식 SDK(google-genai 패키지) 대신 ``requests`` 로 직접 호출하는 이유는,
"REST API 요청/응답이 실제로 어떻게 생겼는지"를 눈으로 보기 위해서다(학습용).

Gemini API 의 특징 (OpenAI 계열과 다른 점 - 학습 포인트)
    1. 모델 이름이 **URL 경로**에 들어간다: ``/v1beta/models/{model}:generateContent``
    2. 인증은 ``x-goog-api-key`` **헤더**로 한다.
       (``?key=...`` 처럼 URL 에 붙이는 방법도 있지만, URL 은 서버 접근 로그·프록시
        기록에 그대로 남기 때문에 키를 URL 에 넣지 않는 헤더 방식이 훨씬 안전하다.)
    3. 요청 본문은 ``contents`` / ``systemInstruction`` / ``generationConfig`` 구조다.
    4. 응답은 ``candidates[0].content.parts[*].text`` 에 들어 있다.
    5. **키가 틀리면 401 이 아니라 400(API_KEY_INVALID)** 이 온다. 그래서 상태 코드만
       보지 않고 본문의 오류 코드까지 확인해야 인증 오류를 제대로 분류할 수 있다.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import requests

from common import (
    DEFAULT_GEMINI_MODEL,
    ENV_GEMINI_API_KEY,
    ENV_GEMINI_MODEL,
    ERROR_API,
    ERROR_AUTH,
    ERROR_NETWORK,
    ERROR_PARSE,
    GEMINI_API_BASE_URL,
    MAX_EVENT_COUNT,
    MAX_JSON_RETRY,
    MIN_EVENT_COUNT,
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

#: 응답 최대 토큰 수. 넉넉히 잡는다 - 부족하면 응답이 중간에 잘려(MAX_TOKENS)
#: JSON 파싱이 실패하거나 리포트 섹션이 통째로 사라진다.
RECOMMENDATION_MAX_TOKENS: int = 1200
REPORT_MAX_TOKENS: int = 3000

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

#: 응답이 비어 있을 때 원인을 사람 말로 설명하기 위한 표.
#: (Gemini 는 안전 필터·길이 제한에 걸리면 200 OK 를 주면서 본문만 비워 보낸다)
FINISH_REASON_MESSAGES: Dict[str, str] = {
    "SAFETY": "안전 필터에 의해 응답이 차단되었습니다.",
    "RECITATION": "저작권 보호 정책에 의해 응답이 차단되었습니다.",
    "MAX_TOKENS": "응답이 최대 길이에 걸려 잘렸습니다. maxOutputTokens 를 늘려 보세요.",
    "OTHER": "알 수 없는 이유로 응답이 중단되었습니다.",
}


def get_model_name() -> str:
    """사용할 LLM 모델 이름을 환경변수에서 읽어온다.

    Returns:
        ``GEMINI_MODEL`` 환경변수 값. 없으면 기본 모델 이름.
    """
    return os.getenv(ENV_GEMINI_MODEL) or DEFAULT_GEMINI_MODEL


def build_endpoint(model: str) -> str:
    """모델 이름으로 generateContent 엔드포인트 URL 을 만든다.

    Gemini 는 OpenAI 와 달리 모델 이름을 요청 본문이 아니라 **URL 경로**에 넣는다.

    Args:
        model: 모델 이름 (예: ``"gemini-2.0-flash"``).

    Returns:
        완성된 엔드포인트 URL.
    """
    return f"{GEMINI_API_BASE_URL}/{model}:generateContent"


def classify_llm_http_status(status_code: int, body: str) -> str:
    """Gemini 의 HTTP 오류를 프로젝트 공통 오류 타입으로 분류한다.

    Gemini 는 **API 키가 잘못되어도 400** 을 돌려주기 때문에, 상태 코드만으로는
    "요청을 잘못 만든 것"과 "키가 틀린 것"을 구분할 수 없다. 그래서 본문에
    ``API_KEY_INVALID`` 같은 표식이 있는지까지 확인한다.

    Args:
        status_code: HTTP 상태 코드.
        body: 응답 본문 문자열.

    Returns:
        ``AUTH_ERROR`` / ``QUOTA_ERROR`` / ``API_ERROR`` 중 하나.
    """
    upper_body = body.upper()
    auth_markers = ("API_KEY_INVALID", "API KEY NOT VALID", "PERMISSION_DENIED", "UNAUTHENTICATED")
    if status_code == 400 and any(marker in upper_body for marker in auth_markers):
        return ERROR_AUTH
    # 나머지(401/403 → 인증, 429 → 쿼터)는 공통 규칙을 그대로 쓴다.
    return classify_http_status(status_code)


# ---------------------------------------------------------------------------
# 저수준: REST API 호출
# ---------------------------------------------------------------------------


def extract_text_from_response(data: Dict[str, Any]) -> str:
    """Gemini 응답 JSON 에서 생성된 텍스트를 꺼낸다.

    Gemini 는 200 OK 를 주면서도 본문이 비어 있을 수 있다(안전 필터, 길이 초과 등).
    그래서 ``data["candidates"][0]...`` 를 한 번에 인덱싱하지 않고 단계별로 확인한다.

    Args:
        data: ``response.json()`` 결과.

    Returns:
        생성된 텍스트.

    Raises:
        ValueError: 텍스트를 꺼낼 수 없는 경우(차단·길이 초과·구조 불일치).
    """
    # 1) 프롬프트 자체가 차단된 경우 candidates 가 아예 없다.
    block_reason = (data.get("promptFeedback") or {}).get("blockReason")
    if block_reason:
        raise ValueError(f"프롬프트가 차단되었습니다(blockReason={block_reason}).")

    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("응답에 candidates 가 없습니다.")

    candidate = candidates[0]
    parts = ((candidate.get("content") or {}).get("parts")) or []
    # parts 는 여러 조각으로 나뉘어 올 수 있으므로 모두 이어 붙인다.
    texts = [part.get("text", "") for part in parts if isinstance(part, dict)]
    text = "".join(texts).strip()

    if not text:
        finish_reason = candidate.get("finishReason", "OTHER")
        detail = FINISH_REASON_MESSAGES.get(finish_reason, f"finishReason={finish_reason}")
        raise ValueError(f"응답 본문이 비어 있습니다. {detail}")

    return text


def call_generate_content(
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
    step: str,
    errors: List[Dict[str, str]],
    force_json: bool = False,
) -> Optional[str]:
    """Gemini generateContent API 에 POST 요청을 보내고 답변 텍스트를 돌려준다.

    POST 를 쓰는 이유: 프롬프트처럼 길고 구조화된 데이터를 "요청 본문(body)"에
    담아 보내야 하기 때문이다. GET 은 URL 쿼리스트링만 쓰므로 적합하지 않다.

    Args:
        system_prompt: 모델의 역할/규칙을 정의하는 시스템 지시문.
        user_prompt: 실제 요청 내용.
        temperature: 창의성 정도(0에 가까울수록 일관적).
        max_tokens: 응답 최대 길이.
        step: 오류 기록용 단계 이름.
        errors: 오류를 누적할 리스트.
        force_json: True 면 모델에게 JSON 형식으로만 답하도록 강제한다(1단계 전용).

    Returns:
        모델이 생성한 문자열. 실패하면 ``None`` (오류는 errors 에 기록된다).
    """
    api_key = os.getenv(ENV_GEMINI_API_KEY, "")
    # 키를 읽는 지점에서 곧바로 마스킹 대상으로 등록한다(로그 유출 방지 이중 안전장치).
    register_secret(api_key)

    headers = {
        # 키를 URL 이 아닌 헤더에 담는다. URL 은 각종 로그에 그대로 남기 때문이다.
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }

    generation_config: Dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
    }
    if force_json:
        # Gemini 가 제공하는 구조화 출력 기능. "코드블록/설명 없이 JSON만" 이라는
        # 프롬프트 지시를 API 차원에서 한 번 더 보강해 준다.
        # 다만 이걸 켰다고 검증을 생략하면 안 된다 - 키 이름이나 타입은 여전히 틀릴 수 있다.
        generation_config["responseMimeType"] = "application/json"

    payload: Dict[str, Any] = {
        # systemInstruction: 대화 내용과 분리해서 "역할·규칙"만 담는 자리.
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": generation_config,
    }

    try:
        # timeout 을 반드시 지정한다. 없으면 서버가 응답하지 않을 때 영원히 멈춘다.
        response = requests.post(
            build_endpoint(get_model_name()),
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
        error_type = classify_llm_http_status(response.status_code, response.text)
        add_error(errors, step, error_type, describe_http_error(response.status_code, response.text))
        # 사용자가 바로 조치할 수 있도록 타입별 안내를 콘솔에 덧붙인다.
        if error_type == ERROR_AUTH:
            print("  [!] 인증 실패. GEMINI_API_KEY 설정을 확인하세요.")
        elif response.status_code == 404:
            print(f"  [!] 모델 '{get_model_name()}' 을 찾을 수 없습니다. GEMINI_MODEL 값을 확인하세요.")
        return None

    # 여기서부터는 200 OK. 그래도 응답이 비어 있을 수 있으므로 방어적으로 접근한다.
    try:
        data = response.json()
    except ValueError as exc:
        add_error(errors, step, ERROR_API, f"LLM 응답이 JSON 이 아닙니다: {truncate_for_log(exc)}")
        return None

    try:
        return extract_text_from_response(data)
    except ValueError as exc:
        add_error(errors, step, ERROR_API, f"LLM 응답에서 텍스트를 꺼내지 못했습니다: {truncate_for_log(exc)}")
        return None


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
        LLM 에 보낼 프롬프트 문자열.
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

        content = call_generate_content(
            system_prompt=RECOMMENDATION_SYSTEM_PROMPT,
            user_prompt=build_recommendation_prompt(date, strict=is_retry),
            temperature=RECOMMENDATION_TEMPERATURE,
            max_tokens=RECOMMENDATION_MAX_TOKENS,
            step=STEP_RECOMMEND,
            errors=errors,
            force_json=True,  # 1단계는 JSON 만 필요하므로 API 차원에서도 강제한다.
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
        system_prompt: 시스템 지시문.
        user_prompt: 리포트 생성 요청 프롬프트.
        errors: 오류를 누적할 리스트.
        step: 오류 기록용 단계 이름.

    Returns:
        Markdown 문자열. 실패하면 ``None``.
    """
    return call_generate_content(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=REPORT_TEMPERATURE,
        max_tokens=REPORT_MAX_TOKENS,
        step=step,
        errors=errors,
        force_json=False,  # 리포트는 Markdown 이므로 JSON 강제를 걸면 안 된다.
    )
