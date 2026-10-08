"""식약처 공식 데이터 조회 인터페이스 + 구현체 (Mock / Real)."""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SAMPLE_PATH = Path(__file__).with_name("sample_data.json")


class MfdsLookupError(Exception):
    """조회 실패(타임아웃, 응답 오류 등). 호출자는 warnings에 기록하고 계속 진행한다."""


@dataclass
class OfficialIngredient:
    name: str
    strength_value: Optional[float] = None
    strength_unit: Optional[str] = None


@dataclass
class OfficialProduct:
    source: str  # MFDS_DRUG | MFDS_HFOOD
    official_id: str  # 품목기준코드 등. 항상 String
    name: str
    ingredients: list[OfficialIngredient] = field(default_factory=list)
    strength_value: Optional[float] = None
    strength_unit: Optional[str] = None


class MfdsClient(ABC):
    @abstractmethod
    def find_by_code(self, code: str) -> Optional[OfficialProduct]:
        """코드 정확 일치 1건. 없으면 None. 실패 시 MfdsLookupError."""

    @abstractmethod
    def search_by_name(self, name: str) -> list[OfficialProduct]:
        """이름 매칭 후보 목록(의약품+건강기능식품). 정확/유사도 판정은 호출자가 한다."""


class MockMfdsClient(MfdsClient):
    """로컬 더미 샘플(sample_data.json)로 동작. 1주차용."""

    def __init__(self, products: Optional[list[OfficialProduct]] = None, path: Path = SAMPLE_PATH):
        self._products = products if products is not None else self._load(path)

    @staticmethod
    def _load(path: Path) -> list[OfficialProduct]:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return [
            OfficialProduct(
                source=p["source"], official_id=str(p["official_id"]), name=p["name"],
                ingredients=[OfficialIngredient(**i) for i in p.get("ingredients", [])],
                strength_value=p.get("strength_value"), strength_unit=p.get("strength_unit"),
            )
            for p in data["products"]
        ]

    def find_by_code(self, code: str) -> Optional[OfficialProduct]:
        return next((p for p in self._products if p.official_id == code), None)

    def search_by_name(self, name: str) -> list[OfficialProduct]:
        return list(self._products)


class RealMfdsClient(MfdsClient):
    """공공데이터포털 식약처 API 구현체. 2주차에 구현. 키는 환경변수 MFDS_API_KEY."""

    def __init__(self, api_key: Optional[str] = None, timeout: float = 5.0, retries: int = 3):
        self.api_key = api_key or os.environ.get("MFDS_API_KEY")
        self.timeout, self.retries = timeout, retries

    def find_by_code(self, code: str) -> Optional[OfficialProduct]:
        raise NotImplementedError("RealMfdsClient는 2주차에 구현 (API 응답 형식 확인 필요)")

    def search_by_name(self, name: str) -> list[OfficialProduct]:
        raise NotImplementedError("RealMfdsClient는 2주차에 구현 (API 응답 형식 확인 필요)")
