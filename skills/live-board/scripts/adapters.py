import importlib.util
from html.parser import HTMLParser
import json
from pathlib import Path
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


ADAPTERS = {"winsmux-backlog": winsmux_backlog, "wbs-cards": wbs_cards}
