"""장소 검색(Kakao Local - 키워드 검색) 담당 모듈.

이 모듈이 책임지는 것
    * ``"{도시} 맛집"`` 키워드로 Kakao Local API 를 GET 호출하는 일
    * 제공자마다 다른 응답 필드를 **우리 프로젝트 공통 스키마**로 변환(normalize)하는 일
    * 모든 실패를 errors 에 기록하되, 프로그램은 절대 멈추지 않게 하는 일

핵심 설계 원칙
    외부 API 응답을 그대로 다음 단계로 넘기면, 나중에 제공자를 Naver 로 바꿀 때
    리포트 생성 코드까지 전부 고쳐야 한다. 그래서 "정규화 함수" 한 곳에서만
    제공자 의존성을 흡수하고, 바깥에는 항상 같은 모양의 dict 를 내보낸다.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests

from common import (
    ENV_KAKAO_REST_API_KEY,
    ERROR_AUTH,
    ERROR_EMPTY_RESULT,
    ERROR_NETWORK,
    ERROR_QUOTA,
    KAKAO_KEYWORD_SEARCH_URL,
    REQUEST_TIMEOUT_SECONDS,
    RESTAURANT_COUNT,
    STEP_PLACE_SEARCH,
    add_error,
    classify_http_status,
    describe_http_error,
    register_secret,
    truncate_for_log,
)

#: 검색 쿼리 템플릿. "{도시} 맛집" 형태로 조립한다.
SEARCH_QUERY_TEMPLATE: str = "{city} 맛집"

#: Kakao 키워드 검색이 한 번에 돌려줄 수 있는 최대 개수(API 제약: 1~15).
KAKAO_MAX_PAGE_SIZE: int = 15


def build_search_query(city: str) -> str:
    """도시 이름으로 검색 쿼리 문자열을 만든다.

    Args:
        city: 1단계에서 추천받은 도시 이름.

    Returns:
        ``"제주 맛집"`` 형태의 검색어.
    """
    return SEARCH_QUERY_TEMPLATE.format(city=city.strip())


def _to_float(value: Any) -> Optional[float]:
    """좌표 문자열을 float 으로 변환한다(실패하면 None).

    Kakao 는 좌표 x/y 를 **문자열**로 내려준다. 공통 스키마에서는 number 로
    다뤄야 하므로 여기서 변환한다.

    Args:
        value: 변환할 값.

    Returns:
        변환된 실수. 변환 불가능하면 ``None``.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_kakao_document(document: Dict[str, Any]) -> Dict[str, Any]:
    """Kakao Local 응답 1건을 프로젝트 공통 스키마로 변환한다.

    공통 스키마::

        {
          "name": str, "address": str, "category": str, "url": str,
          "x": float|None, "y": float|None,      # x=경도(lng), y=위도(lat)
          "lng": float|None, "lat": float|None,  # 읽기 쉬운 별칭
        }

    Args:
        document: Kakao ``documents`` 배열의 원소 하나.

    Returns:
        정규화된 맛집 dict.
    """
    # 도로명 주소가 비어 있는 경우가 있어 지번 주소로 대체한다.
    road_address = (document.get("road_address_name") or "").strip()
    lot_address = (document.get("address_name") or "").strip()
    address = road_address or lot_address or "주소 정보 없음"

    longitude = _to_float(document.get("x"))  # Kakao: x = 경도(longitude)
    latitude = _to_float(document.get("y"))  # Kakao: y = 위도(latitude)

    return {
        "name": (document.get("place_name") or "이름 정보 없음").strip(),
        "address": address,
        "category": (document.get("category_name") or "카테고리 정보 없음").strip(),
        "url": (document.get("place_url") or "").strip(),
        "x": longitude,
        "y": latitude,
        "lng": longitude,
        "lat": latitude,
    }


def search_restaurants(
    city: str,
    errors: List[Dict[str, str]],
    count: int = RESTAURANT_COUNT,
) -> List[Dict[str, Any]]:
    """2단계 - 추천 도시의 맛집을 검색해 공통 스키마 리스트로 돌려준다.

    이 함수는 **어떤 상황에서도 예외를 밖으로 던지지 않는다.** 실패하면 오류를
    기록하고 빈 리스트를 돌려주어 3단계가 정상적으로 이어지게 한다.

    Args:
        city: 검색할 도시 이름.
        errors: 오류를 누적할 리스트.
        count: 추출할 맛집 개수.

    Returns:
        정규화된 맛집 dict 리스트. 실패하거나 결과가 없으면 빈 리스트 ``[]``.
    """
    api_key = os.getenv(ENV_KAKAO_REST_API_KEY, "")
    # 키를 읽는 지점에서 곧바로 마스킹 대상으로 등록해 둔다.
    # (응답 본문에 키가 그대로 섞여 돌아오는 API 도 있기 때문에 방어적으로 한 번 더)
    register_secret(api_key)
    query = build_search_query(city)

    headers = {
        # Kakao 는 "KakaoAK {REST_API_KEY}" 형식의 Authorization 헤더를 요구한다.
        "Authorization": f"KakaoAK {api_key}",
    }
    params = {
        "query": query,
        # size 는 API 허용 범위(1~15) 안으로 맞춘다.
        "size": max(1, min(count, KAKAO_MAX_PAGE_SIZE)),
        "category_group_code": "FD6",  # FD6 = 음식점. 카페/관광지 등이 섞이는 것을 방지.
    }

    try:
        # 검색은 "데이터를 조회"하는 동작이므로 GET + 쿼리스트링(params)이 자연스럽다.
        response = requests.get(
            KAKAO_KEYWORD_SEARCH_URL,
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.Timeout as exc:
        add_error(
            errors,
            STEP_PLACE_SEARCH,
            ERROR_NETWORK,
            f"장소 검색 타임아웃({REQUEST_TIMEOUT_SECONDS}초): {truncate_for_log(exc)}",
        )
        return []
    except requests.exceptions.RequestException as exc:
        add_error(
            errors,
            STEP_PLACE_SEARCH,
            ERROR_NETWORK,
            f"장소 검색 네트워크 오류: {truncate_for_log(exc)}",
        )
        return []

    if response.status_code != 200:
        error_type = classify_http_status(response.status_code)
        add_error(
            errors,
            STEP_PLACE_SEARCH,
            error_type,
            describe_http_error(response.status_code, response.text),
        )
        # 사용자가 바로 조치할 수 있도록 타입별 안내를 콘솔에 덧붙인다.
        if error_type == ERROR_AUTH:
            print("  [!] 인증 실패. 키 설정을 확인하세요. (KAKAO_REST_API_KEY / REST API 키인지 확인)")
        elif error_type == ERROR_QUOTA:
            print("  [!] 호출 한도를 초과했습니다. 잠시 후 다시 시도하세요.")
        return []

    try:
        data = response.json()
    except ValueError as exc:
        add_error(
            errors,
            STEP_PLACE_SEARCH,
            ERROR_NETWORK,
            f"장소 검색 응답이 JSON 이 아닙니다: {truncate_for_log(exc)}",
        )
        return []

    documents = data.get("documents")
    if not isinstance(documents, list) or not documents:
        add_error(
            errors,
            STEP_PLACE_SEARCH,
            ERROR_EMPTY_RESULT,
            f"'{query}' 검색 결과가 0건입니다.",
        )
        return []

    # 원본 응답을 그대로 쓰지 않고 반드시 공통 스키마로 변환해서 내보낸다.
    restaurants = [normalize_kakao_document(document) for document in documents[:count]]
    return restaurants
