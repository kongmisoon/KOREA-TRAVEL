import sys
from pathlib import Path

# backend/services/normalization/tests -> 저장소 루트
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
