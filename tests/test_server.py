import contextlib
from datetime import date, datetime, timedelta, timezone
import hashlib
import http.client
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/live-board/scripts"))


class MainHistoryPathTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "tmp")
        self.addCleanup(temporary.cleanup)
        self.config_path = Path(temporary.name) / "config" / "projects.json"
        self.config_path.parent.mkdir()
        self.config_path.write_text('{"projects": []}\n', encoding="utf-8", newline="\n")

    def run_main(self, *extra_args):
        import server
        relative_config = os.path.relpath(self.config_path, Path.cwd())
        with mock.patch.object(sys, "argv", ["server.py", "--config", relative_config, *extra_args]), \
             mock.patch.object(sys, "stdin", io.StringIO()), \
             mock.patch.object(sys, "stdout", io.StringIO()), \
             mock.patch.object(sys, "stderr", io.StringIO()), \
             mock.patch.object(server, "create_server") as create:
            self.assertEqual(server.main(), 0)
            create.return_value.serve_forever.assert_called_once_with()
            create.return_value.server_close.assert_called_once_with()
        return create

    def test_history_defaults_to_resolved_config_directory(self):
        create = self.run_main()
        create.assert_called_once_with(
            {"projects": []}, 8795,
            history_path=self.config_path.resolve().parent / "tmp" / "card-history.json",
        )

    def test_history_override(self):
        override = self.config_path.parent.parent / "custom-history.json"
        for value in (str(override.resolve()), os.path.relpath(override, Path.cwd())):
            with self.subTest(history=value):
                create = self.run_main("--history", value)
                create.assert_called_once_with({"projects": []}, 8795, history_path=override.resolve())


class DescriptionProgressTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / ".git").mkdir()
        self.source = self.root / "wbs.json"
        self.project = {"id": "test", "name": "試験", "adapter": "wbs-cards", "source": str(self.source)}
        self.data = {"schema": 1, "lane": "test", "repo": "test", "handoff": "", "generated_at": "",
                     "done_hidden": 0, "cards": [{"code": "A", "section": "", "text": "試験", "note": "",
                     "status": "p-run", "label": "", "ball": False}]}
        self.write()

    def write(self):
        previous = self.source.stat().st_mtime if self.source.exists() else 1_700_000_000
        with self.source.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(self.data, stream, ensure_ascii=False)
        os.utime(self.source, (previous + 60, previous + 60))

    def board(self, history_path=None):
        from server import Board
        return Board({"projects": [self.project]}, history_path=history_path)

    def tasks(self, state):
        return state.snapshot()["last_good"]["rows"]["tasks"]

    def test_description_history_lifecycle_and_restart(self):
        history = self.root / "history.json"
        state = self.board(history).projects["test"]
        first = self.tasks(state)[0]
        self.assertTrue(first["description_baseline"])
        self.assertEqual(datetime.fromisoformat(first["description_changed_at"]).timestamp(), self.source.stat().st_mtime)
        persisted = history.read_bytes()
        self.write()
        state.check()
        self.assertEqual(self.tasks(state)[0], first)
        self.assertEqual(history.read_bytes(), persisted)
        self.data["cards"][0]["text"] = "表示する変更"
        self.write()
        state.check()
        changed = self.tasks(state)[0]
        self.assertFalse(changed["description_baseline"])
        self.assertNotEqual(changed["description_changed_at"], first["description_changed_at"])
        self.data["cards"].append({**self.data["cards"][0], "code": "B"})
        self.write()
        state.check()
        self.assertFalse(self.tasks(state)[1]["description_baseline"])
        restarted = self.board(history).projects["test"]
        self.assertEqual(self.tasks(restarted), self.tasks(state))
        self.data["cards"] = self.data["cards"][1:]
        self.write()
        restarted.check()
        self.assertNotIn("A", json.loads(history.read_text(encoding="utf-8"))["test"])
        self.data["cards"].append({**self.data["cards"][0], "code": "C"})
        self.write()
        self.assertFalse(self.tasks(self.board(history).projects["test"])[1]["description_baseline"])

    def test_corrupt_unreadable_history_and_write_failure(self):
        history = self.root / "history.json"
        for content in ("{broken", '{"test":{"A":{}}}', "[]", json.dumps({"test": {"A": {
            "hash": "old", "visible_hash": None, "baseline": False, "changed_at": "2026-11-04T00:00:00Z",
        }}})):
            history.write_text(content, encoding="utf-8", newline="\n")
            self.assertTrue(self.tasks(self.board(history).projects["test"])[0]["description_baseline"])
        with mock.patch("pathlib.Path.read_text", side_effect=PermissionError):
            # Source adapters use open(), so only history reading is denied.
            self.assertTrue(self.tasks(self.board(history).projects["test"])[0]["description_baseline"])
        history.unlink()
        with mock.patch("server.os.replace", side_effect=PermissionError):
            state = self.board(history).projects["test"]
            self.assertIsNone(state.snapshot()["error"])
            self.assertTrue(any("記述履歴" in warning for warning in state.summary()["warnings"]))
            self.assertTrue(state.snapshot()["history_warning"])
        state.check()
        self.assertFalse(state.snapshot()["history_warning"])
        self.assertTrue(history.exists())

    def test_unrepresentable_description_time_is_not_persisted(self):
        import server
        for existing in (False, True):
            for error in (OSError, OverflowError, ValueError, TypeError):
                with self.subTest(existing=existing, error=error.__name__):
                    history = self.root / f"history-{existing}-{error.__name__}.json"
                    state = server.ProjectState(self.project, server.CardHistory(history))
                    if existing:
                        state.check()
                        self.data["cards"][0]["text"] += "変更"
                        self.write()
                    with mock.patch("server.datetime", wraps=datetime) as dates:
                        dates.fromtimestamp.side_effect = error("Unrepresentable file date")
                        state.check()
                    self.assertIsNone(state.snapshot()["error"])
                    task = self.tasks(state)[0]
                    self.assertIsNone(task["description_changed_at"])
                    self.assertTrue(task["description_baseline"])
                    self.assertNotIn("A", json.loads(history.read_text(encoding="utf-8"))["test"])
                    self.assertNotIn("A", server.CardHistory(history).projects["test"])
                    self.write()
                    state.check()
                    self.assertIsNotNone(self.tasks(state)[0]["description_changed_at"])
                    self.assertFalse(self.tasks(state)[0]["description_baseline"])

    def test_snapshot_does_not_publish_source_fields(self):
        state = self.board().projects["test"]
        self.assertIsNone(state.snapshot()["error"])
        for task in self.tasks(state):
            self.assertNotIn("source_fields", task)

    def test_wbs_description_tracks_only_visible_fields(self):
        state = self.board().projects["test"]
        first = self.tasks(state)[0]
        card = self.data["cards"][0]
        for key, value in (("note", "見えない補足"), ("progress", {"count_files": "results/*.json"}),
                           ("extra", {"kept": True})):
            with self.subTest(hidden=key):
                card[key] = value
                self.write()
                state.check()
                current = self.tasks(state)[0]
                self.assertEqual(current["description_changed_at"], first["description_changed_at"])
                self.assertTrue(current["description_baseline"])
        for key, value in (("text", "次の版を公開"), ("label", "v0.2.1"), ("status", "p-wait"),
                           ("section", "公開"), ("ball", True)):
            with self.subTest(visible=key):
                previous = self.tasks(state)[0]["description_changed_at"]
                card[key] = value
                self.write()
                state.check()
                current = self.tasks(state)[0]
                self.assertNotEqual(current["description_changed_at"], previous)
                self.assertFalse(current["description_baseline"])

    def test_wbs_description_tracks_displayed_text_not_markup(self):
        self.data["cards"][0]["text"] = "<b>Release</b>"
        self.write()
        state = self.board().projects["test"]
        first = self.tasks(state)[0]["description_changed_at"]
        self.data["cards"][0]["text"] = "<i>Release</i>"
        self.write()
        state.check()
        self.assertEqual(self.tasks(state)[0]["description_changed_at"], first)
        self.data["cards"][0]["text"] = "<i>Release now</i>"
        self.write()
        state.check()
        self.assertNotEqual(self.tasks(state)[0]["description_changed_at"], first)

    def test_old_history_migrates_without_refresh_and_reloads(self):
        from server import CardHistory
        history = self.root / "history.json"
        changed_at = "2026-10-01T00:00:00+00:00"
        for baseline in (False, True):
            with self.subTest(baseline=baseline):
                history.write_text(json.dumps({"test": {"A": {
                    "hash": "old whole-card hash", "changed_at": changed_at, "baseline": baseline,
                }}}), encoding="utf-8", newline="\n")
                state = self.board(history).projects["test"]
                migrated = self.tasks(state)[0]
                self.assertEqual(migrated["description_changed_at"], changed_at)
                self.assertTrue(migrated["description_baseline"])
                entry = CardHistory(history).projects["test"]["A"]
                self.assertIn("visible_hash", entry)
                self.assertNotIn("hash", entry)
                self.assertEqual(self.tasks(self.board(history).projects["test"])[0], migrated)
                self.data["cards"][0]["label"] += "版"
                self.write()
                state.check()
                changed = self.tasks(state)[0]
                self.assertNotEqual(changed["description_changed_at"], changed_at)
                self.assertFalse(changed["description_baseline"])
                self.assertEqual(self.tasks(self.board(history).projects["test"])[0], changed)

    def test_yaml_date_hidden_fields_do_not_refresh_and_are_not_published(self):
        import server
        import yaml
        self.project.update(adapter="winsmux-backlog", build_py=str(self.root / "build.py"))
        shutil.copyfile(ROOT / "tests/fixtures/backlog_build.py", self.project["build_py"])
        self.data = yaml.safe_load((ROOT / "tests/fixtures/backlog.yaml").read_text(encoding="utf-8"))
        self.data["tasks"][0]["reviewed_on"] = date(2026, 11, 4)
        def save():
            previous = self.source.stat().st_mtime
            self.source.write_text(yaml.safe_dump(self.data), encoding="utf-8", newline="\n")
            os.utime(self.source, (previous + 60, previous + 60))
        save()
        state = server.ProjectState(self.project)
        state.check()
        first = self.tasks(state)[0]
        self.assertNotIn("source_fields", first)
        self.data["tasks"][0]["reviewed_on"] = date(2026, 11, 5)
        save()
        state.check()
        self.assertIsNone(state.snapshot()["error"])
        self.assertEqual(self.tasks(state)[0], first)
        self.data["tasks"][0]["title"] = date(2026, 11, 5)
        save()
        state.check()
        self.assertIsNotNone(state.snapshot()["error"])

    def progress_state(self, pattern="results/*.json"):
        from server import ProjectState
        self.data["cards"][0]["progress"] = {"count_files": pattern, "total": 90, "label": "結果ファイル"}
        self.write()
        state = ProjectState(self.project)
        state.check()
        return state

    def test_progress_source_reload_bypasses_throttle(self):
        from server import ProjectState
        directory = self.root / "results"
        directory.mkdir()
        (directory / "one.json").write_text("{}", encoding="utf-8", newline="\n")
        card = dict(self.data["cards"][0])
        declared = {**card, "progress": {"count_files": "results/*.json"}}
        cases = (
            ("invalid", [declared], [{**declared, "progress": {"count_files": "results/*.json", "total": 0}}], set()),
            ("removed declaration", [declared], [card], set()),
            ("added declaration", [card], [declared], {"A"}),
            ("removed card", [declared], [], set()),
            ("added card", [declared], [declared, {**declared, "code": "B"}], {"A", "B"}),
            ("changed declaration", [declared], [{**card, "progress": {"count_files": "results/*.txt"}}], {"A"}),
        )
        for name, before, after, expected in cases:
            with self.subTest(name=name), mock.patch("server.time.monotonic", return_value=100):
                self.data["cards"] = before
                self.write()
                state = ProjectState(self.project)
                state.check()
                revision = state.progress_revision
                self.data["cards"] = after
                self.write()
                self.assertTrue(state.check())
                snapshot = state.snapshot()
                self.assertIsNone(snapshot["error"])
                self.assertEqual(set(snapshot["progress"]["items"]), expected)
                self.assertEqual(snapshot["progress_revision"], revision + 1)
                if name == "invalid":
                    self.assertTrue(any("progress" in warning for warning in snapshot["last_good"]["warnings"]))
                if name == "changed declaration":
                    self.assertEqual(snapshot["progress"]["items"]["A"]["count"], 0)

    @unittest.skipUnless(os.name == "nt", "Windowsの大小文字規則を検証します")
    def test_progress_matches_native_windows_glob_case(self):
        directory = self.root / "results"
        directory.mkdir()
        (directory / "001.JSON").write_text("{}", encoding="utf-8", newline="\n")
        expected = sum(path.is_file() for path in self.root.glob("results/*.json"))
        self.assertEqual(expected, 1)
        state = self.progress_state()
        self.assertEqual(state.snapshot()["progress"]["items"]["A"]["count"], expected)

    def test_history_hash_failure_keeps_loading_without_stale_age(self):
        import server
        dumps = json.dumps
        for existing in (False, True):
            for error in (TypeError, ValueError, RuntimeError, RecursionError, UnicodeError):
                with self.subTest(existing=existing, error=error.__name__):
                    history = self.root / f"hash-{existing}-{error.__name__}.json"
                    state = server.ProjectState(self.project, server.CardHistory(history))
                    if existing:
                        state.check()
                        self.data["cards"][0]["text"] += "変更"
                        self.write()
                    def fail_hash(value, **kwargs):
                        if kwargs.get("sort_keys"):
                            raise error("Cannot hash task")
                        return dumps(value, **kwargs)
                    with mock.patch("server.json.dumps", side_effect=fail_hash):
                        state.check()
                    self.assertIsNone(state.snapshot()["error"])
                    task = self.tasks(state)[0]
                    self.assertIsNone(task["description_changed_at"])
                    self.assertTrue(task["description_baseline"])
                    self.assertNotIn("source_fields", task)
                    self.assertNotIn("A", json.loads(history.read_text(encoding="utf-8"))["test"])
                    self.write()
                    state.check()
                    self.assertIsNotNone(self.tasks(state)[0]["description_changed_at"])
                    self.assertFalse(self.tasks(state)[0]["description_baseline"])

    def test_progress_count_revision_and_throttle(self):
        directory = self.root / "results"
        directory.mkdir()
        (directory / "a.json").write_text("private", encoding="utf-8", newline="\n")
        (directory / "directory.json").mkdir()
        state = self.progress_state()
        before = state.snapshot()
        self.assertEqual(before["progress"]["items"]["A"]["count"], 1)
        self.assertNotIn("a.json", json.dumps(before["progress"]))
        (directory / "b.json").write_text("private", encoding="utf-8", newline="\n")
        state.check()
        self.assertEqual(state.snapshot()["progress"], before["progress"])
        with mock.patch("server.time.monotonic", return_value=time.monotonic() + 6):
            self.assertTrue(state.check())
        after = state.snapshot()
        self.assertEqual(after["progress"]["items"]["A"]["count"], 2)
        self.assertGreater(after["progress_revision"], before["progress_revision"])
        self.assertEqual(after["last_good"]["loaded_at"], before["last_good"]["loaded_at"])
        self.assertEqual(state.source_revision, 1)
        self.assertEqual(state.summary()["progress"], {"count": 1, "errors": []})

    def test_progress_sse_health_summary_and_no_source_writes_or_pr_refetch(self):
        import server
        (self.root / "results").mkdir()
        self.data["cards"][0]["progress"] = {"count_files": "results/*.json"}
        self.write()
        source_bytes = self.source.read_bytes()
        with mock.patch.object(server, "PROGRESS_CHECK_SECONDS", 0):
            httpd = server.create_server({"projects": [self.project]}, port=0)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
            try:
                state = httpd.board.projects["test"]
                loaded_at = state.snapshot()["last_good"]["loaded_at"]
                connection.request("GET", "/events")
                response = connection.getresponse()
                self.assertEqual(response.readline(), b": connected\n")
                self.assertEqual(response.readline(), b"\n")
                with mock.patch.object(httpd.board.pr_wakeup, "set") as wakeup:
                    (self.root / "results" / "private.json").write_text("private", encoding="utf-8", newline="\n")
                    self.assertEqual(response.readline(), b"event: project\n")
                    self.assertEqual(response.readline(), b'data: "test"\n')
                    wakeup.assert_not_called()
                self.assertEqual(state.snapshot()["last_good"]["loaded_at"], loaded_at)
                for endpoint in ("/api/summary", "/healthz"):
                    client = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
                    try:
                        client.request("GET", endpoint)
                        value = json.load(client.getresponse())
                        progress = value[0]["progress"] if endpoint == "/api/summary" else value["projects"]["test"]["progress"]
                        self.assertEqual(progress, {"count": 1, "errors": []})
                    finally:
                        client.close()
                self.assertEqual(self.source.read_bytes(), source_bytes)
            finally:
                connection.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join()

    def test_history_before_pr_enrichment_for_both_adapter_contracts(self):
        import server
        for adapter in ("wbs-cards", "winsmux-backlog"):
            with self.subTest(adapter=adapter):
                task = {"id": "A", "title": "試験", "status": "doing", "pr": [1], "extra": {"note": "元"}}
                def read_adapter(config):
                    return {"meta": {"updated": ""}, "rows": {"tasks": [dict(task)], "phases": [], "versions": []}}
                with mock.patch.dict(server.ADAPTERS, {adapter: read_adapter}):
                    state = server.ProjectState({**self.project, "adapter": adapter})
                    state.check()
                    first = self.tasks(state)[0]
                    self.assertTrue(first["description_baseline"])
                    state.apply_pr_results({1: {"data": None, "error": {"message": "未取得", "at": None}, "fetched_at": None, "checked_at": None}}, {1})
                    self.write()
                    state.check()
                    self.assertEqual(self.tasks(state)[0]["description_changed_at"], first["description_changed_at"])
                    task["extra"] = {"note": "変更"}
                    self.write()
                    state.check()
                    self.assertFalse(self.tasks(state)[0]["description_baseline"])

    def test_progress_empty_missing_rejected_and_read_failure(self):
        (self.root / "results").mkdir()
        self.assertEqual(self.progress_state().snapshot()["progress"]["items"]["A"]["count"], 0)
        for pattern in ("missing/*.json", "../*.json", "results/**/*.json", "C:results/*.json", "/tmp/*", "..\\*.json", "results/C:foo/*.json"):
            with self.subTest(pattern=pattern):
                progress = self.progress_state(pattern).snapshot()["progress"]
                self.assertEqual(progress["items"], {})
                self.assertEqual(progress["errors"][0]["card"], "A")
                if pattern.startswith("missing"):
                    self.assertEqual(progress["errors"][0]["message"], "取得元が見つかりません")
        with mock.patch("server.os.scandir", side_effect=PermissionError):
            self.assertTrue(self.progress_state().snapshot()["progress"]["errors"])
        (self.root / ".git").rmdir()
        # The test fixture is nested in this repository; stop root discovery explicitly.
        with mock.patch("server.progress_root", return_value=None):
            self.assertTrue(self.progress_state().snapshot()["progress"]["errors"])

    def test_progress_symlink_escape(self):
        directory = self.root / "results"
        directory.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.json").write_text("private", encoding="utf-8", newline="\n")
        try:
            (directory / "link").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("この環境ではシンボリックリンクを作れません")
        # Narrow the discovered project root so the sibling directory is outside it.
        (directory / ".git").mkdir()
        self.source = directory / "wbs.json"
        self.project["source"] = str(self.source)
        self.write()
        state = self.progress_state("link/*.json")
        progress = state.snapshot()["progress"]
        self.assertEqual(progress["items"], {})
        self.assertEqual(progress["errors"], [{"card": "A", "message": "取得元がルートの外にあります"}])


class BoardTests(unittest.TestCase):
    def setUp(self):
        global yaml
        import yaml

        (ROOT / "tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = {
            "id": "sample", "name": "試験用", "adapter": "winsmux-backlog",
            "source": str(self.root / "backlog.yaml"),
            "build_py": str(self.root / "build.py"),
        }
        for key, name in (("source", "backlog.yaml"), ("build_py", "backlog_build.py")):
            shutil.copyfile(ROOT / "tests" / "fixtures" / name, self.project[key])
        self.source = Path(self.project["source"])

    def write_source(self, content):
        previous = self.source.stat().st_mtime_ns if self.source.exists() else 0
        with open(self.source, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.utime(self.source, ns=(previous + 1_000_000_000, previous + 1_000_000_000))

    def load_module(self, name):
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as error:
            if error.name != name:
                raise
            self.fail(f"The required {name} implementation is missing")

    def test_adapter_load(self):
        adapters = self.load_module("adapters")
        result = adapters.ADAPTERS["winsmux-backlog"](self.project)
        original = yaml.safe_load(self.source.read_text(encoding="utf-8"))
        self.assertEqual(len(result["rows"]["tasks"]), len(original["tasks"]))
        self.assertEqual(result["meta"]["product"], original["meta"]["product"])
        self.assertEqual(result["meta"]["current_phase"], original["meta"]["current_phase"])
        self.assertFalse((self.root / "__pycache__").exists())

    def test_yaml_mixed_mapping_keys_have_stable_description_history(self):
        from server import ProjectState
        data = yaml.safe_load(self.source.read_text(encoding="utf-8"))
        extra = {1: "number", "1": "text", "label": "補足"}
        data["tasks"][0]["extra"] = {"nested": [extra]}
        self.write_source(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        state = ProjectState(self.project)
        state.check()
        self.assertIsNone(state.snapshot()["error"])
        first = state.snapshot()["last_good"]["rows"]["tasks"][0]
        self.assertIsNotNone(first["description_changed_at"])
        self.assertTrue(first["description_baseline"])
        data["tasks"][0]["extra"]["nested"][0] = dict(reversed(list(extra.items())))
        self.write_source(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        state.check()
        self.assertIsNone(state.snapshot()["error"])
        self.assertEqual(state.snapshot()["last_good"]["rows"]["tasks"][0], first)
        data["tasks"][0]["extra"]["nested"][0][1] = "changed number"
        self.write_source(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        state.check()
        self.assertIsNone(state.snapshot()["error"])
        changed = state.snapshot()["last_good"]["rows"]["tasks"][0]
        self.assertEqual(changed["description_changed_at"], first["description_changed_at"])
        self.assertTrue(changed["description_baseline"])
        self.assertNotIn("source_fields", changed)

    def test_yaml_description_tracks_displayed_rows_and_linked_pr_numbers(self):
        from server import ProjectState
        data = yaml.safe_load(self.source.read_text(encoding="utf-8"))
        data["tasks"].append({"id": "T2", "title": "依存先", "status": "todo"})
        def save():
            self.write_source(yaml.safe_dump(data, allow_unicode=True))
        save()
        state = ProjectState(self.project)
        state.check()
        first = state.snapshot()["last_good"]["rows"]["tasks"][0]
        for key, value in (("note", "非表示の補足"), ("done_when", "詳細だけの完了条件")):
            with self.subTest(hidden=key):
                data["tasks"][0][key] = value
                save()
                state.check()
                current = state.snapshot()["last_good"]["rows"]["tasks"][0]
                self.assertEqual(current["description_changed_at"], first["description_changed_at"])
                self.assertTrue(current["description_baseline"])
        for key, value in (("title", "更新した題"), ("status", "blocked"), ("phase", "P2"),
                           ("version", "V2"), ("deps", ["T2"]), ("pr", [6, 7]), ("label", "表示ラベル")):
            with self.subTest(visible=key):
                previous = state.snapshot()["last_good"]["rows"]["tasks"][0]["description_changed_at"]
                data["tasks"][0][key] = value
                save()
                state.check()
                current = state.snapshot()["last_good"]["rows"]["tasks"][0]
                self.assertNotEqual(current["description_changed_at"], previous)
                self.assertFalse(current["description_baseline"])
                self.assertNotIn("source_fields", current)
        previous = current["description_changed_at"]
        data["tasks"][1]["status"] = "done"
        save()
        state.check()
        current = state.snapshot()["last_good"]["rows"]["tasks"][0]
        self.assertEqual(current["deps"], "T2")
        self.assertEqual(current["blocked_by"], "")
        self.assertNotEqual(current["description_changed_at"], previous)

    def test_invalid_source_preserves_last_good_and_recovers(self):
        server = self.load_module("server")
        project = server.ProjectState(self.project)
        self.assertTrue(project.check())
        good = project.snapshot()["last_good"]
        valid = self.source.read_text(encoding="utf-8")
        for broken in ("tasks: [\n", "meta: {}\nphases: []\nversions: []\ntasks: []\n"):
            with self.subTest(broken=broken):
                self.write_source(broken)
                self.assertTrue(project.check())
                state = project.snapshot()
                self.assertEqual(state["last_good"], good)
                self.assertTrue(state["error"]["message"])
                self.assertTrue(state["error"]["at"])
                self.assertNotEqual(state["source_mtime"], good["source_mtime"])
                self.assertGreaterEqual(state["checked_at"], good["loaded_at"])
        self.write_source(valid)
        self.assertTrue(project.check())
        self.assertIsNone(project.snapshot()["error"])
        self.assertEqual(project.snapshot()["last_good"]["rows"], good["rows"])

    def test_missing_source_preserves_last_good(self):
        server = self.load_module("server")
        project = server.ProjectState(self.project)
        project.check()
        good = project.snapshot()["last_good"]
        self.source.unlink()
        self.assertTrue(project.check())
        self.assertEqual(project.snapshot()["last_good"], good)
        self.assertIsNone(project.snapshot()["source_mtime"])
        self.assertIsNotNone(project.snapshot()["error"])

    def test_initial_failure_has_no_last_good(self):
        server = self.load_module("server")
        self.write_source("tasks: [\n")
        project = server.ProjectState(self.project)
        project.check()
        self.assertIsNone(project.snapshot()["last_good"])
        self.assertIsNotNone(project.snapshot()["error"])

    def test_unencodable_source_preserves_last_good_and_api_recovers(self):
        with self.running_server():
            project = self.httpd.board.projects["sample"]
            good = self.get_json("/api/projects/sample")["last_good"]
            valid = self.source.read_text(encoding="utf-8")
            for field in ("title", "done_when"):
                with self.subTest(field=field):
                    broken = yaml.safe_load(valid)
                    broken["tasks"][0][field] = "\ud800"
                    self.write_source(yaml.safe_dump(broken, allow_unicode=False))
                    self.assertTrue(project.check())
                    snapshot = project.snapshot()
                    self.assertTrue(snapshot["last_good"] == good)
                    self.assertIsNotNone(snapshot["error"])
                    self.assertTrue(self.get_json("/api/projects/sample")["last_good"] == good)
                    self.assertIsNotNone(self.get_json("/healthz")["projects"]["sample"]["error"])
                    self.write_source(valid)
                    self.assertTrue(project.check())
                    good = project.snapshot()["last_good"]
                    self.assertIsNone(self.get_json("/api/projects/sample")["error"])

    def test_initial_serialization_failure_and_unencodable_error_are_safe(self):
        server = self.load_module("server")
        meta = {"product": "Fixture", "updated": "2026-10-31", "current_phase": ""}
        cases = [
            ({"meta": meta, "rows": {"phases": [], "versions": [],
              "tasks": [{"id": "T1", "title": "\ud800", "status": "todo"}]}}, "surrogates not allowed"),
            ({"meta": meta, "rows": {"phases": [], "versions": [],
              "tasks": [{"id": "T1", "title": "Fixture", "status": "todo", "value": float("nan")}]}},
             "Out of range float values"),
            (ValueError("invalid title: \ud800"), "invalid title"),
        ]
        for result, expected_error in cases:
            with self.subTest(result=ascii(result)):
                project = server.ProjectState(self.project)
                adapter = mock.Mock(side_effect=result) if isinstance(result, Exception) else mock.Mock(return_value=result)
                with mock.patch.dict(server.ADAPTERS, {"winsmux-backlog": adapter}):
                    self.assertTrue(project.check())
                snapshot = project.snapshot()
                self.assertIsNone(snapshot["last_good"])
                self.assertIsNotNone(snapshot["error"])
                self.assertIn(expected_error, snapshot["error"]["message"])
                json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode("utf-8")

    def test_replacement_with_same_mtime_and_size_is_detected(self):
        server = self.load_module("server")
        data = yaml.safe_load(self.source.read_text(encoding="utf-8"))
        data["tasks"][0]["title"] = "OLD"
        self.write_source(yaml.safe_dump(data, allow_unicode=False))
        project = server.ProjectState(self.project)
        project.check()
        before = self.source.stat()
        replacement = self.root / "replacement.yaml"
        replacement.write_bytes(self.source.read_bytes().replace(b"title: OLD", b"title: NEW"))
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, self.source)
        after = self.source.stat()
        self.assertEqual((before.st_mtime_ns, before.st_size), (after.st_mtime_ns, after.st_size))
        self.assertTrue(project.check())
        self.assertEqual(project.snapshot()["last_good"]["rows"]["tasks"][0]["title"], "NEW")

    def test_content_backstop_detects_change_at_30_seconds_with_frozen_metadata(self):
        server = self.load_module("server")
        data = yaml.safe_load(self.source.read_text(encoding="utf-8"))
        data["tasks"][0]["title"] = "OLD"
        self.write_source(yaml.safe_dump(data, allow_unicode=False))
        project = server.ProjectState(self.project)
        with mock.patch("time.monotonic", return_value=100):
            project.check()
        before = self.source.stat()
        self.source.write_bytes(self.source.read_bytes().replace(b"title: OLD", b"title: NEW"))
        with mock.patch.object(Path, "stat", return_value=before):
            with mock.patch("time.monotonic", return_value=129.999):
                self.assertFalse(project.check())
                self.assertEqual(project.snapshot()["last_good"]["rows"]["tasks"][0]["title"], "OLD")
            with mock.patch("time.monotonic", return_value=130):
                self.assertTrue(project.check())
                self.assertEqual(project.snapshot()["last_good"]["rows"]["tasks"][0]["title"], "NEW")
            good = project.snapshot()["last_good"]
            with mock.patch("time.monotonic", return_value=160):
                self.assertFalse(project.check())
                self.assertTrue(project.snapshot()["last_good"] == good)

    def test_content_backstop_preserves_last_good_on_failure_and_recovers(self):
        server = self.load_module("server")
        project = server.ProjectState(self.project)
        with mock.patch("time.monotonic", return_value=100):
            project.check()
        good = project.snapshot()["last_good"]
        before = self.source.stat()
        valid = self.source.read_bytes()
        self.source.write_bytes(b"\0" * len(valid))
        with mock.patch.object(Path, "stat", return_value=before):
            with mock.patch("time.monotonic", return_value=130):
                self.assertTrue(project.check())
                self.assertIsNotNone(project.snapshot()["error"])
                self.assertTrue(project.snapshot()["last_good"] == good)
            self.source.write_bytes(valid)
            with mock.patch("time.monotonic", return_value=131):
                self.assertTrue(project.check())
                self.assertIsNone(project.snapshot()["error"])

    def test_replacement_during_load_preserves_previous_snapshot(self):
        server = self.load_module("server")
        project = server.ProjectState(self.project)
        project.check()
        good = project.snapshot()["last_good"]
        self.write_source(self.source.read_text(encoding="utf-8") + "\n# reload\n")
        adapter = server.ADAPTERS["winsmux-backlog"]

        def replace_after_read(config):
            result = adapter(config)
            before = self.source.stat()
            replacement = self.root / "replacement.yaml"
            replacement.write_bytes(self.source.read_bytes())
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.replace(replacement, self.source)
            return result

        with mock.patch.dict(server.ADAPTERS, {"winsmux-backlog": replace_after_read}):
            self.assertTrue(project.check())
        self.assertIsNotNone(project.snapshot()["error"])
        self.assertTrue(project.snapshot()["last_good"] == good)
        self.assertTrue(project.check())
        self.assertIsNone(project.snapshot()["error"])

    @contextlib.contextmanager
    def running_server(self, projects=None):
        server = self.load_module("server")
        app_root = self.root / "app"
        (app_root / "static").mkdir(parents=True)
        for name in ("server.py", "static/index.html", "static/app.js", "static/style.css"):
            with open(app_root / name, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(f"fixture for {name}\n")
        httpd = server.create_server({"projects": projects or [self.project]}, port=0, root=app_root)
        thread = threading.Thread(target=httpd.serve_forever)
        thread.start()
        self.httpd = httpd
        self.app_root = app_root
        try:
            yield
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(httpd.board.watcher.is_alive())

    def request_with_headers(self, path, headers, expected):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            connection.putrequest("GET", path, skip_host=True)
            for name, value in headers:
                connection.putheader(name, value)
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, expected)
            if path == "/events" and expected == 200:
                self.assertEqual(response.readline(), b": connected\n")
            else:
                body = response.read()
                if expected == 403:
                    self.assertEqual(body, b"Forbidden\n")
                return body
        finally:
            connection.close()

    def test_allowed_request_authorities(self):
        with self.running_server():
            port = self.httpd.server_port
            for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
                for origin in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                    for path in ("/api/projects", "/healthz", "/events", "/"):
                        with self.subTest(host=host, origin=origin, path=path):
                            headers = [("Host", host)] + ([("Origin", origin)] if origin is not None else [])
                            self.request_with_headers(path, headers, 200)

    def test_foreign_host_denied_before_routing(self):
        with self.running_server():
            port = self.httpd.server_port
            for host in (f"attacker.example:{port}", "127.0.0.1:0", "localhost", f"localhost:{port}.attacker.example"):
                for path in ("/api/projects", "/healthz", "/events", "/"):
                    with self.subTest(host=host, path=path):
                        self.request_with_headers(path, [("Host", host)], 403)

    def test_foreign_origin_denied_before_routing(self):
        with self.running_server():
            port = self.httpd.server_port
            for origin in ("http://attacker.example", "null", "", f"https://localhost:{port}", "http://127.0.0.1:0"):
                for path in ("/api/projects", "/healthz", "/events", "/"):
                    with self.subTest(origin=origin, path=path):
                        self.request_with_headers(path, [("Host", f"localhost:{port}"), ("Origin", origin)], 403)

    def test_missing_or_duplicate_authority_headers_denied(self):
        with self.running_server():
            host = f"127.0.0.1:{self.httpd.server_port}"
            for headers in ([],
                            [("Host", host), ("Origin", f"http://{host}"), ("Origin", "http://attacker.example")]):
                with self.subTest(headers=headers):
                    self.request_with_headers("/api/projects", headers, 403)
            server = self.load_module("server")
            handler = server.Handler.__new__(server.Handler)
            handler.server = self.httpd
            handler.headers = http.client.HTTPMessage()
            handler.headers.add_header("Host", host)
            handler.headers.add_header("Host", "attacker.example")
            with mock.patch.object(handler, "respond") as respond:
                handler.do_GET()
            respond.assert_called_once_with(b"Forbidden\n", "text/plain; charset=utf-8", 403)

    def get_json(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            return json.loads(response.read())
        finally:
            connection.close()

    def wbs_project(self):
        source = self.root / "wbs.json"
        data = {
            "schema": 1, "lane": "WBSレーン", "repo": "sample", "handoff": "HANDOFF.md",
            "generated_at": "2026-10-31T00:00:00Z", "done_hidden": 7,
            "cards": [{"code": "A1", "section": "作業", "text": "判断待ち", "note": "補足",
                       "status": "p-owner", "label": "", "ball": True}],
        }
        with open(source, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.write("\n")
        return {"id": "wbs", "name": "WBS", "adapter": "wbs-cards", "source": str(source)}

    def activity_project(self):
        return {**self.wbs_project(), "activity_dir": str(self.root / "activity")}

    def write_activity(self, project, card="A1", **changes):
        directory = Path(project["activity_dir"])
        directory.mkdir(exist_ok=True)
        at = datetime.now(timezone.utc).isoformat()
        data = {"card": card, "step": "試験中", "detail": "Python", "started_at": at, "heartbeat_at": at, **changes}
        temporary = directory / f"{card}.tmp"
        with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=True)
            stream.write("\n")
        target = directory / f"{card}.json"
        temporary.replace(target)
        return target

    def test_activity_present_absent_stale_and_removal(self):
        server = self.load_module("server")
        config = self.activity_project()
        project = server.ProjectState(config)
        project.check()
        good = project.snapshot()["last_good"]
        empty = {"count": 0, "items": [], "unmatched": [], "errors": []}
        self.assertEqual(project.snapshot().get("activity"), empty)
        Path(config["activity_dir"]).mkdir()
        self.assertFalse(project.check())
        for heartbeat in (datetime.now(timezone.utc), datetime.now(timezone.utc) - timedelta(minutes=11)):
            with self.subTest(heartbeat=heartbeat):
                path = self.write_activity(config, heartbeat_at=heartbeat.isoformat())
                self.assertTrue(project.check())
                state = project.snapshot()
                self.assertEqual(state["last_good"], good)
                self.assertIsNone(state["error"])
                self.assertEqual(state["activity"]["count"], 1)
                self.assertEqual(state["activity"]["errors"], [])
                self.assertEqual(state["activity"]["unmatched"], [])
                activity = state["activity"]["items"][0]
                self.assertEqual((activity["file"], activity["card"], activity["step"], activity["detail"]),
                                 ("A1.json", "A1", "試験中", "Python"))
                self.assertEqual(datetime.fromisoformat(activity["heartbeat_at"]), heartbeat)
                revision = state["activity_revision"]
                self.assertFalse(project.check())
                self.assertEqual(project.snapshot()["activity_revision"], revision)
        path.unlink()
        self.assertTrue(project.check())
        self.assertEqual(project.snapshot()["activity"], empty)
        Path(config["activity_dir"]).rmdir()
        self.assertFalse(project.check())
        del config["activity_dir"]
        unconfigured = server.ProjectState(config)
        unconfigured.check()
        self.assertEqual(unconfigured.snapshot()["activity"], empty)

    def test_activity_broken_files_are_isolated_and_recover(self):
        server = self.load_module("server")
        config = self.activity_project()
        project = server.ProjectState(config)
        path = self.write_activity(config)
        valid = json.loads(path.read_text(encoding="utf-8"))
        project.check()
        good = project.snapshot()["last_good"]
        cases = ["{broken", "[]", "[" * 2000 + "]" * 2000]
        cases += [json.dumps({key: value for key, value in valid.items() if key != missing})
                  for missing in ("card", "step", "heartbeat_at")]
        cases += [json.dumps({**valid, **change}) for change in (
            {"card": "other"}, {"card": 1}, {"step": " "}, {"step": []},
            {"heartbeat_at": "invalid"}, {"heartbeat_at": None}, {"heartbeat_at": "2026-11-02"},
            {"detail": 12}, {"started_at": "invalid"}, {"step": "\ud800"},
        )]
        for broken in cases:
            with self.subTest(broken=ascii(broken)):
                with open(path, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(broken)
                project.check()
                state = project.snapshot()
                self.assertEqual(state["last_good"], good)
                self.assertIsNone(state["error"])
                self.assertEqual(state["activity"]["items"], [])
                self.assertEqual(state["activity"]["count"], 0)
                self.assertEqual(state["activity"]["errors"][0]["file"], "A1.json")
                self.assertTrue(state["activity"]["errors"][0]["message"])
                self.write_activity(config)
                self.assertTrue(project.check())
                self.assertEqual(project.snapshot()["activity"]["errors"], [])
                self.assertEqual(project.snapshot()["activity"]["count"], 1)
        path.write_bytes(b"\xff")
        self.assertTrue(project.check())
        self.assertEqual(project.snapshot()["activity"]["errors"][0]["file"], "A1.json")
        self.write_activity(config, card="OTHER")
        project.check()
        self.assertEqual(project.snapshot()["activity"]["count"], 1, "One broken file must not hide other files")
        self.assertEqual(project.snapshot()["activity"]["unmatched"][0]["card"], "OTHER")

    def test_activity_unmatched_rejoins_when_source_changes_and_is_independent_of_errors(self):
        server = self.load_module("server")
        config = self.activity_project()
        project = server.ProjectState(config)
        path = self.write_activity(config, card="OTHER")
        project.check()
        self.assertEqual(project.snapshot()["activity"]["unmatched"][0]["file"], "OTHER.json")
        source = Path(config["source"])
        data = json.loads(source.read_text(encoding="utf-8"))
        data["cards"].append({**data["cards"][0], "code": "OTHER"})
        with open(source, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream)
        self.assertTrue(project.check())
        self.assertEqual(project.snapshot()["activity"]["unmatched"], [])
        self.assertEqual(project.snapshot()["activity"]["items"][0]["card"], "OTHER")
        source.write_bytes(b"{broken")
        project.check()
        good = project.snapshot()["last_good"]
        path.unlink()
        self.assertTrue(project.check())
        self.assertEqual(project.snapshot()["activity"]["count"], 0)
        self.assertEqual(project.snapshot()["last_good"], good)
        self.assertIsNotNone(project.snapshot()["error"])

    def test_activity_joins_winsmux_task_ids_and_ignores_temporary_files(self):
        server = self.load_module("server")
        self.project["activity_dir"] = str(self.root / "activity")
        card = yaml.safe_load(self.source.read_text(encoding="utf-8"))["tasks"][0]["id"]
        self.write_activity(self.project, card=card)
        (Path(self.project["activity_dir"]) / "unfinished.tmp").write_bytes(b"{broken")
        project = server.ProjectState(self.project)
        project.check()
        self.assertEqual(project.snapshot()["activity"]["items"][0]["card"], card)
        self.assertEqual(project.snapshot()["activity"]["errors"], [])

    def test_activity_dir_config_requires_an_absolute_path(self):
        server = self.load_module("server")
        for invalid in (None, "", 1, [], {}, "relative/activity"):
            with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, "activity_dir"):
                server.project_configs({"projects": [{**self.project, "activity_dir": invalid}]})
        config = self.activity_project()
        self.assertEqual(server.project_configs({"projects": [config]}), [config])

    def test_activity_optional_fields_timezones_and_directory_error(self):
        server = self.load_module("server")
        config = self.activity_project()
        project = server.ProjectState(config)
        for heartbeat in ("2026-11-02T09:10:11+09:00", "2026-11-02T00:10:11Z", "2026-11-02T09:10:11"):
            with self.subTest(heartbeat=heartbeat):
                path = self.write_activity(config, heartbeat_at=heartbeat)
                data = json.loads(path.read_text(encoding="utf-8"))
                del data["detail"], data["started_at"]
                with open(path, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(data, stream)
                project.check()
                activity = project.snapshot()["activity"]
                self.assertEqual(activity["errors"], [])
                item = activity["items"][0]
                self.assertNotIn("detail", item)
                self.assertNotIn("started_at", item)
                expected = datetime.fromisoformat(heartbeat).astimezone(timezone.utc)
                self.assertEqual(datetime.fromisoformat(item["heartbeat_at"]), expected)
        path.unlink()
        directory = Path(config["activity_dir"])
        directory.rmdir()
        directory.write_bytes(b"not a directory")
        self.assertTrue(project.check())
        self.assertEqual(project.snapshot()["activity"]["errors"][0]["file"], "activity_dir")
        directory.unlink()
        self.assertTrue(project.check())
        self.assertEqual(project.snapshot()["activity"]["errors"], [])

    def test_activity_notifies_without_waking_pr_fetcher(self):
        server = self.load_module("server")
        config = self.activity_project()
        board = server.Board({"projects": [config]})
        board.pr_wakeup.clear()
        self.write_activity(config)
        with mock.patch.object(board.stopping, "wait", side_effect=[False, True]):
            board.watch()
        self.assertEqual(board.revisions["wbs"], 1)
        self.assertFalse(board.pr_wakeup.is_set())
        source = Path(config["source"])
        data = json.loads(source.read_text(encoding="utf-8"))
        data["done_hidden"] += 1
        with open(source, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream)
        with mock.patch.object(board.stopping, "wait", side_effect=[False, True]):
            board.watch()
        self.assertEqual(board.revisions["wbs"], 2)
        self.assertTrue(board.pr_wakeup.is_set())

    def test_activity_add_update_error_unmatched_and_removal_emit_sse(self):
        config = self.activity_project()
        with self.running_server([config]):
            connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
            connection.request("GET", "/events")
            response = connection.getresponse()
            try:
                self.assertEqual(response.readline(), b": connected\n")
                self.assertEqual(response.readline(), b"\n")
                good = self.get_json("/api/projects/wbs")["last_good"]

                def update(action):
                    started = time.monotonic()
                    action()
                    self.assertEqual(response.readline(), b"event: project\n")
                    self.assertEqual(response.readline(), b'data: "wbs"\n')
                    self.assertEqual(response.readline(), b"\n")
                    state = self.get_json("/api/projects/wbs")
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertEqual(state["last_good"], good)
                    return state["activity"]

                activity = update(lambda: self.write_activity(config))
                self.assertEqual(activity["count"], 1)
                health = self.get_json("/healthz")["projects"]["wbs"]["activity"]
                self.assertEqual((health["count"], health["errors"]), (1, []))
                path = Path(config["activity_dir"]) / "A1.json"
                activity = update(lambda: self.write_activity(config, step="次の処理", heartbeat_at="2026-01-01T00:00:00Z"))
                self.assertEqual(activity["items"][0]["step"], "次の処理")
                activity = update(lambda: path.write_bytes(b"{broken"))
                self.assertEqual(activity["count"], 0)
                self.assertEqual(self.get_json("/healthz")["projects"]["wbs"]["activity"]["errors"][0]["file"], "A1.json")
                activity = update(lambda: self.write_activity(config))
                self.assertEqual(activity["errors"], [])
                activity = update(lambda: self.write_activity(config, card="OTHER"))
                self.assertEqual(activity["unmatched"][0]["card"], "OTHER")
                self.assertEqual(self.get_json("/api/summary")[0]["activity"]["unmatched"][0]["file"], "OTHER.json")
                activity = update(path.unlink)
                self.assertEqual(activity["items"], [])
                update((Path(config["activity_dir"]) / "OTHER.json").unlink)
                self.assertEqual(self.get_json("/healthz")["projects"]["wbs"]["activity"]["count"], 0)
            finally:
                response.close()
                connection.close()

    def test_api_keeps_style_warnings_separate_for_both_adapters(self):
        wbs = self.wbs_project()
        source = Path(wbs["source"])
        data = json.loads(source.read_text(encoding="utf-8"))
        data["cards"][0]["text"] = "題" * 41
        source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8", newline="\n")
        backlog = yaml.safe_load(self.source.read_text(encoding="utf-8"))
        backlog["tasks"][0]["title"] = "題" * 41
        self.write_source(yaml.safe_dump(backlog, allow_unicode=True))
        with self.running_server([self.project, wbs]):
            summaries = self.get_json("/api/summary")
            for summary, count in zip(summaries, (1, 2)):
                good = self.get_json(f"/api/projects/{summary['id']}")["last_good"]
                self.assertEqual(summary["warnings"], [])
                self.assertEqual(good["warnings"], [])
                self.assertEqual(len(good["style_warnings"]), count)
                self.assertEqual(summary["style_warnings"], good["style_warnings"])
            data["cards"][0]["status"] = "future-status"
            source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8", newline="\n")
            self.httpd.board.projects["wbs"].check()
            mixed = self.get_json("/api/projects/wbs")["last_good"]
            self.assertEqual(len(mixed["warnings"]), 1)
            self.assertIn("future-status", mixed["warnings"][0])
            self.assertEqual(len(mixed["style_warnings"]), 2)
            source.write_text("{broken", encoding="utf-8", newline="\n")
            self.httpd.board.projects["wbs"].check()
            failed = self.get_json("/api/projects/wbs")
            self.assertIsNotNone(failed["error"])
            self.assertEqual(failed["last_good"], mixed)

    def test_summary_endpoint_all_projects_and_failure_recovery(self):
        wbs = self.wbs_project()
        missing = {**wbs, "id": "missing", "source": str(self.root / "missing.json")}
        with self.running_server([self.project, wbs, missing]):
            summaries = self.get_json("/api/summary")
            self.assertEqual([item["id"] for item in summaries], ["sample", "wbs", "missing"])
            winsmux, good, unavailable = summaries
            snapshot = self.get_json("/api/projects/sample")["last_good"]
            self.assertEqual(winsmux["total"], len(snapshot["rows"]["tasks"]))
            self.assertEqual(winsmux["current_phase"], snapshot["meta"]["current_phase"])
            self.assertEqual(good["counts"], {"todo": 0, "doing": 0, "blocked": 1, "done": 7})
            self.assertEqual(good["total"], 8)
            self.assertEqual(good["owner_wait"], 1)
            self.assertEqual(good["current_phase"], "")
            self.assertTrue(good["checked_at"])
            self.assertEqual(good["source_mtime"], good["last_good_mtime"])
            self.assertIsNone(unavailable["counts"])
            self.assertIsNone(unavailable["total"])
            self.assertTrue(unavailable["error"]["at"])
            self.assertTrue(all("rows" not in item and "last_good" not in item for item in summaries))
            source = Path(wbs["source"])
            valid = source.read_text(encoding="utf-8")
            with open(source, "w", encoding="utf-8", newline="\n") as stream:
                stream.write("{broken\n")
            self.httpd.board.projects["wbs"].check()
            failed = self.get_json("/api/summary")[1]
            for key in ("counts", "total", "done_hidden", "owner_wait", "loaded_at", "last_good_mtime"):
                self.assertEqual(failed[key], good[key])
            self.assertTrue(failed["error"]["message"])
            self.assertTrue(failed["error"]["at"])
            with open(source, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(valid)
            self.httpd.board.projects["wbs"].check()
            self.assertIsNone(self.get_json("/api/summary")[1]["error"])

    def test_wbs_update_emits_sse_and_updates_summary(self):
        wbs = self.wbs_project()
        with self.running_server([self.project, wbs]):
            connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
            self.addCleanup(connection.close)
            connection.request("GET", "/events")
            response = connection.getresponse()
            self.addCleanup(response.close)
            self.assertEqual(response.readline(), b": connected\n")
            self.assertEqual(response.readline(), b"\n")
            source = Path(wbs["source"])
            data = json.loads(source.read_text(encoding="utf-8"))
            data["done_hidden"] = 8
            with open(source, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.write("\n")
            self.assertEqual(response.readline(), b"event: project\n")
            self.assertEqual(json.loads(response.readline().decode().removeprefix("data: ")), "wbs")
            self.assertEqual(self.get_json("/api/summary")[1]["counts"]["done"], 8)

    def test_mtime_change_emits_sse_without_blocking_requests(self):
        with self.running_server():
            connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
            connection.request("GET", "/events")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream", response.getheader("Content-Type"))
            self.assertEqual(response.readline(), b": connected\n")
            self.assertEqual(response.readline(), b"\n")
            original = self.get_json("/api/projects/sample")["last_good"]
            started = time.monotonic()
            self.write_source(self.source.read_text(encoding="utf-8") + "\n# changed\n")
            self.assertEqual(response.readline(), b"event: project\n")
            self.assertEqual(json.loads(response.readline().decode().removeprefix("data: ")), "sample")
            self.assertLess(time.monotonic() - started, 5)
            state = self.get_json("/api/projects/sample")
            self.assertNotEqual(state["source_mtime"], original["source_mtime"])
            self.assertIsNone(state["error"])
            response.close()
            connection.close()
            self.assertTrue(self.get_json("/healthz")["fingerprint"])

    def test_healthz_fingerprint_matches_files_at_startup(self):
        with self.running_server():
            health = self.get_json("/healthz")
            digest = hashlib.sha256()
            files = {}
            paths = [self.app_root / "server.py", *sorted((self.app_root / "static").rglob("*"))]
            for path in sorted(p for p in paths if p.is_file()):
                name = path.relative_to(self.app_root).as_posix()
                content = path.read_bytes()
                files[name] = hashlib.sha256(content).hexdigest()
                digest.update(name.encode("utf-8") + b"\0" + content + b"\0")
            self.assertEqual(health["files"], files)
            self.assertEqual(health["fingerprint"], digest.hexdigest())
            self.assertTrue(health["started_at"])
            self.assertIsNone(health["projects"]["sample"]["error"])
            self.assertEqual(self.get_json("/api/projects"), [{"id": "sample", "name": "試験用"}])
            with open(self.app_root / "static/app.js", "w", encoding="utf-8", newline="\n") as stream:
                stream.write("changed after startup\n")
            self.assertEqual(self.get_json("/healthz")["fingerprint"], health["fingerprint"])


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    unittest.main()
