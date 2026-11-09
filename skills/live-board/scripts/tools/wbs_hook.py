"""Optional Claude Code PostToolUse hook. Internal failures never block a tool."""
import json
import os
from pathlib import Path
import re
import sys


def candidate_paths(data):
    tool = data.get("tool_name")
    value = data.get("tool_input", {})
    if tool in ("Write", "Edit", "MultiEdit"):
        candidates = [value.get("file_path")]
    elif tool in ("Bash", "PowerShell"):
        command = value.get("command", "")
        candidates = [match.group(1) or match.group(2) or match.group(3) for match in re.finditer(
            r'"([^"\r\n]*wbs\.json)"|\'([^\'\r\n]*wbs\.json)\'|([^\s\'";|&<>()[\]{}=]*wbs\.json)(?=$|[\s;|&<>()[\]{}])', command)]
    else:
        return []
    paths = set()
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        if os.name == "nt":
            candidate = re.sub(r"^/([A-Za-z])/", r"\1:/", candidate)
        path = Path(candidate)
        if not path.is_absolute():
            path = Path(data["cwd"]) / path
        if path.name == "wbs.json" and path.is_file():
            paths.add(path.resolve())
    return sorted(paths)


def main():
    try:
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8")
        raw = sys.stdin.read()
        if "wbs.json" not in raw:
            return 0
        data = json.loads(raw)
        paths = candidate_paths(data)
        if not paths:
            return 0
        from check_wbs import problems
        messages = [message for path in paths for message in problems(path)]
        for message in messages:
            print(message, file=sys.stderr)
        return 2 if messages else 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
