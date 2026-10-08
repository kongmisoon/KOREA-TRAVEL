# B파트: HIRA 진료내역 → 식약처 정규화 (1주차 PoC)

HIRA 진료내역 Excel을 규칙 기반(LLM 미사용)으로 읽어 `MedicationRecord`로 만들고, 식약처 공식 데이터(현재 Mock)와 매칭해 `NormalizationResult`를 반환한다.

## 폴더
- `backend/services/hira/` — `parser.py`, `column_mapping.yaml`(컬럼 별칭 설정)
- `backend/services/mfds/` — `client.py`(`MfdsClient` / `MockMfdsClient` / `RealMfdsClient` 스텁), `sample_data.json`(더미)
- `backend/services/normalization/` — `models.py`(모델 정의 한 곳), `name_utils.py`, `service.py`, `matching_config.yaml`, `cli.py`, `tests/`

## 실행
```bash
pip install openpyxl pyyaml pytest
python -m backend.services.normalization.cli path/to/hira.xlsx > out.json   # 저장소 루트에서
python -m pytest backend/services/normalization/tests -q
# 기대 JSON 재생성(의도한 변경일 때만): python backend/services/normalization/tests/fixtures/make_sample.py
```
API 키는 환경변수 `MFDS_API_KEY`(`.env`, 커밋 금지)로만 읽는다. 2주차 `RealMfdsClient`에서 사용.

## 입출력 예시 (일부 필드 생략)
입력 행: `2024-03-05 | 200800001 | 하나펜정500mg | 아세트아미노펜 | 1 | 3 | 3 | 가나다의원`
```json
{
  "status": "MATCHED",
  "record": {
    "source": "HIRA", "sheet": "진료내역", "row_number": 4,
    "date": "2024-03-05", "date_raw": "2024-03-05",
    "drug_code": "200800001",
    "product_name": {"raw": "하나펜정500mg", "normalized": "하나펜정500mg"},
    "strength": {"raw": "500mg", "value": 500.0, "unit": "mg", "source": "PRODUCT_NAME"},
    "ingredients": [{"name": "아세트아미노펜", "strength_value": null, "strength_unit": null, "source": "HIRA"}],
    "dosage": {"dose_per_time": 1.0, "times_per_day": 3.0, "total_days": 3,
               "raw": {"dose_per_time": "1", "times_per_day": "3", "total_days": "3"}},
    "institution": "가나다의원"
  },
  "match": {"source": "MFDS_DRUG", "official_id": "200800001", "match_method": "CODE_EXACT"},
  "warnings": [], "errors": [],
  "source_ref": {"sheet": "진료내역", "row_number": 4}
}
```
전체 예시는 `tests/fixtures/expected_sample.json`.

## 동작 규칙 요약
- 값 구분: `null`(모름) ≠ `0` ≠ `false` ≠ `""`. 빈 셀은 null, 숫자 0은 0.0으로 보존.
- 날짜는 `YYYY-MM-DD`. 해석 불가/존재하지 않는 날짜는 null + `DATE_INVALID`/`DATE_UNPARSEABLE` 경고 (원본은 `date_raw`).
- `drug_code`는 항상 String. 숫자 셀로 들어온 코드는 앞자리 0을 **복원하지 않고** `CODE_NUMERIC_CELL` 경고만 남김.
- 행 단위 실패(약품명 없음 등)는 그 행만 `FAILED`, 나머지는 계속 처리. 빈 행·반복 헤더 행은 건너뜀.
- 매칭 순서: `CODE_EXACT` → `NAME_EXACT`(괄호·함량·제형·공백 제거 키) → `NAME_FUZZY`(difflib, 임계값 `matching_config.yaml`, 기본 0.9) → `UNMATCHED`(`match_method: "NONE"`).
- 이름이 같은 후보가 여럿이면 함량(값+단위)이 일치하는 것만 남기고, 그래도 2개 이상이면 `PARTIAL`(official_id=null, 후보는 `MULTIPLE_CANDIDATES` 경고).
- 유일하게 매칭돼도 공식 데이터에 성분이 없으면 `PARTIAL`. 비어 있던 `ingredients`/`strength`만 공식 값으로 채우며 `source: "MFDS"`로 표시, 원본(raw)은 건드리지 않음.
- 식약처 조회 실패(`MfdsLookupError`)는 경고(`MFDS_LOOKUP_ERROR`)로 기록하고 해당 행은 UNMATCHED.

## 가정 사항 (팀장 확인 필요)
1. **저장소/브랜치**: 요청서는 저장소 `yakeum`, 브랜치 `feature/data-backend`였으나 이 세션의 저장소는 `KOREA-TRAVEL`, 지정 브랜치는 `claude/lucid-cori-n18hby`였음. `docs/DATA_SCHEMA.md`도 없었음.
2. **스키마**: `docs/DATA_SCHEMA.md`가 없어 `models.py`에 임시 모델 정의(파일 상단에 "임시" 주석). 필드명·중첩 구조(`product_name.raw/normalized`, `strength`, `dosage` 등)는 전부 가정. 추가 필드: `sheet`, `row_number`, `source_ref`, `raw_row`, `strength.source`, `ingredients[].source`. `FAILED`일 때 `record`는 null.
3. **HIRA 컬럼과 별칭**(`hira/column_mapping.yaml`, 실제 파일과 다르면 이 파일만 수정):
   - date: 진료일자·진료일·조제일자·조제일·처방일자·처방일·투약일자·내원일자
   - product_name(필수): 약품명·약품명칭·처방약품명·처방의약품명·의약품명·약품·제품명·품명
   - drug_code: 약품코드·보험코드·약품기준코드·품목기준코드·제품코드 (`주성분코드`는 다른 개념으로 보고 매핑하지 않음; raw_row에만 남음)
   - ingredient: 주성분명·성분명·주성분·성분·일반명·일반명칭 (`,;/+`로 분리)
   - strength: 함량·규격·약품규격 (없으면 제품명 속 `500mg` 같은 표기에서 규칙으로 추출, `source: PRODUCT_NAME`)
   - dose_per_time: 1회투약량·1회투여량·1회복용량·1회용량 / times_per_day: 1일투여횟수·1일투약횟수·1일복용횟수 / total_days: 총투약일수·총투여일수·투약일수·처방일수·일수
   - institution: 요양기관명·요양기관·의료기관명·병원명·약국명·기관명
   - 헤더 행은 위 30행 안에서 별칭이 가장 많이 매칭되는 행(최소 2개, 약품명 필수). 병합 셀은 병합 영역 전체를 좌상단 값으로 채움.
4. `drug_code`는 식약처 `official_id`(품목기준코드 등)와 직접 비교. 보험코드↔품목기준코드 변환표는 쓰지 않음(2주차 API 형식 확인 후 결정).
5. 식약처 데이터는 `mfds/sample_data.json`의 **더미**(가짜 제품·코드)이며, `RealMfdsClient`는 미구현(`NotImplementedError`). 재시도·캐싱·로깅 보강은 2~3주차.
6. 유사도는 `difflib.SequenceMatcher` 비율. 단일 후보는 MATCHED, 2개 이상은 PARTIAL.
