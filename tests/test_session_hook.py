import contextlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "skills/live-board/scripts/tools/session_hook.py"
spec = importlib.util.spec_from_file_location("session_hook", HOOK)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class SessionHookTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cwd = self.root / "repo" / "subfolder"
        self.cwd.mkdir(parents=True)
        (self.cwd.parent / ".git").write_text("gitdir: unused\n", encoding="utf-8", newline="\n")
        self.data_dir = self.root / "plugin data"
        self.path = self.data_dir / "sessions" / "session_1.json"

    def event(self, event="SessionStart", **extra):
        return {"hook_event_name": event, "session_id": "session_1", "cwd": str(self.cwd), **extra}

    def run_hook(self, data=None, raw=None, folder=None):
        result = subprocess.run(
            [sys.executable, str(HOOK), str(folder or self.data_dir)],
            input=json.dumps(data or self.event(), ensure_ascii=False) if raw is None else raw,
            capture_output=True, text=True, encoding="utf-8", timeout=5,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, cwd=ROOT,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        return result

    def send(self, event="SessionStart", **extra):
        self.run_hook(self.event(event, **extra))
        return json.loads(self.path.read_text(encoding="utf-8"))

    def tool(self, name, value, response=None):
        return self.send("PostToolUse", tool_name=name, tool_input=value, tool_response=response or {})

    def todos(self):
        return [{"content": "実装", "status": "in_progress", "activeForm": "実装中"},
                {"content": "確認", "status": "pending"}, {"content": "公開", "status": "pending"}]

    def test_session_lifecycle_git_worktree_root_and_timestamps(self):
        first = self.send(source="startup")
        self.assertEqual(first["schema"], 1)
        self.assertEqual(first["session_id"], "session_1")
        self.assertEqual(first["project"], str(self.cwd.parent))
        self.assertEqual(first["cwd"], str(self.cwd))
        self.assertEqual(first["tasks"], [])
        self.assertIsNone(first["ended_at"])
        self.assertFalse(first["waiting_for_user"])
        self.assertEqual(first["last_prompt"], "")
        self.assertEqual(datetime.fromisoformat(first["started_at"]).utcoffset(), timedelta(0))
        ended = self.send("SessionEnd", reason="prompt_input_exit")
        self.assertIsNotNone(ended["ended_at"])
        self.assertEqual(ended["started_at"], first["started_at"])
        self.assertGreaterEqual(ended["updated_at"], first["updated_at"])
        self.assertIsNone(self.send(source="resume")["ended_at"])

    def test_root_falls_back_to_cwd_and_handles_git_directory(self):
        (self.cwd.parent / ".git").unlink()
        with mock.patch.object(hook.Path, "exists", return_value=False):
            self.assertEqual(hook.project_root(str(self.cwd)), str(self.cwd))
        (self.cwd.parent / ".git").mkdir()
        self.assertEqual(self.send()["project"], str(self.cwd.parent))

    def test_resume_compact_keep_tasks_clear_removes_them(self):
        tasks = self.tool("TodoWrite", {"todos": self.todos()})["tasks"]
        for source in ("resume", "compact"):
            self.assertEqual(self.send(source=source)["tasks"], tasks)
        self.assertEqual(self.send(source="clear")["tasks"], [])

    def test_prompt_sanitized_truncated_and_clears_waiting(self):
        self.send("PreToolUse", tool_name="AskUserQuestion")
        record = self.send("UserPromptSubmit", prompt="\x00\n\t\u202e\u2028" + "題" * 140)
        self.assertEqual(record["last_prompt"], "題" * 120)
        self.assertFalse(record["waiting_for_user"])
        self.assertEqual(record["tasks"], [])

    def test_question_pre_and_post(self):
        self.assertTrue(self.send("PreToolUse", tool_name="AskUserQuestion")["waiting_for_user"])
        self.assertFalse(self.send("PostToolUse", tool_name="AskUserQuestion")["waiting_for_user"])

    def assert_recorded_text(self, text, expected):
        self.assertEqual(self.send("UserPromptSubmit", prompt=text)["last_prompt"], expected[:120])
        events = (
            ("TaskCreate", {"subject": text}, {"task": {"id": "1"}}),
            ("TaskUpdate", {"taskId": "1", "subject": text}, {}),
            ("TodoWrite", {"todos": [{"content": text, "status": "pending"}]}, {}),
            ("mcp__ccd_turn__update_plan", {"items": [{"content": text, "status": "pending"}]}, {}),
        )
        for tool, value, response in events:
            with self.subTest(tool=tool):
                self.assertEqual(self.tool(tool, value, response)["tasks"][0]["text"], expected)

    def test_secret_shapes_masked_in_prompts_and_all_task_tools(self):
        secrets = [
            *("gh" + kind + "_" + "A" * 36 for kind in "pousr"),
            "github" + "_pat_" + "A" * 82,
            "sk" + "-" + "A" * 48,
            "sk" + "-proj-" + "A" * 48,
            "sk" + "-ant-api03-" + "A" * 48,
            *(prefix + "A" * 16 for prefix in ("AK" + "IA", "AS" + "IA")),
            *("xox" + kind + "-" + "1" * 12 + "-" + "A" * 24 for kind in "abprs"),
            "Bearer " + "A" * 32,
            "bEaReR " + "A" * 8 + "." + "B" * 8 + "_-~+/" + "C" * 8 + "==",
        ]
        for index, secret in enumerate(secrets):
            with self.subTest(shape=index):
                self.assert_recorded_text("確認 " + secret + " end", "確認 [redacted] end")

    def test_sensitive_key_values_masked_without_changing_keys_or_quotes(self):
        for key in ("password", "DB_PASSWORD", "passwd", "client_secret", "accessToken", "api_key", "ApiKey"):
            for separator in ("=", ": "):
                for quote in ("", '"', "'"):
                    with self.subTest(key=key, separator=separator, quote=quote):
                        prefix = key + separator + quote
                        self.assert_recorded_text(prefix + "sample-value" + quote + ", next",
                                                  prefix + "[redacted]" + quote + ", next")
        self.assert_recorded_text('{"api_key": "two sample words"}', '{"api_key": "[redacted]"}')
        self.assert_recorded_text("'password': 'two sample words'", "'password': '[redacted]'")
        self.assert_recorded_text("token=Bearer " + "A" * 32, "token=[redacted]")

    def test_secret_redaction_precedes_prompt_truncation(self):
        secret = "gh" + "p_" + "A" * 36
        for offset in (100, 115, 119, 120):
            with self.subTest(offset=offset):
                prefix = "題" * offset
                self.assert_recorded_text(prefix + secret + " end", prefix + "[redacted] end")

    def test_invisible_characters_removed_before_secret_redaction(self):
        secret = "gh" + "p_" + "\u200b" + "A" * 36
        self.assert_recorded_text("確認 " + secret + " end", "確認 [redacted] end")
        self.assert_recorded_text("a\x00\u200b\u2028\u2029b", "ab")
        self.assertEqual(hook.sanitize_text("a\ud800b"), "ab")

    def test_ordinary_prose_and_paths_unchanged(self):
        for text in ("依頼文を確認して、次のタスクを実装してください。",
                     "Review the password policy and token handling, then run the tests.",
                     r"Open C:\work\project\notes.txt and /opt/example/project/notes.md."):
            with self.subTest(text=text):
                self.assert_recorded_text(text, text)

    def test_task_create_update_missing_status_and_delete(self):
        created = self.tool("TaskCreate", {"subject": "実装", "description": "説明"},
                            {"task": {"id": "1", "subject": "実装"}})
        self.assertEqual(created["tasks"], [{"id": "1", "text": "実装", "status": "pending"}])
        updated = self.tool("TaskUpdate", {"taskId": "1", "status": "in_progress"},
                            {"success": True, "taskId": "1", "statusChange": {"from": "pending", "to": "in_progress"}})
        self.assertEqual(updated["tasks"][0]["status"], "in_progress")
        unchanged = self.tool("TaskUpdate", {"taskId": "1", "owner": "agent"}, {"success": True, "taskId": "1"})
        self.assertEqual(unchanged["tasks"], updated["tasks"])
        completed = self.tool("TaskUpdate", {"taskId": "1"},
                              {"success": True, "taskId": "1", "statusChange": {"from": "in_progress", "to": "completed"}})
        self.assertEqual(completed["tasks"][0]["status"], "completed")
        self.assertEqual(self.tool("TaskUpdate", {"taskId": "1", "status": "deleted"})["tasks"], [])

    def test_parallel_events_preserve_all_tasks(self):
        self.send()
        barrier = threading.Barrier(16)

        def create(number):
            barrier.wait(timeout=5)
            self.run_hook(self.event("PostToolUse", tool_name="TaskCreate",
                                    tool_input={"subject": f"Task {number}"},
                                    tool_response={"task": {"id": str(number)}}))

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(create, range(16)))
        record = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual({task["id"] for task in record["tasks"]}, {str(n) for n in range(16)})
        self.assertEqual(len(record["tasks"]), 16)
        self.assertEqual(list(self.path.parent.glob("*.lock")), [])

    def test_session_lock_timeout_is_silent_and_does_not_write(self):
        self.send()
        before = self.path.read_bytes()
        lock = self.path.with_suffix(".lock")
        lock.write_text("", encoding="utf-8", newline="\n")
        self.send("UserPromptSubmit", prompt="must not overwrite")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(lock.exists())

    def test_stale_session_lock_is_recovered(self):
        self.send()
        lock = self.path.with_suffix(".lock")
        lock.write_text("", encoding="utf-8", newline="\n")
        old = time.time() - 31
        os.utime(lock, (old, old))
        self.assertEqual(self.send("UserPromptSubmit", prompt="recovered")["last_prompt"], "recovered")
        self.assertFalse(lock.exists())

    def test_unknown_task_update_and_invalid_status(self):
        self.assertEqual(self.tool("TaskUpdate", {"taskId": "9", "status": "completed"})["tasks"],
                         [{"id": "9", "text": "#9", "status": "completed"}])
        tasks = self.tool("TaskUpdate", {"taskId": "10"})["tasks"]
        self.assertEqual(tasks[-1], {"id": "10", "text": "#10", "status": "pending"})
        self.assertEqual(self.tool("TaskUpdate", {"taskId": "9", "status": "invalid"})["tasks"], tasks)

    def test_task_update_renames_with_and_without_status(self):
        self.tool("TaskCreate", {"subject": "Original"}, {"task": {"id": "1"}})
        tasks = self.tool("TaskUpdate", {"taskId": "1", "subject": "Renamed", "status": "in_progress"})["tasks"]
        self.assertEqual(tasks, [{"id": "1", "text": "Renamed", "status": "in_progress"}])
        tasks = self.tool("TaskUpdate", {"taskId": "1", "subject": "Title only"})["tasks"]
        self.assertEqual(tasks, [{"id": "1", "text": "Title only", "status": "in_progress"}])
        self.assertEqual(self.tool("TaskUpdate", {"taskId": "1", "subject": "Ignored"},
                                   {"success": False})["tasks"], tasks)
        tasks = self.tool("TaskUpdate", {"taskId": "2", "subject": "Previously unseen"})["tasks"]
        self.assertEqual(tasks[-1], {"id": "2", "text": "Previously unseen", "status": "pending"})

    def test_todo_write_replaces_and_numbers(self):
        self.tool("TodoWrite", {"todos": self.todos()})
        record = self.tool("TodoWrite", {"todos": [{"content": "完了", "status": "completed"}]})
        self.assertEqual(record["tasks"], [{"id": "1", "text": "完了", "status": "completed"}])
        self.assertEqual(self.tool("TodoWrite", {"todos": []})["tasks"], [])

    def test_update_plan_full_list_advance_and_indexes(self):
        tool = "mcp__ccd_turn__update_plan"
        initial = self.tool(tool, {"items": self.todos(), "scope": "turn"})
        advanced = self.tool(tool, {"advance": True, "ignored": True})
        self.assertEqual([task["status"] for task in advanced["tasks"]], ["completed", "in_progress", "pending"])
        indexed = self.tool(tool, {"done": [2], "start": 3, "ignored": True})
        self.assertEqual([task["status"] for task in indexed["tasks"]], ["completed", "completed", "in_progress"])
        self.assertEqual(self.tool(tool, {"other": 1})["tasks"], indexed["tasks"])
        self.assertEqual([task["text"] for task in initial["tasks"]], ["実装", "確認", "公開"])

    def test_wrap_up_completes_all_tasks(self):
        initial = self.tool("mcp__ccd_turn__update_plan", {"items": self.todos()})["tasks"]
        tasks = self.tool("mcp__ccd_turn__wrap_up", {})["tasks"]
        self.assertEqual(tasks, [{**task, "status": "completed"} for task in initial])

    def test_update_plan_advance_without_current_and_bounds(self):
        tool = "mcp__other__update_plan"
        self.tool(tool, {"items": [{"content": "確認", "status": "pending"}]})
        tasks = self.tool(tool, {"advance": True})["tasks"]
        self.assertEqual(tasks[0]["status"], "in_progress")
        self.assertEqual(self.tool(tool, {"done": [0, -1, 2], "start": 0})["tasks"], tasks)
        self.assertEqual(self.tool(tool, {"advance": True})["tasks"][0]["status"], "completed")
        self.tool(tool, {"items": [{"content": "保留", "status": "pending"}, *self.todos()]})
        self.assertEqual([task["status"] for task in self.tool(tool, {"advance": True})["tasks"]],
                         ["pending", "completed", "in_progress", "pending"])

    def test_bad_session_ids_cannot_escape_folder(self):
        for session_id in ("", "../escape", "a/b", "a\\b", ".", "a.json", "a" * 129, "日本語", "a\n", None):
            with self.subTest(session_id=session_id):
                self.run_hook(self.event(session_id=session_id))
        self.assertFalse(self.data_dir.exists())
        self.run_hook(self.event(session_id="A_9-" + "x" * 124))
        self.assertEqual(len(list((self.data_dir / "sessions").glob("*.json"))), 1)

    def test_malformed_missing_fields_and_wrong_types_are_silent(self):
        for raw in ("", "{", "[]", "null", "{}", '{"session_id":"ok"}'):
            self.run_hook(raw=raw)
        for patch in ({"cwd": ""}, {"cwd": None}, {"hook_event_name": None},
                      {"hook_event_name": "UserPromptSubmit"}, {"hook_event_name": "PostToolUse", "tool_name": "TaskCreate"}):
            self.run_hook(self.event(**patch))
        self.assertFalse(self.data_dir.exists())

    def test_unwritable_folder_and_replace_failure_are_silent(self):
        blocked = self.root / "file"
        blocked.write_text("file", encoding="utf-8", newline="\n")
        self.run_hook(folder=blocked)
        self.invoke_in_process(mock.patch.object(hook.Path, "mkdir", side_effect=PermissionError))
        self.assertFalse(self.path.exists())
        self.send()
        before = self.path.read_bytes()
        self.invoke_in_process(mock.patch.object(hook.os, "replace", side_effect=PermissionError))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def invoke_in_process(self, patch):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch, mock.patch.object(sys, "argv", [str(HOOK), str(self.data_dir)]), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(self.event()))), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(hook.main(), 0)
        self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))

    def test_atomic_replace_same_folder_and_utf8_lf(self):
        self.send("UserPromptSubmit", prompt="日本語")
        before = self.path.read_bytes()
        replace = os.replace
        def inspect(source, destination):
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertEqual(Path(destination), self.path)
            self.assertEqual(self.path.read_bytes(), before)
            raw = Path(source).read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\r", raw)
            self.assertEqual(json.loads(raw)["last_prompt"], "日本語")
            replace(source, destination)
        self.invoke_in_process(mock.patch.object(hook.os, "replace", side_effect=inspect))
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_retention_only_removes_old_session_records(self):
        current = self.send()
        now = datetime.now(timezone.utc)
        folder = self.path.parent
        for name, days, ended in (("old", 8, True), ("stale", 8, False), ("recent", 6, True)):
            stamp = (now - timedelta(days=days)).isoformat()
            record = {**current, "session_id": name, "updated_at": current["updated_at"] if ended else stamp,
                      "ended_at": stamp if ended else None}
            (folder / f"{name}.json").write_text(json.dumps(record), encoding="utf-8", newline="\n")
        untouched = {"other.json": "{}", "broken.json": "{", "notes.txt": "notes", "leftover.tmp": "temporary"}
        for name, content in untouched.items():
            (folder / name).write_text(content, encoding="utf-8", newline="\n")
        self.send()
        self.assertFalse((folder / "old.json").exists())
        self.assertTrue((folder / "stale.json").exists())
        self.assertTrue((folder / "recent.json").exists())
        for name, content in untouched.items():
            self.assertEqual((folder / name).read_text(encoding="utf-8"), content)

    def test_retention_boundary_uses_end_time_only(self):
        current = self.send()
        now = datetime.now(timezone.utc)
        for name, ended in (("boundary", now - timedelta(days=7)),
                            ("expired", now - timedelta(days=7, seconds=1)),
                            ("running", None)):
            record = {**current, "session_id": name,
                      "started_at": (now - timedelta(days=60)).isoformat(),
                      "updated_at": (now - timedelta(days=30)).isoformat(),
                      "ended_at": ended.isoformat() if ended else None}
            (self.path.parent / f"{name}.json").write_text(json.dumps(record), encoding="utf-8", newline="\n")
        hook.prune_sessions(self.path.parent, now)
        self.assertTrue((self.path.parent / "boundary.json").exists())
        self.assertFalse((self.path.parent / "expired.json").exists())
        self.assertTrue((self.path.parent / "running.json").exists())

    def test_run_well_under_one_second(self):
        started = time.perf_counter()
        self.send()
        self.assertLess(time.perf_counter() - started, 0.75)

    def test_hook_manifest_events_matchers_and_timeout(self):
        manifest = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(set(manifest), {"SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse"})
        self.assertEqual(manifest["PreToolUse"][0]["matcher"], "AskUserQuestion")
        self.assertEqual(manifest["PostToolUse"][0]["matcher"], "TaskCreate|TaskUpdate|TodoWrite|AskUserQuestion|mcp__.*__update_plan|mcp__.*__wrap_up")
        for entries in manifest.values():
            self.assertEqual(entries[0]["hooks"], [{"type": "command", "timeout": 5,
                "command": 'python "${CLAUDE_PLUGIN_ROOT}/skills/live-board/scripts/tools/session_hook.py" "${CLAUDE_PLUGIN_DATA}"'}])


if __name__ == "__main__":
    unittest.main()
