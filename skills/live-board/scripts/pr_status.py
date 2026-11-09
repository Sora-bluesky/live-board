"""Read-only PR enrichment. Unknown data never means closed, absent, or CI success."""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess


PR_REFRESH_SECONDS = 120
GH_TIMEOUT_SECONDS = 30
TASK_LABELS = {"todo": "未着手", "doing": "進行中", "blocked": "待ち", "done": "済み"}


def validate_repo(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+", value
    ) or value.split("/")[-1] in (".", ".."):
        raise ValueError('github_repo は "owner/name" 形式の文字列にしてください。')


def pr_numbers(value, task_id):
    numbers = value if isinstance(value, list) else [value]
    if not numbers or any(type(number) is not int or number <= 0 or number > 2_147_483_647 for number in numbers):
        raise ValueError(f"課題 {task_id}: pr は正の整数または1件以上の正の整数の配列にしてください。")
    return list(dict.fromkeys(numbers))


def graphql_query(numbers):
    fields = """number url title state isDraft mergedAt
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }"""
    pulls = "\n".join(f"pr_{number}: pullRequest(number: {number}) {{ {fields} }}" for number in sorted(set(numbers)))
    return f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {pulls} }} }}"


class PRFetchError(ValueError):
    pass


def error_reason(message):
    message = message.lower()
    if any(word in message for word in ("rate limit", "rate_limit", "rate_limited")):
        return "GitHub の利用回数の上限に達しました"
    if any(word in message for word in ("auth", "login", "credential", "401", "token")):
        return "gh の認証を確認してください"
    return "gh の実行または GitHub の応答に問題があります"


def map_pull_request(value, repo, number):
    try:
        if not isinstance(value, dict):
            raise ValueError
        if type(value["number"]) is not int or value["number"] != number:
            raise ValueError
        if not isinstance(value["url"], str) or value["url"].lower() != f"https://github.com/{repo}/pull/{number}".lower():
            raise ValueError
        if not isinstance(value["title"], str) or value["state"] not in ("OPEN", "MERGED", "CLOSED") or type(value["isDraft"]) is not bool:
            raise ValueError
        value["title"].encode("utf-8")
        merged = value["mergedAt"]
        if value["state"] == "MERGED":
            if not isinstance(merged, str) or datetime.fromisoformat(merged).tzinfo is None:
                raise ValueError
        elif merged is not None:
            raise ValueError
        nodes = value["commits"]["nodes"]
        if not isinstance(nodes, list) or len(nodes) > 1:
            raise ValueError
        # An explicit null rollup (or no commits) means no CI; missing fields do not.
        rollup = nodes[0]["commit"]["statusCheckRollup"] if nodes else None
        ci = "none" if rollup is None else {
            "SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure",
            "PENDING": "pending", "EXPECTED": "pending",
        }[rollup["state"]]
    except (KeyError, TypeError, ValueError, UnicodeError, IndexError):
        raise PRFetchError(f"PR {number}番の応答が不完全または不正です") from None
    return {"number": number, "url": value["url"], "title": value["title"], "state": value["state"],
            "is_draft": value["isDraft"], "merged_at": merged, "ci": ci}


def find_gh():
    """Search only absolute PATH directories outside the working directory."""
    cwd = Path.cwd().resolve()
    names = ["gh"]
    if os.name == "nt":
        extensions = [extension.strip() for extension in os.environ.get("PATHEXT", "").split(os.pathsep)
                      if extension.strip()]
        names = ["gh" + extension for extension in (extensions or [".COM", ".EXE", ".BAT", ".CMD"])]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(entry)
        if not entry or not directory.is_absolute():
            continue
        directory = directory.resolve()
        if directory.is_relative_to(cwd):
            continue
        for name in names:
            candidate = (directory / name).resolve()
            if candidate.is_relative_to(cwd):
                continue
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate)
    return None


def fetch_pull_requests(repo, numbers, runner=None):
    """One GraphQL request for a repo. The runner is injectable at the Board boundary."""
    executable = find_gh()
    if not executable:
        raise PRFetchError("gh が見つかりません")
    owner, name = repo.split("/")
    # Stdin avoids the Windows command-line limit when many tasks link PRs.
    command = [executable, "api", "graphql", "--hostname", "github.com", "--input", "-"]
    payload = json.dumps({"query": graphql_query(numbers), "variables": {"owner": owner, "name": name}})
    try:
        # The board runs under pythonw, so a console child like gh would open its own terminal window.
        result = (runner or subprocess.run)(command, input=payload, capture_output=True, text=True, encoding="utf-8",
                                           timeout=GH_TIMEOUT_SECONDS, check=False,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise PRFetchError("gh の応答がタイムアウトしました") from None
    except (OSError, UnicodeError):
        raise PRFetchError("gh を実行できません") from None
    try:
        response = json.loads(result.stdout)
    except (ValueError, TypeError):
        if result.returncode:
            raise PRFetchError(error_reason(result.stderr)) from None
        raise PRFetchError("応答が正しいJSONではありません") from None
    if not isinstance(response, dict):
        raise PRFetchError("応答の形式が不正です")
    data = response.get("data")
    errors = response.get("errors", [])
    if not isinstance(errors, list):
        raise PRFetchError("応答の形式が不正です")
    if result.returncode and not errors:
        raise PRFetchError(error_reason(result.stderr))
    if not isinstance(data, dict) and (result.returncode or errors):
        raise PRFetchError(error_reason(result.stderr + json.dumps(errors)))
    repository = data.get("repository") if isinstance(data, dict) else None
    if not isinstance(repository, dict):
        raise PRFetchError("リポジトリを取得できません（名前・権限を確認してください）")
    # Only scoped NOT_FOUND errors leave sibling PR data trustworthy.
    aliases = {f"pr_{number}": number for number in numbers}
    failed = {}
    for error in errors:
        if not isinstance(error, dict) or error.get("type") != "NOT_FOUND":
            raise PRFetchError(error_reason(json.dumps(errors)))
        path = error.get("path")
        if path == ["repository"]:
            raise PRFetchError("リポジトリを取得できません（名前・権限を確認してください）")
        if not isinstance(path, list) or len(path) != 2 or path[0] != "repository" or not isinstance(path[1], str) or path[1] not in aliases:
            raise PRFetchError(error_reason(json.dumps(errors)))
        number = aliases[path[1]]
        failed[number] = f"PR {number}番が見つかりません"
    mapped = {}
    for number in numbers:
        try:
            if number in failed:
                raise PRFetchError(failed[number])
            mapped[number] = {"data": map_pull_request(repository.get(f"pr_{number}"), repo, number), "error": None}
        except PRFetchError as error:
            mapped[number] = {"data": None, "error": str(error)}
    return mapped


def cache_result(number, previous, result, at):
    error = result["error"]
    return {"number": number, "data": previous.get("data") if error else result["data"],
            "fetched_at": previous.get("fetched_at") if error else at, "checked_at": at,
            "error": {"message": f"PR の状態を取得できません（{error}）", "at": at} if error else None}


def task_flags(task, prs):
    known = [pr["data"] for pr in prs if pr["status"] == "ok"]
    flags = []
    label = TASK_LABELS.get(task["status"], "不明")
    for pr in known:
        if pr["state"] == "MERGED" and task["status"] != "done":
            flags.append({"kind": "mismatch", "message": f"PR {pr['number']}番はマージ済み・課題は{label}"})
        if pr["state"] == "OPEN" and task["status"] == "todo":
            flags.append({"kind": "mismatch", "message": f"PR {pr['number']}番は公開中・課題は未着手"})
        if pr["state"] == "OPEN" and pr["ci"] == "failure":
            flags.append({"kind": "ci", "message": f"PR {pr['number']}番の CI が失敗しています"})
    if prs and len(known) == len(prs) and all(pr["state"] == "CLOSED" for pr in known) and task["status"] in ("doing", "done"):
        flags.append({"kind": "mismatch", "message": f"すべての PR がマージされずに終了・課題は{label}"})
    return flags


def enrich_snapshot(good, repo, results, at):
    if good is None:
        return None
    tasks = []
    summary = {"linked_count": 0, "mismatch_count": 0, "ci_failure_count": 0, "unknown_count": 0, "errors": []}
    for task in good["rows"]["tasks"]:
        prs = []
        for number in task.get("pr", []):
            record = results.get(number, {"number": number, "data": None, "fetched_at": None,
                                          "checked_at": None, "error": None})
            if not repo:
                record = {**record, "error": {"message": "PR の状態を取得できません（github_repo が設定されていません）", "at": None}}
            status = "error" if record["error"] else "ok" if record["data"] else "pending"
            age = max(0, (datetime.fromisoformat(at) - datetime.fromisoformat(record["fetched_at"])).total_seconds()) if record["fetched_at"] else None
            prs.append({**record, "status": status, "age_seconds": age,
                        "url": f"https://github.com/{repo}/pull/{number}" if repo else None})
        flags = task_flags(task, prs)
        tasks.append({**task, "prs": prs, "pr_flags": flags})
        summary["linked_count"] += len(prs)
        summary["mismatch_count"] += any(flag["kind"] == "mismatch" for flag in flags)
        summary["ci_failure_count"] += any(flag["kind"] == "ci" for flag in flags)
        summary["unknown_count"] += sum(pr["status"] != "ok" for pr in prs)
        for pr in prs:
            if pr["error"] and pr["error"]["message"] not in summary["errors"]:
                summary["errors"].append(pr["error"]["message"])
    return {**good, "rows": {**good["rows"], "tasks": tasks}, "pr_summary": summary}
