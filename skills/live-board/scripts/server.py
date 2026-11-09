import argparse
from datetime import datetime, timezone
import fnmatch
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sys
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit

from adapters import ADAPTERS
from pr_status import (
    PRFetchError, PR_REFRESH_SECONDS, cache_result, enrich_snapshot,
    fetch_pull_requests, validate_repo,
)


ROOT = Path(__file__).resolve().parent
POLL_SECONDS = 1
KEEPALIVE_SECONDS = 15
CONTENT_CHECK_SECONDS = 30
PROGRESS_CHECK_SECONDS = 5


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def file_signature(stat):
    return (
        stat.st_mtime_ns, stat.st_size, stat.st_dev, stat.st_ino,
        getattr(stat, "st_ctime_ns", None),
        getattr(stat, "st_birthtime_ns", getattr(stat, "st_birthtime", None)),
    )


def source_date_info(updated, mtime):
    try:
        file_date = datetime.fromtimestamp(mtime).date().isoformat()
    except (OSError, OverflowError, ValueError, TypeError):
        file_date = None
    try:
        declared = datetime.fromisoformat(updated)
        declared_date = (declared.astimezone() if declared.tzinfo else declared).date().isoformat()
    except (OSError, OverflowError, ValueError, TypeError):
        declared_date = None
    return {"declared_local_date": declared_date, "file_local_date": file_date,
            "mismatch": declared_date != file_date if declared_date and file_date else None}


def linked_pr_numbers(last_good):
    tasks = last_good["rows"]["tasks"] if last_good else []
    return {number for task in tasks for number in task.get("pr", [])}


def read_activity(directory):
    if directory is None:
        return [], []
    try:
        paths = sorted(Path(directory).iterdir())
    except FileNotFoundError:
        return [], []
    except OSError as error:
        return [], [{"file": "activity_dir", "message": f"フォルダーを読み込めません（{type(error).__name__}）。"}]
    items, errors = [], []
    for path in paths:
        if path.suffix != ".json":
            continue
        try:
            if path.is_dir():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or any(
                not isinstance(data.get(key), str) or not data[key].strip()
                for key in ("card", "step", "heartbeat_at")
            ):
                raise ValueError("card・step・heartbeat_at には空でない文字列が必要です。")
            if data["card"] != path.stem:
                raise ValueError("card とファイル名の課題IDが一致しません。")
            if "detail" in data and not isinstance(data["detail"], str):
                raise ValueError("detail は文字列にしてください。")
            item = {key: data[key] for key in ("card", "step", "detail") if key in data}
            item["file"] = path.name
            for key in ("started_at", "heartbeat_at"):
                if key not in data:
                    continue
                value = data[key]
                if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}[Tt ]", value):
                    raise ValueError(f"{key} はISO 8601の日時にしてください。")
                try:
                    item[key] = datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()
                except (OSError, OverflowError, ValueError):
                    raise ValueError(f"{key} はISO 8601の日時にしてください。") from None
            json_bytes(item)
            items.append(item)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, RecursionError) as error:
            if isinstance(error, OSError):
                message = f"ファイルを読み込めません（{type(error).__name__}）。"
            elif isinstance(error, UnicodeError):
                message = "UTF-8で表示できる文字列にしてください。"
            elif isinstance(error, (json.JSONDecodeError, RecursionError)):
                message = "JSONの構文が正しくありません。"
            else:
                message = str(error)
            name = path.name.encode("utf-8", errors="backslashreplace").decode("utf-8")
            errors.append({"file": name, "message": message})
    return items, errors


def history_fields(value):
    if isinstance(value, dict):
        if all(isinstance(key, str) for key in value):
            return {key: history_fields(item) for key, item in value.items()}
        # Preserve both 1 and "1", including their values, in a stable order.
        return sorted(([type(key).__name__, str(key), history_fields(item)]
                       for key, item in value.items()), key=lambda entry: (entry[0], entry[1]))
    if isinstance(value, (list, tuple)):
        return [history_fields(item) for item in value]
    return value


class CardHistory:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self.projects = {}
        self.lock = threading.Lock()
        self.dirty = False
        self.warning = None
        if self.path is not None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError()
                for cards in data.values():
                    if not isinstance(cards, dict):
                        raise ValueError()
                    for entry in cards.values():
                        if (not isinstance(entry, dict)
                            or not isinstance(entry.get("visible_hash" if "visible_hash" in entry else "hash"), str)
                            or type(entry.get("baseline")) is not bool
                            or not isinstance(entry.get("changed_at"), str)
                            or datetime.fromisoformat(entry["changed_at"]).utcoffset() is None):
                            raise ValueError()
                self.projects = data
            except (OSError, ValueError, TypeError, RecursionError):
                pass

    def annotate(self, project_id, tasks, mtime):
        with self.lock:
            first = project_id not in self.projects
            previous = self.projects.get(project_id, {})
            cards = {}
            for task in tasks:
                try:
                    canonical = json.dumps(history_fields(task.get("source_fields", task)),
                                           sort_keys=True, ensure_ascii=False, allow_nan=False,
                                           separators=(",", ":"), default=str)
                    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                except Exception:
                    # History must not prevent an otherwise valid source from loading.
                    continue
                finally:
                    task.pop("source_fields", None)
                entry = previous.get(task["id"])
                if entry is not None and "visible_hash" not in entry:
                    entry = {"visible_hash": digest, "changed_at": entry["changed_at"], "baseline": True}
                elif entry is None or entry["visible_hash"] != digest:
                    try:
                        changed_at = datetime.fromtimestamp(mtime, timezone.utc).isoformat()
                    except (OSError, OverflowError, ValueError, TypeError):
                        continue
                    entry = {"visible_hash": digest, "changed_at": changed_at, "baseline": first}
                cards[task["id"]] = entry
            if first or cards != previous:
                self.projects[project_id] = cards
                self.dirty = True
            for task in tasks:
                entry = cards.get(task["id"])
                task["description_changed_at"] = entry["changed_at"] if entry else None
                task["description_baseline"] = entry["baseline"] if entry else True

    def save(self):
        with self.lock:
            if self.path is None or not self.dirty:
                return
            temporary = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", delete=False,
                                                 dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp") as stream:
                    temporary = Path(stream.name)
                    json.dump(self.projects, stream, ensure_ascii=False, allow_nan=False)
                    stream.write("\n")
                os.replace(temporary, self.path)
                self.dirty = False
                self.warning = None
            except (OSError, ValueError, RecursionError):
                self.warning = "記述履歴を保存できません。再起動すると記述の更新時刻が失われる可能性があります。"
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass


def progress_root(source):
    for directory in Path(source).absolute().parents:
        if (directory / ".git").exists():
            return directory.resolve()
    return None


def count_progress(root, pattern):
    parts = pattern.replace("\\", "/").split("/")
    if (Path(pattern).is_absolute() or any(PureWindowsPath(part).drive for part in parts)
        or pattern.startswith(("/", "\\")) or ".." in parts or "**" in pattern):
        raise ValueError("取得元はルート内の相対パスにしてください（絶対パス・ドライブ・..・** は使えません）。")
    parts = [part for part in parts if part not in ("", ".")]
    if not parts:
        raise ValueError("取得元のファイルパターンを指定してください。")
    # Check the fixed directory prefix explicitly: glob APIs can hide read errors.
    directory = root
    index = 0
    while index < len(parts) - 1 and not any(char in parts[index] for char in "*?["):
        directory = directory / parts[index]
        index += 1
    if not directory.resolve().is_relative_to(root):
        raise ValueError("取得元がルートの外にあります")
    if not directory.is_dir():
        raise ValueError("取得元が見つかりません")
    count, newest = 0, None

    def visit(directory, remaining):
        nonlocal count, newest
        with os.scandir(directory) as entries:
            for entry in entries:
                if not fnmatch.fnmatch(entry.name, remaining[0]):
                    continue
                path = Path(entry.path)
                if not path.resolve().is_relative_to(root):
                    continue
                if len(remaining) > 1:
                    if entry.is_dir():
                        visit(path, remaining[1:])
                elif entry.is_file():
                    mtime = entry.stat().st_mtime
                    count += 1
                    newest = max(newest, mtime) if newest is not None else mtime

    visit(directory, parts[index:])
    return count, datetime.fromtimestamp(newest, timezone.utc).isoformat() if newest is not None else None


def read_progress(source, tasks):
    items, errors = {}, []
    for task in tasks:
        spec = task.get("progress")
        if spec is None:
            continue
        try:
            root = progress_root(source)
            if root is None:
                raise ValueError("プロジェクトのルートが見つかりません")
            count, newest = count_progress(root, spec["count_files"])
            items[task["id"]] = {"count": count, "total": spec.get("total"), "label": spec.get("label"),
                                 "last_changed_at": newest}
        except (OSError, ValueError, RuntimeError) as error:
            message = str(error) if isinstance(error, ValueError) else "取得元を読み込めません"
            errors.append({"card": task["id"], "message": message})
    return {"items": items, "errors": errors}


def progress_summary(progress):
    return {"count": len(progress["items"]), "errors": progress["errors"]}


class ProjectState:
    def __init__(self, config, history=None):
        self.config = config
        self.history = history if history is not None else CardHistory()
        self.history_warning = None
        self.lock = threading.Lock()
        self.last_good = None
        self.error = None
        self.source_mtime = None
        self.checked_at = None
        self.signature = None
        self.content_hash = None
        self.content_checked_at = None
        self.source_revision = 0
        self.activity_items = []
        self.activity_errors = []
        self.activity = {"count": 0, "items": [], "unmatched": [], "errors": []}
        self.activity_revision = 0
        self.pr_results = {}
        self.pr_revision = 0
        self.progress = {"items": {}, "errors": []}
        self.progress_revision = 0
        self.progress_checked_at = None
        self.body = json_bytes({
            "last_good": None, "error": None, "source_mtime": None, "checked_at": None,
            "activity": self.activity, "activity_revision": self.activity_revision,
            "progress": self.progress, "progress_revision": self.progress_revision,
            "history_warning": self.history_warning,
        })

    def _publish(self, last_good, error):
        checked_at = now()
        # An unlinked PR must lose its current status even between fetch cycles.
        numbers = linked_pr_numbers(last_good)
        results = {number: value for number, value in self.pr_results.items() if number in numbers}
        last_good = enrich_snapshot(last_good, self.config.get("github_repo"), results, checked_at)
        cards = {task["id"] for task in last_good["rows"]["tasks"]} if last_good else set()
        activity = {
            "count": len(self.activity_items),
            "items": [item for item in self.activity_items if item["card"] in cards],
            "unmatched": [item for item in self.activity_items if item["card"] not in cards],
            "errors": self.activity_errors,
        }
        activity_changed = activity != self.activity
        activity_revision = self.activity_revision + activity_changed
        self.history.save()
        warning_changed = self.history_warning != self.history.warning
        self.history_warning = self.history.warning
        body = json_bytes({
            "last_good": last_good, "error": error,
            "source_mtime": self.source_mtime, "checked_at": checked_at,
            "pr_revision": self.pr_revision,
            "activity": activity, "activity_revision": activity_revision,
            "progress": self.progress, "progress_revision": self.progress_revision,
            "history_warning": self.history_warning,
        })
        self.last_good = last_good
        self.pr_results = results
        self.error = error
        self.checked_at = checked_at
        self.body = body
        self.activity = activity
        self.activity_revision = activity_revision
        return activity_changed or warning_changed

    def refresh_progress(self, tasks, force=False):
        checked = time.monotonic()
        if not force and self.progress_checked_at is not None and checked - self.progress_checked_at < PROGRESS_CHECK_SECONDS:
            return False
        self.progress_checked_at = checked
        progress = read_progress(self.config["source"], tasks) if self.config["adapter"] == "wbs-cards" else {"items": {}, "errors": []}
        if progress == self.progress:
            return False
        self.progress = progress
        self.progress_revision += 1
        return True

    def check(self):
        with self.lock:
            previous_error = self.error
            previous_activity_revision = self.activity_revision
            self.activity_items, self.activity_errors = read_activity(self.config.get("activity_dir"))
            changed = False
            progress_changed = False
            try:
                source = Path(self.config["source"])
                stat = source.stat()
                signature = file_signature(stat)
                self.source_mtime = stat.st_mtime
                checked_at = time.monotonic()
                content_due = self.content_checked_at is None or checked_at - self.content_checked_at >= CONTENT_CHECK_SECONDS
                if signature == self.signature and self.error is None and not content_due:
                    progress_changed = self.refresh_progress(self.last_good["rows"]["tasks"])
                    return self._publish(self.last_good, None) or progress_changed
                content_hash = hashlib.sha256(source.read_bytes()).digest()
                changed = signature != self.signature or content_hash != self.content_hash
                self.content_checked_at = checked_at
                if not changed and self.error is None:
                    progress_changed = self.refresh_progress(self.last_good["rows"]["tasks"])
                    return self._publish(self.last_good, None) or progress_changed
                self.signature = signature
                self.content_hash = content_hash
                result = ADAPTERS[self.config["adapter"]](self.config)
                after = source.stat()
                if signature != file_signature(after) or content_hash != hashlib.sha256(source.read_bytes()).digest():
                    self.signature = None
                    raise ValueError("読込中にファイルが変わりました。次の確認で読み直します。")
                json_bytes({**result, "rows": {**result["rows"], "tasks": [
                    {key: value for key, value in task.items() if key != "source_fields"}
                    for task in result["rows"]["tasks"]
                ]}})
                self.history.annotate(self.config["id"], result["rows"]["tasks"], self.source_mtime)
                progress_changed = self.refresh_progress(result["rows"]["tasks"], force=True)
                self._publish({
                    **result, "source_mtime": self.source_mtime, "loaded_at": now(),
                    "source_date": source_date_info(result["meta"]["updated"], self.source_mtime),
                }, None)
            except Exception as error:
                progress_changed = self.refresh_progress(self.last_good["rows"]["tasks"] if self.last_good else [])
                if isinstance(error, OSError):
                    message = f"ファイルを読み込めません（{type(error).__name__}）。"
                    try:
                        stat = Path(self.config["source"]).stat()
                        self.source_mtime = stat.st_mtime
                        signature = file_signature(stat)
                    except OSError:
                        self.source_mtime = None
                        signature = ("unavailable", type(error).__name__)
                    changed = signature != self.signature
                    self.signature = signature
                else:
                    message = str(error) or type(error).__name__
                message = message.encode("utf-8", errors="backslashreplace").decode("utf-8")
                self._publish(self.last_good, {"message": message, "at": now()})
                changed = changed or previous_error is None or previous_error["message"] != message
            source_changed = changed or (previous_error is not None and self.error is None)
            self.source_revision += source_changed
            return source_changed or self.activity_revision != previous_activity_revision or progress_changed

    def pr_request(self):
        with self.lock:
            return self.config.get("github_repo"), linked_pr_numbers(self.last_good)

    def apply_pr_results(self, cached, requested):
        with self.lock:
            # Re-read active links under the publication lock: the source may have
            # changed during gh. Historical cache entries are never fresh results.
            results = {}
            for number in linked_pr_numbers(self.last_good):
                if number not in cached:
                    continue
                value = cached[number]
                if number not in requested and not value["error"]:
                    value = {**value, "error": {"message": "PR の状態を取得できません（再取得を待っています）", "at": None}}
                results[number] = value
            if results == self.pr_results:
                return False
            self.pr_results = results
            self.pr_revision += 1
            self._publish(self.last_good, self.error)
            return True

    def snapshot(self):
        with self.lock:
            return json.loads(self.body)

    def snapshot_bytes(self):
        with self.lock:
            return self.body

    def summary(self):
        state = self.snapshot()
        good = state["last_good"]
        tasks = good["rows"]["tasks"] if good else []
        hidden = good.get("done_hidden", 0) if good else 0
        counts = {status: sum(task["status"] == status for task in tasks)
                  for status in ("todo", "doing", "blocked", "done")}
        counts["done"] += hidden
        return {
            "id": self.config["id"], "name": self.config["name"], "adapter": self.config["adapter"],
            "counts": counts if good else None, "total": len(tasks) + hidden if good else None,
            "done_hidden": hidden if good else None,
            "owner_wait": sum(task.get("raw_status") in ("p-owner", "p-sora") or task.get("ball") is True for task in tasks) if good else None,
            "current_phase": good["meta"].get("current_phase") if good else None,
            "source_mtime": state["source_mtime"], "checked_at": state["checked_at"],
            "last_good_mtime": good["source_mtime"] if good else None,
            "loaded_at": good["loaded_at"] if good else None,
            "error": state["error"], "warnings": (good.get("warnings", []) if good else []) + ([state["history_warning"]] if state["history_warning"] else []),
            "style_warnings": good.get("style_warnings", []) if good else [],
            "pr_summary": good["pr_summary"] if good else None,
            "activity": {key: state["activity"][key] for key in ("count", "errors", "unmatched")},
            "progress": progress_summary(state["progress"]),
        }


def project_configs(config):
    projects = config.get("projects") if isinstance(config, dict) else None
    if not isinstance(projects, list) or not projects:
        raise ValueError("projects には1件以上のプロジェクトを指定してください。")
    seen = set()
    for project in projects:
        if not isinstance(project, dict) or any(
            not isinstance(project.get(key), str) or not project[key]
            for key in ("id", "name", "adapter", "source")
        ):
            raise ValueError("各プロジェクトに id・name・adapter・source が必要です。")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", project["id"]) or project["id"] in seen:
            raise ValueError("id は重複しない英数字・ハイフン・アンダースコアにしてください。")
        if project["adapter"] not in ADAPTERS:
            raise ValueError("未登録のadapterです。")
        if "github_repo" in project:
            validate_repo(project["github_repo"])
        if "activity_dir" in project and (
            not isinstance(project["activity_dir"], str) or not project["activity_dir"]
            or not Path(project["activity_dir"]).is_absolute()
        ):
            raise ValueError("activity_dir は空でない絶対パスにしてください。")
        paths = ("source", "build_py") if project["adapter"] == "winsmux-backlog" else ("source",)
        if "build_py" in paths and (not isinstance(project.get("build_py"), str) or not project["build_py"]):
            raise ValueError("winsmux-backlog には build_py が必要です。")
        if any(not Path(project[key]).is_absolute() for key in paths):
            raise ValueError("source と build_py は絶対パスにしてください。")
        seen.add(project["id"])
    return projects


class Board:
    def __init__(self, config, gh_runner=None, history_path=None):
        history = CardHistory(history_path)
        self.projects = {p["id"]: ProjectState(p, history) for p in project_configs(config)}
        self.condition = threading.Condition()
        self.revisions = dict.fromkeys(self.projects, 0)
        self.stopping = threading.Event()
        self.pr_wakeup = threading.Event()
        self.gh_runner = gh_runner
        self.pr_cache = {}
        for project in self.projects.values():
            project.check()
        self.watcher = threading.Thread(target=self.watch, name="source-watcher", daemon=True)
        self.pr_worker = threading.Thread(target=self.refresh_prs, name="pr-fetcher", daemon=True)
        self.pr_wakeup.set()

    def notify(self, project_id):
        with self.condition:
            self.revisions[project_id] += 1
            self.condition.notify_all()

    def watch(self):
        while not self.stopping.wait(POLL_SECONDS):
            for project_id, project in self.projects.items():
                source_revision = project.source_revision
                if project.check():
                    if project.source_revision != source_revision:
                        self.pr_wakeup.set()
                    self.notify(project_id)

    def refresh_prs(self):
        while not self.stopping.is_set():
            self.pr_wakeup.wait(PR_REFRESH_SECONDS)
            self.pr_wakeup.clear()
            if self.stopping.is_set():
                break
            self.refresh_pr_cycle()

    def refresh_pr_cycle(self):
        requests = {}
        for project in self.projects.values():
            repo, numbers = project.pr_request()
            if repo and numbers:
                requests.setdefault(repo, set()).update(numbers)
        for repo, numbers in requests.items():
            if self.stopping.is_set():
                return
            self.refresh_pr_repo(repo, numbers)
            for project_id, project in self.projects.items():
                if project.config.get("github_repo") == repo:
                    if project.apply_pr_results(self.pr_cache[repo], numbers):
                        self.notify(project_id)

    def refresh_pr_repo(self, repo, numbers):
        try:
            results = fetch_pull_requests(repo, numbers, runner=self.gh_runner)
        except Exception as error:
            # Only our sanitized errors may reach the board, never raw runner output.
            reason = str(error) if isinstance(error, PRFetchError) else "gh の実行に失敗しました"
            results = {number: {"data": None, "error": reason} for number in numbers}
        at = now()
        cached = self.pr_cache.setdefault(repo, {})
        for number in numbers:
            cached[number] = cache_result(number, cached.get(number, {}), results[number], at)

    def stop(self):
        self.stopping.set()
        self.pr_wakeup.set()
        with self.condition:
            self.condition.notify_all()
        for thread in (self.watcher, self.pr_worker):
            if thread.is_alive():
                thread.join()


def code_fingerprint(root):
    paths = [root / "server.py", *sorted((root / "static").rglob("*"))]
    digest = hashlib.sha256()
    files = {}
    for path in sorted(p for p in paths if p.is_file()):
        name = path.relative_to(root).as_posix()
        content = path.read_bytes()
        files[name] = hashlib.sha256(content).hexdigest()
        digest.update(name.encode("utf-8") + b"\0" + content + b"\0")
    return digest.hexdigest(), files


class BoardServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self):
        if hasattr(self, "board"):
            self.board.stop()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            self.connection.settimeout(KEEPALIVE_SECONDS + 5)
            super().handle()
        except (ConnectionError, TimeoutError, OSError):
            pass

    def log_message(self, format, *args):
        pass

    def respond(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value, status=200):
        self.respond(json_bytes(value), "application/json; charset=utf-8", status)

    def do_GET(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origins = {f"http://{host}" for host in hosts}
        host_headers = self.headers.get_all("Host", [])
        origin_headers = self.headers.get_all("Origin", [])
        if (len(host_headers) != 1 or host_headers[0] not in hosts
                or (origin_headers and (len(origin_headers) != 1 or origin_headers[0] not in origins))):
            self.respond(b"Forbidden\n", "text/plain; charset=utf-8", 403)
            return
        path = urlsplit(self.path).path
        board = self.server.board
        if path == "/api/projects":
            self.send_json([
                {"id": project_id, "name": project.config["name"]}
                for project_id, project in board.projects.items()
            ])
        elif path == "/api/summary":
            self.send_json([project.summary() for project in board.projects.values()])
        elif path.startswith("/api/projects/"):
            project = board.projects.get(unquote(path.removeprefix("/api/projects/")))
            if project is None:
                self.send_json({"error": "プロジェクトが見つかりません。"}, 404)
            else:
                self.respond(project.snapshot_bytes(), "application/json; charset=utf-8")
        elif path == "/healthz":
            states = {}
            for project_id, project in board.projects.items():
                state = project.snapshot()
                good = state.pop("last_good")
                state["progress"] = progress_summary(state["progress"])
                states[project_id] = {**state, "last_good_at": good["loaded_at"] if good else None}
            self.send_json({
                "fingerprint": self.server.fingerprint, "files": self.server.files,
                "started_at": self.server.started_at, "projects": states,
            })
        elif path == "/events":
            self.events()
        else:
            assets = {
                "/": ("static/index.html", "text/html; charset=utf-8"),
                "/static/app.js": ("static/app.js", "text/javascript; charset=utf-8"),
                "/static/style.css": ("static/style.css", "text/css; charset=utf-8"),
            }
            if path not in assets:
                self.send_json({"error": "ページが見つかりません。"}, 404)
                return
            name, content_type = assets[path]
            try:
                content = (self.server.root / name).read_bytes()
            except OSError:
                self.send_json({"error": "画面のファイルを読み込めません。"}, 404)
                return
            self.respond(content, content_type)

    def events(self):
        board = self.server.board
        with board.condition:
            seen = board.revisions.copy()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b": connected\n\n")
        self.wfile.flush()
        while not board.stopping.is_set():
            with board.condition:
                board.condition.wait_for(
                    lambda: board.stopping.is_set() or board.revisions != seen,
                    timeout=KEEPALIVE_SECONDS,
                )
                changed = [key for key in seen if seen[key] != board.revisions[key]]
                seen = board.revisions.copy()
            if board.stopping.is_set():
                break
            if changed:
                for project_id in changed:
                    self.wfile.write(f"event: project\ndata: {json.dumps(project_id)}\n\n".encode("utf-8"))
            else:
                self.wfile.write(b"event: heartbeat\ndata: {}\n\n")
            self.wfile.flush()


def create_server(config, port=8795, root=ROOT, gh_runner=None, history_path=None):
    root = Path(root)
    board = Board(config, gh_runner=gh_runner, history_path=history_path)
    fingerprint, files = code_fingerprint(root)
    httpd = BoardServer(("127.0.0.1", port), Handler)
    httpd.board = board
    httpd.root = root
    httpd.fingerprint = fingerprint
    httpd.files = files
    httpd.started_at = now()
    board.watcher.start()
    board.pr_worker.start()
    return httpd


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="正本ファイルを読むローカル進捗盤")
    parser.add_argument("--config", type=Path, default=ROOT / "projects.local.json")
    parser.add_argument("--port", type=int, default=8795)
    parser.add_argument("--history", type=Path, help="Card history file (default: tmp/card-history.json next to config)")
    args = parser.parse_args()
    try:
        config_path = args.config.resolve()
        history_path = args.history.resolve() if args.history is not None else config_path.parent / "tmp" / "card-history.json"
        with open(config_path, encoding="utf-8") as stream:
            config = json.load(stream)
        httpd = create_server(config, args.port, history_path=history_path)
    except (OSError, ValueError) as error:
        message = type(error).__name__ if isinstance(error, OSError) else str(error)
        print(f"起動できません: {message}", file=sys.stderr)
        return 1
    print(f"http://127.0.0.1:{httpd.server_port} で起動しました。終了は Ctrl+C。", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
