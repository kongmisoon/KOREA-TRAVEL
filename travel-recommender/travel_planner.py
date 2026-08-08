"""국내 여행 추천 CLI - 프로그램 진입점(entry point).

실행 예::

    python travel_planner.py --date "2026-03-15"

3단계 파이프라인
    [1/3] LLM  : 날짜 → 추천 도시 / 날씨 / 행사 / 이유 (JSON 구조화 출력)
    [2/3] Kakao: 추천 도시 → "{도시} 맛집" 검색 → 공통 스키마로 정규화
    [3/3] LLM  : 1·2단계 결과 → 최종 여행 리포트(Markdown)

설계 원칙
    * 외부 호출은 실패할 수 있다는 것을 전제로 짠다. 실패는 ``errors`` 에 기록하고
      파이프라인은 계속 진행한다(맛집 0건이어도 리포트는 나온다).
    * API 키는 코드가 아니라 ``.env`` / 환경변수에서만 읽는다.
    * 로그에는 키 값이 절대 그대로 찍히지 않는다(마스킹).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

import llm_client
import place_client
import report
from common import (
    DATE_FORMAT,
    ENV_GEMINI_API_KEY,
    ENV_KAKAO_REST_API_KEY,
    ERROR_FALLBACK,
    ERROR_IO,
    RESTAURANT_COUNT,
    RESULTS_DIR,
    STEP_REPORT,
    STEP_SAVE,
    add_error,
    mask_api_key,
    register_secret,
    truncate_for_log,
)

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

#: 이 프로그램이 반드시 필요로 하는 환경변수 목록 (이름, 발급 안내).
REQUIRED_ENV_VARS: Tuple[Tuple[str, str], ...] = (
    (ENV_GEMINI_API_KEY, "https://aistudio.google.com/apikey 에서 무료 발급"),
    (ENV_KAKAO_REST_API_KEY, "https://developers.kakao.com 내 애플리케이션 > 앱 키 > REST API 키"),
)

#: 종료 코드. 0 = 정상, 1 = 사용자 입력/설정 오류.
EXIT_OK: int = 0
EXIT_USAGE_ERROR: int = 1

#: 결과 파일 이름 템플릿.
RAW_FILENAME_TEMPLATE: str = "{date}_raw.json"
REPORT_FILENAME_TEMPLATE: str = "{date}_travel_plan.md"

#: 이 파일이 있는 디렉터리. results/ 와 .env 를 실행 위치와 무관하게 찾기 위해 사용한다.
BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# CLI 인자 처리
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    """argparse 파서를 만든다.

    Returns:
        ``--date`` 옵션이 정의된 ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        prog="travel_planner.py",
        description="여행 날짜를 입력하면 국내 여행지 추천 + 맛집 + 리포트를 생성합니다.",
        epilog='예시: python travel_planner.py --date "2026-03-15"',
    )
    parser.add_argument(
        "--date",
        required=True,
        metavar="YYYY-MM-DD",
        help='여행 날짜 (형식: YYYY-MM-DD, 예: "2026-03-15")',
    )
    return parser


def validate_date(date_text: str, parser: argparse.ArgumentParser) -> str:
    """날짜 문자열 형식을 검증한다.

    ``datetime.strptime`` 은 형식이 맞지 않으면 ValueError 를 던진다. 이를 이용해
    "2026-13-45" 같은 존재하지 않는 날짜까지 한 번에 걸러낸다.

    Args:
        date_text: 사용자가 입력한 날짜 문자열.
        parser: usage 출력을 위한 파서.

    Returns:
        정규화된 ``YYYY-MM-DD`` 문자열.

    Raises:
        SystemExit: 형식이 잘못된 경우 usage 를 출력하고 종료 코드 1로 종료한다.
    """
    try:
        parsed = datetime.strptime(date_text, DATE_FORMAT)
    except ValueError:
        print(f"[오류] 날짜 형식이 올바르지 않습니다: {date_text!r}", file=sys.stderr)
        print("       형식은 YYYY-MM-DD 여야 합니다. (예: 2026-03-15)", file=sys.stderr)
        parser.print_usage(sys.stderr)
        raise SystemExit(EXIT_USAGE_ERROR)

    # strptime 을 통과한 값을 다시 문자열로 만들어, 파일 이름에 쓰기 좋게 정규화한다.
    return parsed.strftime(DATE_FORMAT)


# ---------------------------------------------------------------------------
# 환경변수(API 키) 검사
# ---------------------------------------------------------------------------


def load_environment() -> None:
    """``.env`` 파일을 읽어 환경변수로 로드한다.

    실행 위치와 무관하게 동작하도록 이 스크립트가 있는 폴더의 ``.env`` 를 본다.
    이미 셸에 export 된 환경변수가 있으면 그 값이 우선한다(python-dotenv 기본 동작).
    """
    load_dotenv(os.path.join(BASE_DIR, ".env"))


def check_api_keys() -> Dict[str, str]:
    """필수 API 키가 모두 설정되어 있는지 검사한다.

    키가 하나라도 없으면 **작업을 시작하기 전에** 종료한다. 절반쯤 진행한 뒤
    실패하면 사용자만 혼란스럽기 때문이다(fail fast).

    Returns:
        {환경변수 이름: 키 값} 딕셔너리.

    Raises:
        SystemExit: 키가 하나라도 없으면 안내 메시지를 출력하고 종료 코드 1로 종료한다.
    """
    keys: Dict[str, str] = {}
    missing: List[Tuple[str, str]] = []

    for env_name, how_to_get in REQUIRED_ENV_VARS:
        value = os.getenv(env_name, "").strip()
        if not value:
            missing.append((env_name, how_to_get))
        else:
            keys[env_name] = value
            # 이후 로그에 이 값이 섞여 나가면 자동으로 마스킹되도록 등록한다.
            register_secret(value)

    if missing:
        print("[오류] 필수 API 키가 설정되지 않았습니다.\n", file=sys.stderr)
        for env_name, how_to_get in missing:
            print(f"  - {env_name} (발급: {how_to_get})", file=sys.stderr)
        print(
            "\n설정 방법 (택 1)\n"
            "  1) .env 파일 사용 (권장)\n"
            "       cp .env.example .env\n"
            "       # .env 를 열어 각 키 값을 채운다. .env 는 .gitignore 로 보호된다.\n"
            "  2) 셸 환경변수로 직접 지정\n"
            "       macOS/Linux    : export GEMINI_API_KEY=\"YOUR_KEY\"\n"
            "       Windows PowerShell: $env:GEMINI_API_KEY=\"YOUR_KEY\"\n",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_USAGE_ERROR)

    # 키 "값"은 절대 그대로 출력하지 않는다. 앞 4자리만 남긴 마스킹 형태로만 확인시켜 준다.
    for env_name, value in keys.items():
        print(f"  - {env_name}: {mask_api_key(value)} (확인됨)")

    return keys


# ---------------------------------------------------------------------------
# 결과 저장
# ---------------------------------------------------------------------------


def ensure_results_dir() -> str:
    """``results/`` 폴더를 만들고 경로를 돌려준다.

    Returns:
        results 폴더의 절대 경로.
    """
    results_path = os.path.join(BASE_DIR, RESULTS_DIR)
    # exist_ok=True 이므로 이미 있어도 예외가 발생하지 않는다(재실행 안전).
    os.makedirs(results_path, exist_ok=True)
    return results_path


def save_raw_json(
    results_path: str,
    date: str,
    recommendation: Dict[str, Any],
    restaurants: List[Dict[str, Any]],
    errors: List[Dict[str, str]],
) -> Optional[str]:
    """원본 데이터를 ``results/{date}_raw.json`` 으로 저장한다.

    Args:
        results_path: results 폴더 경로.
        date: 여행 날짜.
        recommendation: 1단계 추천 JSON.
        restaurants: 2단계 맛집 리스트.
        errors: 누적된 오류 리스트.

    Returns:
        저장된 파일 경로. 실패하면 ``None``.
    """
    payload = {
        "date": date,
        "recommendation": recommendation,
        "restaurants": restaurants,
        "errors": errors,
    }
    file_path = os.path.join(results_path, RAW_FILENAME_TEMPLATE.format(date=date))
    try:
        with open(file_path, "w", encoding="utf-8") as file:
            # ensure_ascii=False 로 한글이 \uXXXX 로 깨지지 않게 저장한다.
            json.dump(payload, file, ensure_ascii=False, indent=2)
    except OSError as exc:
        add_error(errors, STEP_SAVE, ERROR_IO, f"raw JSON 저장 실패: {truncate_for_log(exc)}")
        return None
    return file_path


def save_markdown(
    results_path: str,
    date: str,
    markdown: str,
    errors: List[Dict[str, str]],
) -> Optional[str]:
    """최종 리포트를 ``results/{date}_travel_plan.md`` 로 저장한다.

    Args:
        results_path: results 폴더 경로.
        date: 여행 날짜.
        markdown: 최종 Markdown 문자열.
        errors: 누적된 오류 리스트.

    Returns:
        저장된 파일 경로. 실패하면 ``None``.
    """
    file_path = os.path.join(results_path, REPORT_FILENAME_TEMPLATE.format(date=date))
    try:
        with open(file_path, "w", encoding="utf-8") as file:
            file.write(markdown)
    except OSError as exc:
        add_error(errors, STEP_SAVE, ERROR_IO, f"리포트 저장 실패: {truncate_for_log(exc)}")
        return None
    return file_path


# ---------------------------------------------------------------------------
# 파이프라인 3단계
# ---------------------------------------------------------------------------


def run_pipeline(date: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]], str, List[Dict[str, str]]]:
    """3단계 파이프라인을 순서대로 실행한다.

    Args:
        date: 검증을 통과한 여행 날짜.

    Returns:
        ``(추천 JSON, 맛집 리스트, 최종 Markdown, 오류 리스트)`` 튜플.
    """
    # 모든 단계가 공유하는 오류 수집 리스트. 마지막에 JSON/리포트 양쪽에 실린다.
    errors: List[Dict[str, str]] = []

    # ---------------- [1/3] LLM 여행지 추천 ----------------
    print(f"\n[1/3] LLM 으로 {date} 여행지를 추천받는 중... (모델: {llm_client.get_model_name()})")
    recommendation = llm_client.get_recommendation(date, errors)
    print(f"  - 추천 지역: {recommendation['recommended_city']}")
    print(f"  - 날씨: {recommendation['weather']}")
    print(f"  - 행사: {', '.join(recommendation['events'])}")

    # ---------------- [2/3] 맛집 검색 ----------------
    city = recommendation["recommended_city"]
    print(f"\n[2/3] Kakao Local 로 '{city} 맛집' 검색 중... (최대 {RESTAURANT_COUNT}곳)")
    restaurants = place_client.search_restaurants(city, errors)
    print(f"  - {report.summarize_restaurants_for_console(restaurants)}")

    # ---------------- [3/3] 최종 리포트 생성 ----------------
    print("\n[3/3] LLM 으로 최종 리포트(Markdown) 생성 중...")
    markdown = llm_client.generate_report_markdown(
        system_prompt=report.REPORT_SYSTEM_PROMPT,
        user_prompt=report.build_report_prompt(date, recommendation, restaurants),
        errors=errors,
        step=STEP_REPORT,
    )

    if markdown is None:
        # LLM 이 실패해도 결과물은 나와야 한다 → 코드로 만든 기본 리포트로 대체한다.
        add_error(
            errors,
            STEP_REPORT,
            ERROR_FALLBACK,
            "LLM 리포트 생성 실패 → 로컬 기본 리포트로 대체했습니다.",
        )
        final_markdown = report.build_fallback_report(date, recommendation, restaurants, errors)
    else:
        # LLM 출력은 형식이 흔들릴 수 있으므로 필수 섹션을 코드가 최종 검수한다.
        final_markdown = report.finalize_report(markdown, date, recommendation, restaurants, errors)
        print("  - 리포트 생성 완료 (필수 섹션 검수 완료)")

    return recommendation, restaurants, final_markdown, errors


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    """프로그램 진입 함수.

    Returns:
        종료 코드(0 = 정상).
    """
    parser = build_arg_parser()
    args = parser.parse_args()

    print("=" * 60)
    print(" 국내 여행 추천 CLI (LLM + 장소 검색 API)")
    print("=" * 60)

    # 1) 날짜 형식 검증 (실패 시 usage 출력 후 종료 코드 1)
    date = validate_date(args.date, parser)

    # 2) API 키 검사 (실패 시 설정 안내 후 종료 코드 1)
    print("\n[준비] API 키 확인 중...")
    load_environment()
    check_api_keys()

    # 3) 3단계 파이프라인 실행
    recommendation, restaurants, markdown, errors = run_pipeline(date)

    # 4) 결과 저장
    results_path = ensure_results_dir()
    # 리포트를 먼저 저장한다. 저장 중 오류가 나면 그 오류까지 raw JSON 에 담기 위해서다.
    report_file = save_markdown(results_path, date, markdown, errors)
    raw_file = save_raw_json(results_path, date, recommendation, restaurants, errors)

    # 5) 마무리 안내
    print("\n" + "=" * 60)
    print(" 완료! 결과 파일이 저장되었습니다.")
    print("=" * 60)
    print(f"  - 원본 데이터 : {raw_file or '저장 실패'}")
    print(f"  - 여행 리포트 : {report_file or '저장 실패'}")
    print(f"  - 기록된 오류 : {len(errors)}건" + (" (없음)" if not errors else " → 리포트 하단 참고"))
    return EXIT_OK


if __name__ == "__main__":
    # SystemExit 은 argparse/검증 함수가 직접 던지므로 그대로 전파시킨다.
    sys.exit(main())
