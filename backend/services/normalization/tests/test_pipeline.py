import json
from pathlib import Path

import openpyxl
import pytest

from make_sample import HEADER, build_sample_xlsx
from backend.services.hira.parser import parse_date, parse_hira_excel, parse_number
from backend.services.mfds.client import MfdsClient, MfdsLookupError, MockMfdsClient
from backend.services.normalization.name_utils import match_key, parse_strength
from backend.services.normalization.service import normalize_file, normalize_rows

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    p = tmp_path_factory.mktemp("x") / "sample.xlsx"
    build_sample_xlsx(p)
    return p


@pytest.fixture(scope="module")
def by_row(sample):
    results, _, _ = normalize_file(sample)
    return {r.source_ref["row_number"]: r for r in results}


def test_expected_json(sample):
    results, fw, fe = normalize_file(sample)
    actual = {"file_warnings": fw, "file_errors": fe, "results": [r.to_dict() for r in results]}
    expected = json.loads((FIX / "expected_sample.json").read_text(encoding="utf-8"))
    assert actual == expected


def test_deterministic(sample):
    a = [r.to_dict() for r in normalize_file(sample)[0]]
    b = [r.to_dict() for r in normalize_file(sample)[0]]
    assert a == b


def test_normal_row(by_row):
    r = by_row[4]
    assert r.status == "MATCHED" and r.match.match_method == "CODE_EXACT"
    assert r.record.date == "2024-03-05" and r.record.dosage.total_days == 3
    assert r.record.product_name.raw == "하나펜정500mg"
    assert r.record.strength.value == 500 and r.record.strength.unit == "mg"


def test_leading_zero_code_kept_as_string(by_row):
    r = by_row[6]
    assert r.record.drug_code == "012345678" and r.match.match_method == "CODE_EXACT"


def test_numeric_code_cell_warns_and_is_not_padded(by_row):
    r = by_row[7]
    assert r.record.drug_code == "12345678"
    assert any(w.startswith("CODE_NUMERIC_CELL") for w in r.warnings)
    assert r.match.match_method != "CODE_EXACT"  # 0을 추측해서 붙이지 않는다


def test_invalid_date_is_null_with_warning(by_row):
    r = by_row[7]
    assert r.record.date is None and r.record.date_raw == "2024-13-45"
    assert any(w.startswith("DATE_INVALID") for w in r.warnings)


def test_empty_values_are_null(by_row):
    r = by_row[9]
    assert r.record.date is None and r.record.drug_code is None
    assert r.record.ingredients == [] and r.record.institution == "가나다의원"


def test_zero_is_not_null(by_row):
    d = by_row[15].record.dosage
    assert d.dose_per_time == 0.0 and d.dose_per_time is not None
    assert d.total_days == 30


def test_unparseable_number_is_null_with_warning(by_row):
    r = by_row[16]
    assert r.record.dosage.dose_per_time is None and r.record.dosage.raw["dose_per_time"] == "abc"
    assert any("NUMBER_UNPARSEABLE" in w for w in r.warnings)


def test_duplicate_candidates_partial(by_row):
    r = by_row[9]
    assert r.status == "PARTIAL" and r.match.official_id is None
    assert any(w.startswith("MULTIPLE_CANDIDATES") for w in r.warnings)


def test_strength_disambiguates_duplicates(by_row):
    r = by_row[5]
    assert r.status == "MATCHED" and r.match.official_id == "200800002"
    assert [i.name for i in r.record.ingredients] == ["아세트아미노펜"]
    assert r.record.ingredients[0].source == "MFDS"


def test_fuzzy_single_and_multiple(by_row):
    assert by_row[10].status == "MATCHED" and by_row[10].match.match_method == "NAME_FUZZY"
    assert by_row[11].status == "PARTIAL" and by_row[11].match.match_method == "NAME_FUZZY"


def test_unmatched_keeps_raw(by_row):
    r = by_row[12]
    assert r.status == "UNMATCHED" and r.match.match_method == "NONE"
    assert r.record.product_name.raw == "존재안함정"


def test_failed_row_does_not_stop_others(by_row):
    r = by_row[14]
    assert r.status == "FAILED" and r.record is None and r.errors
    assert by_row[15].status == "MATCHED"  # 실패 행 뒤의 행도 처리됨


def test_official_without_ingredients_is_partial(by_row):
    assert by_row[16].status == "PARTIAL"


def test_blank_and_repeated_header_rows_skipped(sample):
    rows = parse_hira_excel(sample).rows
    assert {r.row_number for r in rows}.isdisjoint({8, 13})


def test_merged_cell_filled(sample):
    rows = {r.row_number: r for r in parse_hira_excel(sample).rows}
    assert rows[5].record.institution == "가나다의원"


def test_header_detected_at_any_row_with_aliases(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    for _ in range(5):
        ws.append(["메모"])
    ws.append(["조제일자", "처방약품명", "보험코드"])
    ws.append(["2024-01-02", "하나펜정500mg", "200800001"])
    p = tmp_path / "a.xlsx"
    wb.save(p)
    out = parse_hira_excel(p)
    assert len(out.rows) == 1 and out.rows[0].row_number == 7
    assert out.rows[0].record.date == "2024-01-02"


def test_no_header_is_file_error(tmp_path):
    wb = openpyxl.Workbook()
    wb.active.append(["아무", "내용"])
    p = tmp_path / "b.xlsx"
    wb.save(p)
    out = parse_hira_excel(p)
    assert out.errors and out.errors[0].startswith("HEADER_NOT_FOUND")


def test_unreadable_file(tmp_path):
    p = tmp_path / "c.xlsx"
    p.write_text("not excel")
    assert parse_hira_excel(p).errors[0].startswith("FILE_UNREADABLE")


def test_lookup_error_recorded_not_raised(sample):
    class Boom(MfdsClient):
        def find_by_code(self, code):
            raise MfdsLookupError("timeout")

        def search_by_name(self, name):
            raise MfdsLookupError("timeout")

    results = normalize_rows(parse_hira_excel(sample).rows, Boom())
    ok = [r for r in results if r.status != "FAILED"]
    assert ok and all(r.status == "UNMATCHED" for r in ok)
    assert all(any("MFDS_LOOKUP_ERROR" in w for w in r.warnings) for r in ok)


@pytest.mark.parametrize("v,exp", [
    ("2024-03-05", "2024-03-05"), ("2024.3.5", "2024-03-05"), ("20240305", "2024-03-05"),
    ("2024년 3월 5일", "2024-03-05"), (20240305, "2024-03-05"), ("2024-02-30", None), ("", None), (None, None),
])
def test_parse_date(v, exp):
    assert parse_date(v)[0] == exp


@pytest.mark.parametrize("v,exp", [("1", 1.0), (0, 0.0), ("0.5", 0.5), ("1/2", 0.5), ("3정", 3.0), ("1,000", 1000.0), ("", None), ("x", None)])
def test_parse_number(v, exp):
    assert parse_number(v)[0] == exp


def test_name_utils():
    assert match_key("타이레놀 정 500mg") == match_key("타이레놀정")
    assert match_key("하나펜정500mg(수출용)") == "하나펜"
    assert parse_strength("하나펜정 0.5 g") == (0.5, "g")
    assert parse_strength("약") == (None, None)
