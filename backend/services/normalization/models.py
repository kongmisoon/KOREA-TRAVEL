# 임시: DATA_SCHEMA.md 확정 시 맞출 것
# 이 프로젝트의 모든 공통 모델 정의는 이 파일 한 곳에만 둔다. (팀장이 합칠 때 이 파일만 맞추면 됨)
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

# null / 0 / false / "" 는 서로 다른 의미다. 값을 모르면 None(null), 0은 실제 0이다.


@dataclass
class RawNormalized:
    raw: Optional[str] = None  # 원본 (공백만 제거)
    normalized: Optional[str] = None  # 정규화 (NFKC + 공백 정리)


@dataclass
class Strength:
    """함량. raw/normalized 분리."""

    raw: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None  # mg, g, mcg, ml, iu, %
    source: Optional[str] = None  # COLUMN | PRODUCT_NAME | MFDS


@dataclass
class Ingredient:
    name: str
    strength_value: Optional[float] = None
    strength_unit: Optional[str] = None
    source: str = "HIRA"  # HIRA | MFDS


@dataclass
class Dosage:
    """복용법. 숫자 필드는 정규화 값, raw는 원본 문자열."""

    dose_per_time: Optional[float] = None
    times_per_day: Optional[float] = None
    total_days: Optional[int] = None
    raw: dict = field(default_factory=dict)  # {"dose_per_time": "...", ...}


@dataclass
class MedicationRecord:
    source: str = "HIRA"
    sheet: Optional[str] = None
    row_number: Optional[int] = None  # Excel 실제 행 번호(1-base)
    date: Optional[str] = None  # YYYY-MM-DD
    date_raw: Optional[str] = None
    drug_code: Optional[str] = None  # 항상 String, 앞자리 0 보존
    drug_code_raw: Optional[str] = None
    product_name: RawNormalized = field(default_factory=RawNormalized)
    strength: Strength = field(default_factory=Strength)
    ingredients: list[Ingredient] = field(default_factory=list)
    dosage: Dosage = field(default_factory=Dosage)
    institution: Optional[str] = None
    raw_row: dict = field(default_factory=dict)  # 원본 행 전체 {헤더: 문자열}


@dataclass
class Match:
    source: Optional[str] = None  # MFDS_DRUG | MFDS_HFOOD | None
    official_id: Optional[str] = None
    match_method: str = "NONE"  # CODE_EXACT | NAME_EXACT | NAME_FUZZY | NONE


@dataclass
class NormalizationResult:
    status: str  # MATCHED | PARTIAL | UNMATCHED | FAILED
    record: Optional[MedicationRecord]  # FAILED(행 해석 불가)이면 None
    match: Match = field(default_factory=Match)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source_ref: Optional[dict] = None  # {"sheet":..., "row_number":...} (스키마 외 추가 필드)

    def to_dict(self) -> dict:
        return asdict(self)
