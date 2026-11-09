"""Validate WBS cards without modifying their source."""
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters import wbs_cards


def problems(path):
    path = Path(path)
    try:
        result = wbs_cards({"source": str(path)})
        return [f"{path}: {warning}" for warning in result["warnings"] + result["style_warnings"]]
    except (OSError, ValueError, RecursionError) as error:
        if isinstance(error, OSError):
            message = "ファイルを読み込めません"
        elif isinstance(error, UnicodeError):
            message = "UTF-8で読み込めません"
        elif isinstance(error, RecursionError):
            message = "JSONの入れ子が深すぎます"
        else:
            message = str(error)
        return [f"{path}: {message}"]


def main(args=None):
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    args = sys.argv[1:] if args is None else args
    if not args:
        print("使い方: python tools/check_wbs.py <wbs.json のパス>...", file=sys.stderr)
        return 1
    messages = [message for path in args for message in problems(path)]
    for message in messages:
        print(message, file=sys.stderr)
    return int(bool(messages))


if __name__ == "__main__":
    sys.exit(main())
