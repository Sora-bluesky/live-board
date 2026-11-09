import importlib.util
import hashlib
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import unicodedata

from pr_status import pr_numbers


TITLE_MAX_CHARS = 40
LABEL_MAX_CHARS = 12
WBS_CARD_KEYS = frozenset({"code", "section", "text", "note", "status", "label", "ball", "progress"})
WBS_TOP_LEVEL_KEYS = frozenset({"schema", "lane", "repo", "handoff", "generated_at", "done_hidden", "cards"})


def quote_file_value(value):
    text = "".join(char for char in str(value)
                   if unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Zl", "Zp"})
    if len(text) > 40:
        text = text[:39] + "…"
    return f"「{text}」"


def title_warning(code, title):
    if len(title) > TITLE_MAX_CHARS:
        return f"課題 {quote_file_value(code)}: 題が{len(title)}字です。題は作業の名前だけを{TITLE_MAX_CHARS}字以内で書き、経緯・数値・PR は note に移してください。"
    return None


def unknown_key_warnings(data, allowed, subject):
    return [f"{subject}: 不明なキー{quote_file_value(key)}はボードに表示されません。内容は note に移してください。"
            for key in sorted(data.keys() - allowed)]


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(markup):
    parser = PlainText()
    parser.feed(markup)
    parser.close()
    return "".join(parser.parts)


def winsmux_backlog(project):
    import yaml

    build_path = Path(project["build_py"])
    spec = importlib.util.spec_from_file_location("live_board_backlog", build_path)
    if spec is None or spec.loader is None:
        raise ValueError("build_py をPythonモジュールとして読み込めません。")
    module = importlib.util.module_from_spec(spec)
    # Import by path without writing bytecode beside the read-only reference.
    source = spec.loader.get_source(spec.name)
    exec(compile(source, str(build_path), "exec"), module.__dict__)
    try:
        with open(project["source"], encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = f"（行{mark.line + 1}、列{mark.column + 1}）" if mark else ""
        raise ValueError(f"YAMLの構文が正しくありません{location}。") from None
    errors = module.validate(data)
    if errors:
        raise ValueError("検証に失敗しました: " + "; ".join(errors))
    meta = {key: str(data["meta"][key]) for key in ("product", "updated", "current_phase")}
    # dashboard_rows intentionally drops extra YAML keys; task ids are the join key.
    linked = {task["id"]: pr_numbers(task["pr"], task["id"])
              for task in data["tasks"] if "pr" in task}
    rows = module.dashboard_rows(data)
    style_warnings = []
    for task in rows["tasks"]:
        if warning := title_warning(task["id"], task["title"]):
            style_warnings.append(warning)
        task["pr"] = linked.get(task["id"], [])
        task["source_fields"] = {key: task.get(key, "") for key in
                                 ("title", "status", "phase", "version", "deps", "blocked_by", "pr", "label")}
    return {"rows": rows, "meta": meta, "warnings": [], "style_warnings": style_warnings}


def wbs_cards(project):
    try:
        with open(project["source"], encoding="utf-8") as stream:
            data = json.load(stream)
    except json.JSONDecodeError as error:
        raise ValueError(f"JSONの構文が正しくありません（行{error.lineno}、列{error.colno}）。") from None
    if not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1:
        raise ValueError("wbs-cards は schema 1 のJSONを指定してください。")
    if any(not isinstance(data.get(key), str) for key in ("lane", "repo", "handoff", "generated_at")):
        raise ValueError("lane・repo・handoff・generated_at は文字列にしてください。")
    hidden = data.get("done_hidden")
    if type(hidden) is not int or hidden < 0:
        raise ValueError("done_hidden は0以上の整数にしてください。")
    if not isinstance(data.get("cards"), list):
        raise ValueError("cards は配列にしてください。")
    mapping = {"p-run": "doing", "p-hold": "todo", "p-wait": "blocked", "p-owner": "blocked", "p-sora": "blocked"}
    tasks, phases = [], {}
    warnings = []
    style_warnings = unknown_key_warnings(data, WBS_TOP_LEVEL_KEYS, "最上位")
    seen = set()
    for index, card in enumerate(data["cards"], 1):
        if not isinstance(card, dict) or any(
            not isinstance(card.get(key), str)
            for key in ("code", "section", "text", "note", "status", "label")
        ) or type(card.get("ball", False)) is not bool:
            raise ValueError(f"cards の{index}件目の形式が正しくありません。")
        code = card["code"]
        if not code.strip() or code in seen or not card["text"].strip():
            raise ValueError(f"cards の{index}件目には重複しないcodeと空でないtextが必要です。")
        seen.add(code)
        style_warnings.extend(unknown_key_warnings(card, WBS_CARD_KEYS, f"課題 {quote_file_value(code)}"))
        if warning := title_warning(code, card["text"]):
            style_warnings.append(warning)
        if len(card["label"]) > LABEL_MAX_CHARS:
            style_warnings.append(f"課題 {quote_file_value(code)}: label が{len(card['label'])}字です。label は{LABEL_MAX_CHARS}字以内の短いバッジです。文章は note に移してください。")
        raw_status = card["status"]
        ball = card.get("ball", False)
        if raw_status == "p-sora":
            style_warnings.append(f'課題 {quote_file_value(code)}: status "p-sora" は旧形式です。"p-owner" と書いてください。')
        if ball:
            style_warnings.append(f'課題 {quote_file_value(code)}: ball: true は旧形式です。代わりに status: "p-owner" と書いてください。')
        status = mapping.get(raw_status, "todo")
        if raw_status not in mapping:
            if raw_status == "done":
                warnings.append(f'課題 {quote_file_value(code)}: status "done" は使えません。完了は cards から外し done_hidden を増やします。')
            else:
                warnings.append(f"課題 {quote_file_value(code)}: 不明な状態{quote_file_value(raw_status)}を未着手として扱いました。使える値は p-run, p-hold, p-wait, p-owner です（旧形式の p-sora も受け付けます）。")
        section = card["section"]
        phases.setdefault(section, {"id": section, "title": section, "order": len(phases), "is_current": "no"})
        tasks.append({
            "id": code, "title": plain_text(card["text"]), "phase": section, "version": "", "status": status,
            "raw_status": raw_status, "note": card["note"], "label": card["label"], "ball": ball,
            "done_when": "", "deps": "", "blocked_by": "", "ready": "yes" if status == "todo" else "no",
            "source_fields": {"text": plain_text(card["text"]), "ball": ball, **{key: card[key] for key in ("label", "status", "section")}},
        })
        if "progress" in card:
            progress = card["progress"]
            if (not isinstance(progress, dict)
                or not isinstance(progress.get("count_files"), str) or not progress["count_files"].strip()
                or ("total" in progress and (type(progress["total"]) is not int or progress["total"] <= 0))
                or ("label" in progress and not isinstance(progress["label"], str))):
                warnings.append(f"課題 {quote_file_value(code)}: progress の count_files は空でない文字列、total は正の整数、label は文字列にしてください。")
            else:
                tasks[-1]["progress"] = {key: progress[key] for key in ("count_files", "total", "label") if key in progress}
    pending = sum(task["raw_status"] == "p-hold" for task in tasks)
    if pending and not any(task["status"] == "doing" for task in tasks):
        warnings.append(f"進行中の課題がありません（未着手 {pending} 件）。次の課題を進行中へ移すか、止まっている理由をカードに書いてください。")
    for phase in phases.values():
        children = [task for task in tasks if task["phase"] == phase["id"]]
        phase["status"] = next((value for value in ("doing", "blocked", "todo")
                                if any(task["status"] == value for task in children)), "todo")
    return {
        "rows": {"phases": list(phases.values()), "versions": [], "tasks": tasks},
        "meta": {"product": project.get("name", data["lane"]), "updated": data["generated_at"], "current_phase": ""},
        "done_hidden": hidden, "warnings": warnings, "style_warnings": style_warnings,
    }


def session_time(value):
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError("Session timestamps require a timezone")
    return timestamp


def folder_name(value):
    return Path(value.replace("\\", "/")).name or value


def session_project_path(value):
    return os.path.normcase(os.path.abspath(os.path.normpath(value))).replace("\\", "/")


def session_project_id(value):
    path = session_project_path(value)
    slug = re.sub(r"[^a-z0-9_-]", "-", folder_name(path).lower())
    return f"{slug}-{hashlib.sha256(path.encode('utf-8')).hexdigest()[:6]}"


def read_sessions(source):
    folder = Path(source)
    now = datetime.now(timezone.utc)
    records, warnings = [], []
    if folder.exists() and not folder.is_dir():
        raise ValueError("claude-sessions の source はセッションフォルダーにしてください。")
    for path in sorted(folder.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                record = json.load(stream)
            if (not isinstance(record, dict) or type(record.get("schema")) is not int or record["schema"] != 1
                    or any(not isinstance(record.get(key), str) or not record[key]
                           for key in ("session_id", "cwd", "project", "started_at", "updated_at"))
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", record["session_id"])
                    or record["session_id"] != path.stem
                    or type(record.get("waiting_for_user")) is not bool
                    or not isinstance(record.get("last_prompt"), str)
                    or not isinstance(record.get("tasks"), list)):
                raise ValueError("Invalid session record")
            session_time(record["started_at"])
            if not Path(record["project"]).is_absolute():
                raise ValueError("Session project requires an absolute path")
            session_time(record["updated_at"])
            ended = session_time(record["ended_at"]) if record["ended_at"] is not None else None
            if ended is not None and ended < now - timedelta(days=1):
                continue
            seen = set()
            for task in record["tasks"]:
                if (not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"]
                        or task["id"] in seen or not isinstance(task.get("text"), str)
                        or task.get("status") not in {"pending", "in_progress", "completed"}):
                    raise ValueError("Invalid session task")
                seen.add(task["id"])
        except (OSError, ValueError, TypeError, KeyError):
            warnings.append(f"セッション記録 {quote_file_value(path.name)}: 読み込めない記録を除外しました。")
            continue
        records.append(record)
    return records, warnings


def read_session_sources(project):
    records, warnings, failed_sources = {}, [], {}
    folder_providers = {}
    for provider in project.get("session_providers", [project]):
        try:
            sessions, messages = read_sessions(provider["source"])
        except (OSError, ValueError) as error:
            failed_sources[provider["source"]] = error
            continue
        warnings.extend(messages)
        hidden = {session_project_path(path) for path in provider.get("hidden", [])}
        for record in sessions:
            path = session_project_path(record["project"])
            if path in hidden:
                continue
            contributors = folder_providers.setdefault(path, [])
            if provider not in contributors:
                contributors.append(provider)
            key = (path, record["session_id"])
            previous = records.get(key)
            if previous is None or session_time(record["updated_at"]) > session_time(previous[0]["updated_at"]):
                # Keep folder provenance even when a later provider wins a record.
                records[key] = (record, contributors)
    return list(records.values()), list(dict.fromkeys(warnings)), failed_sources


def session_projects(project):
    records, warnings, failed_sources = read_session_sources(project)
    groups = {}
    for record, providers in records:
        path = session_project_path(record["project"])
        groups.setdefault(path, ([], providers))[0].append(record)
    projects = []
    for path, (sessions, providers) in groups.items():
        owner = providers[0]
        config = {**owner, "id": session_project_id(path),
                  "name": folder_name(sessions[0]["project"]) or owner["name"],
                  "session_project": path, "session_providers": providers}
        projects.append((config, claude_sessions(config, sessions, warnings)))
    return sorted(projects, key=lambda item: (
        not item[1]["meta"]["session_active"],
        -session_time(item[1]["meta"]["updated"]).timestamp(), item[0]["id"],
    )), set(failed_sources)


def claude_sessions(project, records=None, warnings=None):
    failed_sources = {}
    if records is None:
        sourced_records, warnings, failed_sources = read_session_sources(project)
        records = [record for record, _ in sourced_records]
    if "session_project" in project:
        records = [record for record in records
                   if session_project_path(record["project"]) == project["session_project"]]
    if failed_sources:
        raise next(iter(failed_sources.values()))
    tasks, hidden, updated = [], 0, ""
    section = project["name"]
    for record in records:
        modified = session_time(record["updated_at"])
        updated = max(updated, modified.astimezone(timezone.utc).isoformat())
        ended = record["ended_at"] is not None
        session_id = record["session_id"]
        entries = record["tasks"]
        if not entries:
            entries = [{"id": "prompt", "text": record["last_prompt"] or "作業中のセッション", "status": "in_progress"}]
        for entry in entries:
            if entry["status"] == "completed":
                hidden += 1
                continue
            status, raw_status = {"pending": ("todo", "p-hold"), "in_progress": ("doing", "p-run")}[entry["status"]]
            paused = ended and entry["status"] == "in_progress"
            if paused:
                status, raw_status = "todo", "p-hold"
            elif entry["status"] == "in_progress" and record["waiting_for_user"]:
                status, raw_status = "blocked", "p-owner"
            title = entry["text"]
            tasks.append({
                "id": f"{session_id}:{entry['id']}", "title": title, "phase": section, "version": "",
                "status": status, "raw_status": raw_status,
                "note": session_id[:8], "label": "休止中" if paused else "", "ball": False,
                "done_when": "", "deps": "", "blocked_by": "", "ready": "yes" if status == "todo" else "no",
                "source_fields": {"text": title, "status": raw_status, "section": section,
                                  "paused": paused},
            })
    phases = [{"id": section, "title": section, "order": 0, "is_current": "no",
               "status": next((status for status in ("doing", "blocked", "todo")
                               if any(task["status"] == status for task in tasks)), "done")}] if records else []
    return {"rows": {"phases": phases, "versions": [], "tasks": tasks},
            "meta": {"product": project["name"], "updated": updated, "current_phase": "",
                     "session_active": any(record["ended_at"] is None for record in records)},
            "done_hidden": hidden, "warnings": warnings or [], "style_warnings": []}


ADAPTERS = {"winsmux-backlog": winsmux_backlog, "wbs-cards": wbs_cards, "claude-sessions": claude_sessions}
