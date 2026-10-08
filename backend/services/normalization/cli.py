"""사용법: python -m backend.services.normalization.cli <hira.xlsx> > out.json"""
import json
import sys

from backend.services.normalization.service import normalize_file


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    results, warnings, errors = normalize_file(argv[0])
    print(json.dumps(
        {"file_warnings": warnings, "file_errors": errors, "results": [r.to_dict() for r in results]},
        ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
