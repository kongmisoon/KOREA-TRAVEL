"""제품명/함량 정규화 유틸 (규칙 기반, 결정적)."""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

_UNIT_ALIASES = {
    "mg": "mg", "밀리그램": "mg", "g": "g", "그램": "g",
    "mcg": "mcg", "μg": "mcg", "µg": "mcg", "마이크로그램": "mcg",
    "ml": "ml", "밀리리터": "ml", "iu": "iu", "%": "%",
}
STRENGTH_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mg|g|mcg|μg|µg|ml|iu|%|밀리그램|그램|마이크로그램|밀리리터)", re.IGNORECASE
)
_FORMS = sorted(
    ["서방정", "장용정", "필름코팅정", "연질캡슐", "경질캡슐", "캡슐", "정", "시럽", "현탁액",
     "액", "산", "과립", "주사", "주", "크림", "연고", "겔", "패치"],
    key=len, reverse=True,
)


def clean_text(value: str) -> str:
    """NFKC + 연속 공백 정리."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def normalize_unit(unit: str) -> Optional[str]:
    return _UNIT_ALIASES.get(unit.lower())


def parse_strength(text: str) -> tuple[Optional[float], Optional[str]]:
    """'500mg' -> (500.0, 'mg'). 첫 번째 함량 표기만 사용. 없으면 (None, None)."""
    m = STRENGTH_RE.search(clean_text(text))
    if not m:
        return None, None
    return float(m.group(1)), normalize_unit(m.group(2))


def match_key(name: str) -> str:
    """매칭용 키: 괄호·함량·제형·공백/기호 제거."""
    s = clean_text(name).lower()
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", s)
    s = STRENGTH_RE.sub("", s)
    s = re.sub(r"[\s\-_/.,·]+", "", s)
    changed = True
    while changed:  # 끝에 붙은 제형 반복 제거
        changed = False
        for f in _FORMS:
            if s.endswith(f) and len(s) > len(f):
                s = s[: -len(f)]
                changed = True
                break
    return s
