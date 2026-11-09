def validate(data):
    if not isinstance(data, dict) or any(
        key not in data.get("meta", {}) for key in ("product", "updated", "current_phase")
    ):
        return ["meta の必須項目がありません。"]
    return []


def dashboard_rows(data):
    tasks = []
    statuses = {task["id"]: task["status"] for task in data["tasks"]}
    for task in data["tasks"]:
        deps = task.get("deps", [])
        blocked = [key for key in deps if statuses.get(key) != "done"]
        tasks.append({**{key: task.get(key, "") for key in
                         ("id", "title", "status", "phase", "version", "done_when", "label")},
                      "deps": ", ".join(deps), "blocked_by": ", ".join(blocked),
                      "ready": "yes" if task["status"] == "todo" and not blocked else "no"})
    return {"tasks": tasks, "phases": data["phases"], "versions": data["versions"]}
