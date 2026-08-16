# KOREA-TRAVEL

API를 활용한 **국내 여행지 추천 CLI 프로그램** 과제 저장소입니다.

여행 날짜를 입력하면 3단계 파이프라인을 거쳐 Markdown 여행 리포트를 만들어 줍니다.

```
날짜 입력 → [1] LLM 추천(JSON) → [2] 장소 API 맛집 검색 → [3] LLM 리포트(Markdown)
```

## 📂 프로젝트 위치

실제 코드는 **[`travel-recommender/`](./travel-recommender)** 폴더에 있습니다.

| 문서 | 설명 |
| --- | --- |
| [travel-recommender/README.md](./travel-recommender/README.md) | 설치·API 키 발급·실행 방법·오류 대처법 |
| [travel-recommender/docs/학습정리.md](./travel-recommender/docs/학습정리.md) | 학습 정리 4가지 (REST/GET·POST, JSON 구조화, 오류 대응, 키 관리) |

## 🚀 빠른 실행

```bash
cd travel-recommender
pip install -r requirements.txt

# API 키 없이 동작 확인하기
python travel_planner.py --date "2026-03-15" --demo

# 실제 API 로 실행하기 (.env 에 키 입력 후)
cp .env.example .env
python travel_planner.py --date "2026-03-15"
```

## 🛠 사용 기술

- Python 3.10+ / 터미널 전용 (웹 프레임워크 미사용)
- LLM: **Google Gemini** (`generateContent` REST API)
- 장소 검색: **Kakao Local** (키워드 검색 API)
- 라이브러리: `argparse`, `requests`, `python-dotenv`, `json`, `os`, `datetime`

## 🔐 보안

- API 키는 코드에 하드코딩하지 않고 `.env` → `os.getenv()` 로만 읽습니다.
- `.env` 와 `results/` 는 `.gitignore` 에 등록되어 커밋되지 않습니다.
- 로그·오류 메시지에 키가 노출되지 않도록 앞 4자리만 남기고 마스킹합니다.
