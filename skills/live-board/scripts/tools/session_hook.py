"""Record session tasks locally. Internal failures must never block a tool."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unicodedata


SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
STATUSES = {"pending", "in_progress", "completed"}
SECRET_SHAPES = re.compile(
    r"(?<![A-Za-z0-9_])(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+"
    r"|(?<![A-Za-z0-9_])sk-(?:ant-)?[A-Za-z0-9_-]{16,}"
    r"|(?<![A-Za-z0-9_])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])xox[abprs]-[A-Za-z0-9-]+"
    r"|(?i:(?<![A-Za-z0-9_])Bearer)\s+[A-Za-z0-9._~+/-]+=*"
)
SECRET_VALUES = re.compile(
    r'''(?P<prefix>(?i:(?<![\w./\\-])["']?[\w.-]*(?:password|passwd|secret|token|api_key|apikey)'''
    r'''[\w.-]*["']?[ \t]*[:=][ \t]*))(?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;"'<>}]+)'''
)


def redact_text(text):
    """Mask common credentials, preserving assignment keys and value quotes."""
    text = SECRET_SHAPES.sub("[redacted]", text)

    def mask_value(match):
        value = match["value"]
        quote = value[0] if value[0] in "\"'" else ""
        return match["prefix"] + quote + "[redacted]" + quote

    return SECRET_VALUES.sub(mask_value, text)


def sanitize_text(text):
    text = "".join(char for char in text if unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Zl", "Zp"})
    return redact_text(text)


def project_root(cwd):
    path = Path(cwd).absolute()
    for directory in (path, *path.parents):
        if (directory / ".git").exists():
            return str(directory)
    return cwd


def task_list(items, text_key):
    if not isinstance(items, list):
        raise ValueError("Expected a task list")
    tasks = []
    for index, item in enumerate(items, 1):
        if (not isinstance(item, dict) or not isinstance(item.get(text_key), str)
                or item.get("status") not in STATUSES):
            raise ValueError("Invalid task")
        tasks.append({"id": str(index), "text": sanitize_text(item[text_key]), "status": item["status"]})
    return tasks


def apply_event(record, data, timestamp):
    event = data["hook_event_name"]
    if event == "SessionStart":
        record["ended_at"] = None
        record["waiting_for_user"] = False
        if data.get("source") == "clear":
            record["tasks"] = []
            record["last_prompt"] = ""
    elif event == "SessionEnd":
        record["ended_at"] = timestamp
        record["waiting_for_user"] = False
    elif event == "UserPromptSubmit":
        prompt = data["prompt"]
        if not isinstance(prompt, str):
            raise ValueError("Invalid prompt")
        record["last_prompt"] = sanitize_text(prompt)[:120]
        record["waiting_for_user"] = False
    elif event in {"PreToolUse", "PostToolUse"}:
        tool = data.get("tool_name")
        if tool == "AskUserQuestion":
            record["waiting_for_user"] = event == "PreToolUse"
        elif event != "PostToolUse":
            return False
        else:
            value = data["tool_input"]
            if tool == "TaskCreate":
                task_id = data["tool_response"]["task"]["id"]
                subject = value["subject"]
                if not isinstance(task_id, str) or not task_id or not isinstance(subject, str):
                    raise ValueError("Invalid task")
                record["tasks"] = [task for task in record["tasks"] if task["id"] != task_id]
                record["tasks"].append({"id": task_id, "text": sanitize_text(subject), "status": "pending"})
            elif tool == "TaskUpdate":
                response = data.get("tool_response", {})
                if response.get("success") is False:
                    return False
                task_id = value["taskId"]
                if not isinstance(task_id, str) or not task_id:
                    raise ValueError("Invalid task id")
                if "subject" in value and not isinstance(value["subject"], str):
                    raise ValueError("Invalid task subject")
                status = value.get("status", response.get("statusChange", {}).get("to"))
                if status == "deleted":
                    record["tasks"] = [task for task in record["tasks"] if task["id"] != task_id]
                elif status is None or status in STATUSES:
                    task = next((task for task in record["tasks"] if task["id"] == task_id), None)
                    if task is None:
                        task = {"id": task_id, "text": f"#{task_id}", "status": "pending"}
                        record["tasks"].append(task)
                    if status is not None:
                        task["status"] = status
                    if "subject" in value:
                        task["text"] = sanitize_text(value["subject"])
            elif tool == "TodoWrite":
                record["tasks"] = task_list(value["todos"], "content")
            elif isinstance(tool, str) and re.fullmatch(r"mcp__.*__wrap_up", tool):
                for task in record["tasks"]:
                    task["status"] = "completed"
            elif isinstance(tool, str) and re.fullmatch(r"mcp__.*__update_plan", tool):
                if "items" in value:
                    record["tasks"] = task_list(value["items"], "content")
                elif value.get("advance") is True:
                    tasks = record["tasks"]
                    current = next((index for index, task in enumerate(tasks)
                                    if task["status"] == "in_progress"), None)
                    if current is not None:
                        tasks[current]["status"] = "completed"
                    pending = next((task for task in tasks[0 if current is None else current + 1:]
                                    if task["status"] == "pending"), None)
                    if pending is not None:
                        pending["status"] = "in_progress"
                else:
                    done = value.get("done", [])
                    start = value.get("start")
                    for index, task in enumerate(record["tasks"], 1):
                        if index in done:
                            task["status"] = "completed"
                        if type(start) is int and index == start:
                            task["status"] = "in_progress"
            else:
                return False
    else:
        return False
    return True


def prune_sessions(folder, now):
    cutoff = now - timedelta(days=7)
    for path in folder.glob("*.json"):
        if path.is_symlink() or not SESSION_ID.fullmatch(path.stem) or not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                record = json.load(stream)
            if record.get("schema") != 1 or record.get("session_id") != path.stem:
                continue
            if not record.get("ended_at"):
                continue
            timestamp = datetime.fromisoformat(record["ended_at"])
            if timestamp.tzinfo is not None and timestamp < cutoff:
                path.unlink()
        except Exception:
            continue


@contextmanager
def session_lock(path):
    lock = path.with_suffix(".lock")
    deadline = time.monotonic() + 2
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("Session record is busy")
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 30:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Session record is busy")
            time.sleep(min(0.02, remaining))
    try:
        yield
    finally:
        os.close(descriptor)
        lock.unlink(missing_ok=True)


def main():
    try:
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8")
        data = json.load(sys.stdin)
        session_id, cwd = data["session_id"], data["cwd"]
        if (not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id)
                or not isinstance(cwd, str) or not cwd.strip()):
            return 0
        folder = Path(sys.argv[1]) / "sessions"
        path = folder / f"{session_id}.json"
        if path.is_symlink():
            return 0
        now = datetime.now(timezone.utc)
        timestamp = now.isoformat()
        record = {"schema": 1, "session_id": session_id, "cwd": cwd, "project": project_root(cwd),
                  "started_at": timestamp, "updated_at": timestamp, "ended_at": None,
                  "waiting_for_user": False, "last_prompt": "", "tasks": []}
        # Reject malformed/irrelevant events before creating any on-disk state.
        if not apply_event({**record, "tasks": []}, data, timestamp):
            return 0
        folder.mkdir(parents=True, exist_ok=True)
        with session_lock(path):
            if path.exists():
                with path.open(encoding="utf-8") as stream:
                    existing = json.load(stream)
                if existing.get("schema") == 1 and existing.get("session_id") == session_id:
                    record.update(existing)
            record.update(cwd=cwd, project=project_root(cwd), updated_at=timestamp)
            if not apply_event(record, data, timestamp):
                return 0
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                                 dir=folder, suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(record, stream, ensure_ascii=False)
                    stream.write("\n")
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        prune_sessions(folder, now)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
