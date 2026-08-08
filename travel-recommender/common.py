"""프로젝트 전역에서 공유하는 상수와 헬퍼 함수 모음.

이 모듈에는 다음 세 가지가 들어있다.

1. 상수(constant)  : 매직 넘버/매직 스트링을 코드 곳곳에 흩뿌리지 않기 위해 한곳에 모았다.
2. 오류 수집 헬퍼  : ``add_error()`` 로 모든 오류를 같은 형식(dict)으로 기록한다.
3. 비밀값 마스킹    : 로그/에러 메시지에 API 키가 그대로 찍히는 사고를 막는다.

왜 별도 모듈로 뺐는가?
    ``llm_client`` / ``place_client`` / ``report`` 모두가 이 헬퍼들을 필요로 하는데,
    진입점인 ``travel_planner`` 에 두면 순환 import(circular import)가 발생한다.
    "여러 모듈이 공통으로 쓰는 것"은 가장 아래층 모듈에 두는 것이 안전하다.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Set

# ---------------------------------------------------------------------------
# 상수 정의 (매직 넘버 금지 - 의미 있는 이름을 붙여 한곳에서 관리한다)
# ---------------------------------------------------------------------------

#: CLI 에서 받는 날짜 형식. datetime.strptime 검증에도 그대로 쓰인다.
DATE_FORMAT: str = "%Y-%m-%d"

#: 결과 파일을 저장할 디렉터리 이름.
RESULTS_DIR: str = "results"

#: 맛집을 몇 곳까지 추출할 것인가.
RESTAURANT_COUNT: int = 5

#: 모든 외부 HTTP 호출의 타임아웃(초). 지정하지 않으면 응답이 없을 때 영원히 멈춘다.
REQUEST_TIMEOUT_SECONDS: int = 20

#: LLM 이 JSON 을 잘못 뱉었을 때 허용하는 "재시도 횟수". 무한 재시도 금지 → 1회만.
MAX_JSON_RETRY: int = 1

#: 1차 추천 JSON 의 events 배열 개수 제한 (요구사항: 1~3개).
MIN_EVENT_COUNT: int = 1
MAX_EVENT_COUNT: int = 3

# --- 환경변수 이름 (키 "값"이 아니라 "이름"만 코드에 적는다) ---
ENV_GEMINI_API_KEY: str = "GEMINI_API_KEY"
ENV_KAKAO_REST_API_KEY: str = "KAKAO_REST_API_KEY"
ENV_GEMINI_MODEL: str = "GEMINI_MODEL"

#: GEMINI_MODEL 환경변수가 없을 때 사용할 기본 모델.
#: gemini-2.0-flash 는 무료 티어에서 쓸 수 있고 응답이 빠르며,
#: "생각(thinking) 토큰"을 쓰지 않아 짧은 응답이 잘려 나갈 위험이 적다.
DEFAULT_GEMINI_MODEL: str = "gemini-2.0-flash"

# --- 외부 API 엔드포인트 ---
#: Gemini 는 모델 이름이 URL 경로에 들어간다.
#: 최종 형태: {BASE}/{model}:generateContent
GEMINI_API_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/models"
KAKAO_KEYWORD_SEARCH_URL: str = "https://dapi.kakao.com/v2/local/search/keyword.json"

# --- 파이프라인 단계 이름 (errors 기록 시 "step" 값으로 사용) ---
STEP_RECOMMEND: str = "recommend"
STEP_PLACE_SEARCH: str = "place_search"
STEP_REPORT: str = "report"
STEP_SAVE: str = "save"

# --- 오류 타입 (errors 기록 시 "type" 값으로 사용) ---
ERROR_AUTH: str = "AUTH_ERROR"
ERROR_QUOTA: str = "QUOTA_ERROR"
ERROR_NETWORK: str = "NETWORK_ERROR"
ERROR_PARSE: str = "PARSE_ERROR"
ERROR_EMPTY_RESULT: str = "EMPTY_RESULT"
ERROR_API: str = "API_ERROR"
ERROR_IO: str = "IO_ERROR"
ERROR_FALLBACK: str = "FALLBACK"

#: 키를 마스킹할 때 앞에서 몇 글자를 남길 것인가.
MASK_VISIBLE_PREFIX: int = 4


# ---------------------------------------------------------------------------
# 비밀값(secret) 마스킹
# ---------------------------------------------------------------------------

#: 프로그램이 실행 중 다루는 비밀값 목록. 로그로 나가기 전에 이 값들을 지운다.
_REGISTERED_SECRETS: Set[str] = set()


def mask_api_key(key: str) -> str:
    """API 키를 로그에 안전하게 찍을 수 있는 형태로 마스킹한다.

    앞 4자리만 남기고 나머지는 ``*`` 로 가린다. 키가 너무 짧으면 전부 가린다.

    Args:
        key: 마스킹할 원본 키 문자열.

    Returns:
        예) ``"sk-p****************"`` 형태의 마스킹된 문자열.
    """
    if not key:
        return "(빈 값)"
    if len(key) <= MASK_VISIBLE_PREFIX:
        # 짧은 키는 앞자리를 남기면 사실상 전체가 노출되므로 통째로 가린다.
        return "*" * len(key)
    return key[:MASK_VISIBLE_PREFIX] + "*" * (len(key) - MASK_VISIBLE_PREFIX)


def register_secret(value: str) -> None:
    """마스킹 대상 비밀값을 등록한다.

    프로그램 시작 시 읽어온 API 키들을 등록해 두면, 이후 ``scrub_secrets()`` 가
    모든 로그/에러 메시지에서 해당 값을 자동으로 가려준다.
    (사람이 실수로 키를 print 하는 사고를 코드 차원에서 한 번 더 막는 안전장치)

    Args:
        value: 등록할 비밀값. 빈 문자열이면 무시한다.
    """
    if value:
        _REGISTERED_SECRETS.add(value)


def scrub_secrets(text: str) -> str:
    """문자열에 등록된 비밀값이 섞여 있으면 마스킹된 형태로 치환한다.

    Args:
        text: 검사할 문자열(주로 예외 메시지나 API 응답 본문).

    Returns:
        비밀값이 마스킹된 문자열.
    """
    safe_text = text
    for secret in _REGISTERED_SECRETS:
        if secret in safe_text:
            safe_text = safe_text.replace(secret, mask_api_key(secret))
    return safe_text


# ---------------------------------------------------------------------------
# 오류 수집
# ---------------------------------------------------------------------------


def add_error(
    errors: List[Dict[str, str]],
    step: str,
    error_type: str,
    message: str,
) -> None:
    """오류를 errors 리스트에 dict 형태로 기록하고 콘솔에도 알린다.

    이 프로젝트의 핵심 원칙은 "오류가 나도 파이프라인은 계속 진행한다"이다.
    다만 ``except Exception: pass`` 처럼 조용히 삼키면 나중에 디버깅이 불가능하므로,
    반드시 (1) errors 리스트에 남기고 (2) 사용자에게도 즉시 보여준다.

    Args:
        errors: 오류를 누적할 리스트. 이 리스트가 그대로 결과 JSON 에 저장된다.
        step: 오류가 발생한 파이프라인 단계 (예: ``"place_search"``).
        error_type: 오류 분류 (예: ``"AUTH_ERROR"``).
        message: 사람이 읽을 수 있는 설명. 비밀값은 자동으로 마스킹된다.
    """
    safe_message = scrub_secrets(message)
    errors.append({"step": step, "type": error_type, "message": safe_message})
    # stderr 로 보내면 표준 출력(진행 상황)과 오류를 파이프라인에서 분리할 수 있다.
    print(f"  [!] {step} / {error_type}: {safe_message}", file=sys.stderr)


def describe_http_error(status_code: int, body: str) -> str:
    """HTTP 오류 응답을 로그용 한 줄 메시지로 요약한다.

    응답 본문이 지나치게 길면 잘라내고, 비밀값은 마스킹한다.

    Args:
        status_code: HTTP 상태 코드.
        body: 응답 본문 문자열.

    Returns:
        ``"HTTP 401: {...}"`` 형태의 요약 문자열.
    """
    snippet = scrub_secrets(body.strip().replace("\n", " "))
    max_length = 200
    if len(snippet) > max_length:
        snippet = snippet[:max_length] + "..."
    return f"HTTP {status_code}: {snippet}"


def classify_http_status(status_code: int) -> str:
    """HTTP 상태 코드를 프로젝트 공통 오류 타입으로 변환한다.

    Args:
        status_code: HTTP 상태 코드.

    Returns:
        ``AUTH_ERROR`` / ``QUOTA_ERROR`` / ``API_ERROR`` 중 하나.
    """
    if status_code in (401, 403):
        # 401 = 인증 정보 없음/틀림, 403 = 인증은 됐지만 권한 없음. 둘 다 키 설정 문제.
        return ERROR_AUTH
    if status_code == 429:
        # 429 = Too Many Requests. 호출량 초과 또는 요금제 한도 소진.
        return ERROR_QUOTA
    return ERROR_API


def truncate_for_log(value: Any, limit: int = 300) -> str:
    """임의의 값을 로그에 넣기 좋은 짧은 문자열로 바꾼다.

    Args:
        value: 문자열로 만들 값.
        limit: 최대 길이.

    Returns:
        길이가 제한된 안전한 문자열.
    """
    text = scrub_secrets(str(value))
    if len(text) > limit:
        return text[:limit] + "..."
    return text
