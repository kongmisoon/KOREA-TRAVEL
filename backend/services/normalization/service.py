"""파서 결과 + 식약처 조회 결과 -> NormalizationResult 리스트."""
from __future__ import annotations

import copy
import difflib
import logging
from pathlib import Path
from typing import Optional

import yaml

from backend.services.hira.parser import ParsedRow, parse_hira_excel
from backend.services.mfds.client import MfdsClient, MfdsLookupError, MockMfdsClient, OfficialProduct
from backend.services.normalization.models import Ingredient, Match, MedicationRecord, NormalizationResult, Strength
from backend.services.normalization.name_utils import match_key

log = logging.getLogger(__name__)
DEFAULT_CONFIG = Path(__file__).with_name("matching_config.yaml")


def load_config(path: Optional[Path] = None) -> dict:
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _by_strength(rec: MedicationRecord, cands: list[OfficialProduct]) -> list[OfficialProduct]:
    """후보가 여러 개일 때 함량(값+단위)이 둘 다 알려져 있고 일치하는 것만 남긴다."""
    if rec.strength.value is None or rec.strength.unit is None:
        return cands
    same = [c for c in cands if c.strength_value == rec.strength.value and c.strength_unit == rec.strength.unit]
    return same or cands


def _describe(cands: list[OfficialProduct]) -> str:
    return ", ".join(f"{c.official_id}:{c.name}" for c in cands)


def _enrich(rec: MedicationRecord, p: OfficialProduct) -> MedicationRecord:
    out = copy.deepcopy(rec)  # 원본(raw) 값은 건드리지 않고 비어 있는 정규화 필드만 채운다
    if not out.ingredients:
        out.ingredients = [
            Ingredient(i.name, i.strength_value, i.strength_unit, source="MFDS") for i in p.ingredients
        ]
    if out.strength.value is None and p.strength_value is not None:
        out.strength = Strength(raw=out.strength.raw, value=p.strength_value, unit=p.strength_unit, source="MFDS")
    return out


def normalize_record(rec: MedicationRecord, client: MfdsClient, threshold: float) -> NormalizationResult:
    warnings: list[str] = []
    ref = {"sheet": rec.sheet, "row_number": rec.row_number}

    def done(status, method, product=None, record=rec):
        m = Match(product.source if product else None, product.official_id if product else None, method)
        return NormalizationResult(status, record, m, warnings, [], ref)

    def resolved(product: OfficialProduct, method: str):
        full = _enrich(rec, product)
        if not product.ingredients:
            warnings.append("OFFICIAL_INGREDIENTS_MISSING: 공식 데이터에 성분 정보 없음")
            return done("PARTIAL", method, product, full)
        return done("MATCHED", method, product, full)

    try:
        # 1) 코드 정확 일치
        if rec.drug_code:
            p = client.find_by_code(rec.drug_code)
            if p:
                return resolved(p, "CODE_EXACT")

        # 2) 정규화 제품명 정확 일치
        cands = client.search_by_name(rec.product_name.raw or "")
        key = match_key(rec.product_name.raw or "")
        exact = [c for c in cands if match_key(c.name) == key]
        if exact:
            pick = _by_strength(rec, exact)
            if len(pick) == 1:
                return resolved(pick[0], "NAME_EXACT")
            warnings.append(f"MULTIPLE_CANDIDATES: {_describe(pick)}")
            return done("PARTIAL", "NAME_EXACT")

        # 3) 유사도 매칭
        scored = sorted(
            ((difflib.SequenceMatcher(None, key, match_key(c.name)).ratio(), c) for c in cands),
            key=lambda t: (-t[0], t[1].official_id),
        )
        near = [(s, c) for s, c in scored if s >= threshold]
        if near:
            pick = _by_strength(rec, [c for _, c in near])
            if len(pick) == 1:
                score = next(s for s, c in near if c is pick[0])
                warnings.append(f"FUZZY_MATCH: score={score:.3f}")
                return resolved(pick[0], "NAME_FUZZY")
            warnings.append(f"MULTIPLE_CANDIDATES: {_describe(pick)}")
            return done("PARTIAL", "NAME_FUZZY")
    except MfdsLookupError as e:
        warnings.append(f"MFDS_LOOKUP_ERROR: {e}")
        return done("UNMATCHED", "NONE")

    # 4) 실패 — 원본 값은 그대로 보존
    warnings.append("NO_OFFICIAL_MATCH: 공식 데이터에서 찾지 못함")
    return done("UNMATCHED", "NONE")


def normalize_rows(rows: list[ParsedRow], client: MfdsClient, config: Optional[dict] = None) -> list[NormalizationResult]:
    threshold = (config or load_config())["fuzzy_threshold"]
    results = []
    for row in rows:
        ref = {"sheet": row.sheet, "row_number": row.row_number}
        if row.record is None:
            results.append(NormalizationResult("FAILED", None, Match(), list(row.warnings), list(row.errors), ref))
            continue
        try:
            r = normalize_record(row.record, client, threshold)
        except Exception as e:  # 한 행이 실패해도 계속
            log.exception("normalize failed %s", ref)
            results.append(NormalizationResult(
                "FAILED", row.record, Match(), list(row.warnings), [f"NORMALIZE_EXCEPTION: {type(e).__name__}: {e}"], ref))
            continue
        r.warnings = list(row.warnings) + r.warnings
        results.append(r)
    return results


def normalize_file(path, client: Optional[MfdsClient] = None, config: Optional[dict] = None):
    """HIRA Excel -> (NormalizationResult 리스트, 파일 수준 warnings, 파일 수준 errors)."""
    parsed = parse_hira_excel(path)
    results = normalize_rows(parsed.rows, client or MockMfdsClient(), config)
    return results, parsed.warnings, parsed.errors
