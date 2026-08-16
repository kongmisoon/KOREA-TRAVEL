# 🧳 국내 여행 추천 CLI (travel-recommender)

여행 날짜 하나만 입력하면 **LLM → 장소 검색 API → LLM** 3단계 파이프라인을 거쳐
그 시기에 어울리는 국내 여행지, 날씨, 행사, 맛집, 1일 일정이 담긴
**Markdown 여행 리포트**를 자동으로 만들어 주는 터미널 프로그램입니다.

Python 백엔드 학습용 프로젝트로, 다음을 실습하는 것이 목표입니다.

- REST API 호출(GET / POST)과 요청·응답 구조 이해
- LLM 출력을 **JSON 으로 구조화**해 다음 단계 입력으로 넘기는 파이프라인 설계
- 외부 API 오류(인증 / 쿼터 / 네트워크 / 파싱) 대응 원칙
- API 키를 코드에 쓰지 않고 `.env` · 환경변수로 관리하는 습관

---

## 1. 프로그램 개요 및 동작 흐름

```
사용자 입력: --date "2026-03-15"
        │
        ▼
┌──────────────────────────────────────────────────────────────┐
│ [1/3] LLM (Google Gemini generateContent)                     │
│   입력: 날짜                                                  │
│   출력: {recommended_city, weather, events[], reason} JSON    │
│   실패 시: 강한 제약 프롬프트로 1회만 재시도 → 그래도 실패하면 기본값 │
└──────────────────────────────────────────────────────────────┘
        │  recommended_city
        ▼
┌──────────────────────────────────────────────────────────────┐
│ [2/3] 장소 검색 (Kakao Local 키워드 검색)                       │
│   입력: "{추천도시} 맛집"                                       │
│   출력: 맛집 5곳 → 공통 스키마로 정규화                          │
│   실패 시: errors 에 기록하고 빈 배열([])로 계속 진행             │
└──────────────────────────────────────────────────────────────┘
        │  추천 JSON + 맛집 리스트(0건 가능)
        ▼
┌──────────────────────────────────────────────────────────────┐
│ [3/3] LLM (최종 리포트 생성)                                    │
│   출력: Markdown 리포트 (추천지역/이유/날씨/행사/맛집/일정/오류)    │
│   실패 시: 수집된 데이터로 로컬 기본 리포트 생성                   │
└──────────────────────────────────────────────────────────────┘
        │
        ▼
  results/{date}_raw.json      (원본 데이터 + 오류 기록)
  results/{date}_travel_plan.md (최종 리포트)
```

### 핵심 설계 원칙

| 원칙 | 구현 |
| --- | --- |
| 외부 API 는 언제든 실패한다 | 모든 호출을 `try-except` + `timeout=20초` 로 감싼다 |
| 재시도는 "고쳐질 오류"에만 | 5xx(서버 일시 장애)만 2초→4초로 최대 2회. 429·4xx 는 재시도하지 않는다 |
| 실패해도 멈추지 않는다 | 오류는 `errors` 리스트에 기록하고 다음 단계로 진행 |
| 조용한 실패 금지 | `add_error()` 가 리스트에 남기고 **콘솔에도 즉시 출력** |
| LLM 출력은 못 믿는다 | JSON 스키마 검증 + 리포트 필수 섹션 누락 시 코드가 보정 |
| 키는 코드에 없다 | `.env` → `os.getenv()` 로만 읽고, 로그에서는 앞 4자리만 노출 |

### 파일 구조

```
travel-recommender/
├── travel_planner.py   # 실행 진입점: CLI 파싱, 키 검사, 파이프라인 실행, 결과 저장
├── llm_client.py       # Gemini REST 호출, 추천 JSON 파싱/검증/재시도
├── place_client.py     # Kakao Local 검색, 응답 정규화, 오류 분류
├── report.py           # 리포트 프롬프트 조립, 섹션 보정, fallback 리포트
├── common.py           # 공통 상수, add_error(), 키 마스킹
├── demo_data.py        # --demo 모드에서 쓰는 예시(mock) 데이터
├── requirements.txt
├── .env.example        # 키 "이름"만 들어있음 (실제 값 절대 금지)
├── .gitignore          # .env, results/ 포함
├── README.md
└── results/            # 실행 시 자동 생성 (git 추적 제외)
```

---

## 2. 설치 방법

Python **3.10 이상**이 필요합니다.

```bash
# 1) 프로젝트 폴더로 이동
cd travel-recommender

# 2) (권장) 가상환경 생성 및 활성화
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows

# 3) 의존성 설치
pip install -r requirements.txt
```

설치되는 라이브러리는 `requests`(HTTP 호출)와 `python-dotenv`(.env 로딩) 둘뿐입니다.
FastAPI·Flask 같은 웹 프레임워크는 사용하지 않습니다(터미널 전용 프로그램).

---

## 3. API 키 발급 및 `.env` 설정

### 3-1. Google Gemini API 키 (무료)

1. <https://aistudio.google.com/apikey> 접속 후 Google 계정으로 로그인
2. **API 키 만들기 (Create API key)** 클릭
3. 생성된 `AIza...` 키를 복사
4. **카드 등록 없이 무료 티어로 사용할 수 있습니다.** 대신 분당/일일 호출 횟수 제한이
   있어, 짧은 시간에 여러 번 실행하면 `429`(QUOTA_ERROR)가 날 수 있습니다.

### 3-2. Kakao Local REST API 키

1. <https://developers.kakao.com> 접속 후 로그인
2. **내 애플리케이션 → 애플리케이션 추가하기**
3. 생성한 앱 → **앱 키** 탭 → **REST API 키** 복사
   - ⚠️ JavaScript 키 / Admin 키가 아니라 **REST API 키**입니다.
4. 카카오맵 장소 검색은 별도 신청 없이 REST API 키로 바로 사용할 수 있습니다.

### 3-3. `.env` 파일 만들기 (권장)

```bash
cp .env.example .env
```

`.env` 파일을 열어 값을 채웁니다. **형식은 반드시 `KEY_NAME=your_api_key_here` 한 줄씩**입니다.

```dotenv
GEMINI_API_KEY=your_api_key_here
KAKAO_REST_API_KEY=your_api_key_here
GEMINI_MODEL=gemini-flash-latest
```

> `.env` 는 `.gitignore` 에 등록되어 있어 커밋되지 않습니다.
> 이 README 와 `.env.example` 에는 **절대 실제 키 값을 적지 마세요.**

### 3-4. `.env` 없이 셸 환경변수로 지정하기

```bash
# macOS / Linux
export GEMINI_API_KEY="YOUR_KEY"
export KAKAO_REST_API_KEY="YOUR_KEY"
```

```powershell
# Windows PowerShell
$env:GEMINI_API_KEY="YOUR_KEY"
$env:KAKAO_REST_API_KEY="YOUR_KEY"
```

셸에 이미 설정된 환경변수가 있으면 `.env` 값보다 **셸 값이 우선**합니다.
`export` 방식은 해당 터미널 세션에서만 유효합니다.

---

## 4. 실행 방법

```bash
python travel_planner.py --date "2026-03-15"
```

| 옵션 | 필수 | 설명 |
| --- | --- | --- |
| `--date` | ✅ | 여행 날짜. `YYYY-MM-DD` 형식 |
| `--demo` | | **API 키 없이** 내장 예시 데이터로 실행 (아래 4-1 참고) |
| `-h`, `--help` | | 도움말 출력 |

### 4-1. 키가 아직 없다면 — 데모 모드로 먼저 확인하기

키를 발급받기 전에 "이 프로그램이 뭘 만들어 내는지" 먼저 보고 싶다면:

```bash
python travel_planner.py --date "2026-03-15" --demo
```

- 외부 API 를 **한 번도 호출하지 않습니다.** 따라서 키도, 인터넷도 필요 없습니다.
- 대신 `demo_data.py` 에 들어 있는 예시 데이터를 사용합니다.
- 예시 데이터는 **실제 API 가 돌려주는 날것의 형태**(LLM 의 코드블록 섞인 응답,
  Kakao 의 `place_name`/문자열 좌표)로 저장돼 있어, JSON 파싱 → 스키마 검증 →
  정규화 → 섹션 보정까지 **실제와 똑같은 코드 경로**를 지납니다.
  즉 데모가 성공하면 "적어도 내 코드 로직은 정상"이라고 판단할 수 있습니다.
- 결과 파일은 실제 실행 결과와 헷갈리지 않도록 이름이 다릅니다.
  ```
  results/2026-03-15_demo_raw.json
  results/2026-03-15_demo_travel_plan.md
  ```
  리포트 맨 위에도 🧪 데모 표식이 붙습니다.

> ⚠️ 데모 모드의 맛집은 **실재하지 않는 예시**입니다. 실제 결과가 필요하면
> 키를 설정하고 `--demo` 없이 실행하세요.

### 실행 예시 출력

```text
============================================================
 국내 여행 추천 CLI (LLM + 장소 검색 API)
============================================================

[준비] API 키 확인 중...
  - GEMINI_API_KEY: AQ.A******************** (확인됨)
  - KAKAO_REST_API_KEY: 1a2b****************** (확인됨)

[1/3] LLM 으로 2026-03-15 여행지를 추천받는 중... (모델: gemini-flash-latest)
  - 추천 지역: 광양
  - 날씨: 3월 중순 평균 기온은 10도에서 14도 내외로 포근하여 야외 활동을 즐기기에 좋습니다.
  - 행사: 광양매화축제, 섬진강 꽃길 트레킹

[2/3] Kakao Local 로 '제주 맛집' 검색 중... (최대 5곳)
  - 5곳 확보: 흑돼지거리 본점, 제주 해장국, 성산 물회, 올레국수, 고기국수집

[3/3] LLM 으로 최종 리포트(Markdown) 생성 중...
  - 리포트 생성 완료 (필수 섹션 검수 완료)

============================================================
 완료! 결과 파일이 저장되었습니다.
============================================================
  - 원본 데이터 : /.../travel-recommender/results/2026-03-15_raw.json
  - 여행 리포트 : /.../travel-recommender/results/2026-03-15_travel_plan.md
  - 기록된 오류 : 0건 (없음)
```

### 잘못된 입력을 넣으면

```bash
$ python travel_planner.py --date "2026-13-45"
[오류] 날짜 형식이 올바르지 않습니다: '2026-13-45'
       형식은 YYYY-MM-DD 여야 합니다. (예: 2026-03-15)
usage: travel_planner.py [-h] --date YYYY-MM-DD
$ echo $?
1
```

날짜 형식 오류와 키 미설정은 **종료 코드 1** 로 즉시 종료합니다.
반면 실행 중 발생하는 API 오류는 종료하지 않고 리포트에 기록됩니다.

---

## 5. 결과물 확인 방법

실행이 끝나면 `results/` 폴더가 자동 생성되고 파일 2개가 저장됩니다.

```
results/
├── 2026-03-15_raw.json        # 원본 데이터 (디버깅·재활용용)
└── 2026-03-15_travel_plan.md  # 사람이 읽는 최종 리포트
```

### (1) `{date}_raw.json`

```json
{
  "date": "2026-03-15",
  "recommendation": {
    "recommended_city": "제주",
    "weather": "3월 중순 평균 15도 내외, 바람이 있으나 온화함",
    "events": ["유채꽃 축제", "봄 시즌 지역 행사"],
    "reason": "추천 근거 2~4문장"
  },
  "restaurants": [
    {
      "name": "가게 이름",
      "address": "제주특별자치도 제주시 ...",
      "category": "음식점 > 한식 > 국수",
      "url": "http://place.map.kakao.com/12345678",
      "x": 126.52,
      "y": 33.51,
      "lng": 126.52,
      "lat": 33.51
    }
  ],
  "errors": []
}
```

- `ensure_ascii=False, indent=2` 로 저장하므로 한글이 그대로 보입니다.
- `restaurants` 는 **0건일 수 있습니다**(빈 배열). 오류가 아니라 정상 동작입니다.
- `errors` 에는 실행 중 발생한 모든 문제가 `{step, type, message}` 형태로 남습니다.

### (2) `{date}_travel_plan.md`

다음 섹션이 **항상** 포함됩니다(LLM 이 빠뜨리면 코드가 채워 넣습니다).

```
# {date} 국내 여행 추천 리포트
## 추천 지역
## 추천 이유
## 날씨 요약
## 행사/축제
## 맛집 추천        ← 0건이면 "데이터 없음 (장소 검색 결과 0건)"
## 1일 일정 제안     ← 오전 / 오후 / 저녁
## 오류 요약(errors) ← 오류가 없으면 "없음"
```

VS Code 에서 `Ctrl+Shift+V`(macOS `Cmd+Shift+V`)로 미리보기하면 보기 좋습니다.

> 💡 **오류 요약 섹션은 LLM 이 아니라 프로그램이 직접 만듭니다.**
> 3단계에서 발생한 오류는 LLM 이 알 수 없고, 오류 기록은 창작이 개입하면 안 되는 "사실"이기 때문입니다.

---

## 6. 보안 주의사항 ⚠️

1. **`.env` 를 반드시 `.gitignore` 에 추가**하세요. 이 저장소에는 이미 등록되어 있습니다.
   ```bash
   git check-ignore -v .env   # 무시되고 있는지 확인
   ```
2. **키를 코드/README/결과 파일에 하드코딩하지 마세요.** 이 프로젝트는 `os.getenv()` 로만 키를 읽습니다.
3. **키를 실수로 커밋했다면 즉시 재발급(revoke)** 하세요.
   - 커밋을 되돌려도 **git 히스토리와 GitHub 캐시에는 값이 남습니다.**
   - 순서: ① 발급 사이트에서 기존 키 삭제 → ② 새 키 발급 → ③ `.env` 갱신 → ④ 히스토리 정리
4. **로그에 키를 찍지 마세요.** 이 프로젝트는 `mask_api_key()` 로 앞 4자리만 남기고,
   `register_secret()` / `scrub_secrets()` 로 오류 메시지에 키가 섞여 나가는 것까지 자동 차단합니다.
5. **키는 사람마다 따로 발급**받으세요. 슬랙·메신저로 키를 공유하는 것은 사고의 시작입니다.
6. `results/` 도 `.gitignore` 에 포함되어 있습니다. 실행 결과에 개인 정보가 섞일 수 있기 때문입니다.

---

## 7. 자주 발생하는 오류와 대처법

| 증상 | `errors` 의 type | 원인 | 대처 |
| --- | --- | --- | --- |
| `필수 API 키가 설정되지 않았습니다` | (즉시 종료) | `.env` 없음 / 변수명 오타 | `cp .env.example .env` 후 값 입력. 변수명은 `GEMINI_API_KEY`, `KAKAO_REST_API_KEY` |
| `HTTP 400: ... API_KEY_INVALID` | `AUTH_ERROR` | **Gemini 는 키가 틀려도 401 이 아니라 400 을 보냅니다** | 키 재확인. 이 프로그램은 본문의 `API_KEY_INVALID` 문구를 보고 인증 오류로 분류합니다 |
| `HTTP 401` / `HTTP 403` | `AUTH_ERROR` | 키가 틀렸거나 만료, Kakao 는 JavaScript 키를 쓴 경우 | 키 재확인. Kakao 는 **REST API 키**여야 함. 헤더 형식 `KakaoAK {키}` |
| `HTTP 404: This model ... is no longer available` | `API_ERROR` | **모델이 단종됨** (실제로 겪은 오류입니다) | `.env` 의 `GEMINI_MODEL` 을 `gemini-flash-latest` 로 두면 이 문제가 생기지 않습니다. 아래 "사용 가능한 모델 확인" 참고 |
| `HTTP 429` | `QUOTA_ERROR` | 호출 한도 초과 | 잠시 후 재시도. Gemini 무료 티어는 **분당/일일 호출 제한**이 있습니다. **자동 재시도하지 않습니다** — 한도 초과 상태에서 다시 부르면 상황만 나빠지기 때문입니다 |
| `HTTP 503: experiencing high demand` | `API_ERROR` | 모델 서버 일시 과부하 (무료 티어에서 흔함) | **프로그램이 2초 → 4초 간격으로 최대 2번 자동 재시도**합니다. 그래도 안 되면 잠시 후 다시 실행하세요 |
| `HTTP 500` / `502` / `504` | `API_ERROR` | 제공자 서버 장애 | 503 과 같은 자동 재시도 대상입니다 |
| `타임아웃` / `네트워크 오류` | `NETWORK_ERROR` | 인터넷 끊김, 사내 프록시/방화벽 | 네트워크 확인. 프록시 환경이면 `HTTPS_PROXY` 설정 확인 |
| `응답 본문이 비어 있습니다` | `API_ERROR` | 안전 필터 차단(SAFETY) 또는 길이 초과(MAX_TOKENS) | 200 OK 인데도 본문이 빌 수 있는 것이 Gemini 의 특징입니다. `MAX_TOKENS` 면 `llm_client.py` 의 `REPORT_MAX_TOKENS` 를 늘리세요 |
| `JSON 파싱 실패` | `PARSE_ERROR` | LLM 이 설명 문장·코드블록을 덧붙임 | 프로그램이 코드블록 제거 후 **1회만** 재시도하고, 실패 시 기본값으로 진행합니다. 반복되면 `GEMINI_MODEL` 을 더 성능 좋은 모델로 바꿔 보세요 |
| `검색 결과가 0건입니다` | `EMPTY_RESULT` | 추천 도시명이 지나치게 넓거나 특이함 | 오류가 아닙니다. 리포트에는 "데이터 없음"으로 표기되고 나머지는 정상 생성됩니다 |
| `ModuleNotFoundError: requests` | - | 의존성 미설치 / 가상환경 미활성화 | `pip install -r requirements.txt` |

> 실행이 끝나면 `results/{date}_raw.json` 의 `errors` 배열과 리포트 하단
> "오류 요약(errors)" 표를 먼저 확인하세요. 어떤 단계에서 무엇이 실패했는지 그대로 남아 있습니다.

---

### 사용 가능한 모델 확인 (404 가 날 때)

계정에서 쓸 수 있는 모델 목록은 아래 명령으로 확인할 수 있습니다.

```bash
# macOS / Linux
curl -s -H "x-goog-api-key: $GEMINI_API_KEY" \
  https://generativelanguage.googleapis.com/v1beta/models | grep '"name"'
```

여기서 나온 이름(`models/` 접두사는 빼고)을 `.env` 의 `GEMINI_MODEL` 에 넣으면 됩니다.

---

## 8. 다른 API 제공자로 바꾸고 싶다면

- **LLM 을 OpenAI 로**: `llm_client.call_generate_content()` 의 URL·헤더·요청 본문·응답 파싱만 수정하면 됩니다.
  나머지 코드는 "문자열을 돌려주는 함수"로만 이 모듈을 사용하므로 영향받지 않습니다.
  (실제로 이 프로젝트는 OpenAI 로 먼저 만든 뒤 이 파일 하나만 바꿔 Gemini 로 옮겼습니다.)
- **장소 검색을 Naver Local 로**: `place_client.normalize_kakao_document()` 대신
  `normalize_naver_item()` 을 만들어 **같은 공통 스키마**를 반환하게 하면 됩니다.
  `report.py` 와 `travel_planner.py` 는 한 줄도 고칠 필요가 없습니다.
  → 이것이 "원본 응답을 그대로 쓰지 않고 정규화 함수를 따로 두는" 이유입니다.
