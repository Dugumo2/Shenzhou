"""只读核验候选包、真实浏览器证据和独立视觉审阅，不部署。"""
import argparse
import json
from pathlib import Path

from release_acceptance import verify_acceptance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-archive', required=True, type=Path)
    parser.add_argument('--acceptance', required=True, type=Path)
    parser.add_argument('--visual-review', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = verify_acceptance(args.candidate_archive, args.acceptance, args.visual_review)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, '验收门禁拒绝：' + str(exc) + '\n')
    print(json.dumps({key: value for key, value in result.items() if key != 'files'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
