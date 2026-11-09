import copy
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/live-board/scripts"))

from adapters import winsmux_backlog
import pr_status as pr
from server import Board, ProjectState, create_server, project_configs, source_date_info


FAKE_GH = str(Path(sys.executable).with_name("fake-gh.exe"))

AT = "2026-10-31T00:00:00+00:00"


def pull(number=6, state="OPEN", ci="SUCCESS", draft=False):
    return {"number": number, "url": f"https://github.com/owner/repo/pull/{number}",
            "title": "PR の題 <&>", "state": state, "isDraft": draft,
            "mergedAt": AT if state == "MERGED" else None,
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": ci} if ci else None}}]}}


def response(pulls):
    return subprocess.CompletedProcess([], 0, json.dumps({"data": {"repository": pulls}}), "")


def record(state="OPEN", number=6, ci="SUCCESS", draft=False, error=None):
    data = pr.map_pull_request(pull(number, state, ci, draft), "owner/repo", number)
    return {"number": number, "data": data, "status": "error" if error else "ok", "error": error}


class GhResolutionTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cwd = self.root / "cwd"
        self.bin = self.root / "bin"
        self.cwd.mkdir()
        self.bin.mkdir()
        previous = Path.cwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous)
        environment = mock.patch.dict(os.environ, {"PATHEXT": ".EXE"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("NoDefaultCurrentDirectoryInExePath", None)
        self.runner = mock.Mock(return_value=response({"pr_6": pull()}))

    def executable(self, directory):
        executable = directory / ("gh.exe" if os.name == "nt" else "gh")
        executable.write_bytes(b"fake executable; never run")
        executable.chmod(0o755)
        return executable

    def test_cwd_shadow_does_not_hide_external_path_executable(self):
        self.executable(self.cwd)
        executable = self.executable(self.bin)
        os.environ["PATH"] = str(self.bin)
        pr.fetch_pull_requests("owner/repo", {6}, self.runner)
        self.runner.assert_called_once()
        self.assertEqual(self.runner.call_args.args[0][0], str(executable))

    def test_path_excludes_empty_dot_and_cwd_entries(self):
        executable = self.executable(self.bin)
        os.environ["PATH"] = os.pathsep.join(("", ".", str(self.cwd), str(self.cwd / ".." / "cwd"), str(self.bin), ""))
        pr.fetch_pull_requests("owner/repo", {6}, self.runner)
        self.assertEqual(Path(self.runner.call_args.args[0][0]).resolve(), executable)

    def test_cwd_executable_is_never_run(self):
        executable = self.executable(self.cwd)
        for path in ("", ".", str(self.cwd), str(self.bin)):
            with self.subTest(path=path), mock.patch.dict(os.environ, {"PATH": path}):
                with self.assertRaisesRegex(pr.PRFetchError, "gh が見つかりません"):
                    pr.fetch_pull_requests("owner/repo", {6}, self.runner)
        self.runner.assert_not_called()
        self.assertTrue(executable.exists())

    def test_relative_and_nested_path_entries_are_skipped(self):
        nested = self.cwd / "nested"
        nested.mkdir()
        self.executable(nested)
        executable = self.executable(self.bin)
        for entry in ("nested", str(nested), str(nested / ".." / "nested"), os.path.join("..", "bin")):
            with self.subTest(entry=entry), mock.patch.dict(os.environ, {"PATH": entry}):
                with self.assertRaisesRegex(pr.PRFetchError, "gh が見つかりません"):
                    pr.fetch_pull_requests("owner/repo", {6}, self.runner)
        self.runner.assert_not_called()
        os.environ["PATH"] = os.pathsep.join(("nested", str(nested), str(self.bin)))
        pr.fetch_pull_requests("owner/repo", {6}, self.runner)
        self.assertEqual(self.runner.call_args.args[0][0], str(executable))

    def test_normal_path_uses_first_executable(self):
        executable = self.executable(self.bin)
        second = self.root / "second"
        second.mkdir()
        self.executable(second)
        os.environ["PATH"] = os.pathsep.join((str(self.bin), str(second)))
        pr.fetch_pull_requests("owner/repo", {6}, self.runner)
        self.runner.assert_called_once()
        self.assertEqual(self.runner.call_args.args[0][0], str(executable))

    def test_directory_named_gh_is_skipped(self):
        (self.bin / ("gh.exe" if os.name == "nt" else "gh")).mkdir()
        os.environ["PATH"] = str(self.bin)
        with self.assertRaisesRegex(pr.PRFetchError, "gh が見つかりません"):
            pr.fetch_pull_requests("owner/repo", {6}, self.runner)
        self.runner.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows PATHEXT search")
    def test_empty_pathext_uses_default_extensions(self):
        executable = self.executable(self.bin)
        os.environ["PATH"] = str(self.bin)
        for extensions in (None, "", ";;;", " \t ", "; \t; ;"):
            with self.subTest(extensions=extensions), mock.patch.dict(os.environ):
                if extensions is None:
                    os.environ.pop("PATHEXT", None)
                else:
                    os.environ["PATHEXT"] = extensions
                self.assertEqual(pr.find_gh(), str(executable))

    @unittest.skipUnless(os.name == "nt", "Windows PATHEXT search")
    def test_pathext_order_is_used_within_each_path_directory(self):
        executable = self.executable(self.bin)
        command = self.bin / "gh.cmd"
        command.write_bytes(b"fake command; never run")
        os.environ["PATH"] = str(self.bin)
        for extensions, expected in ((".CMD;.EXE", command), (".EXE;.CMD", executable), (".MISSING;.EXE", executable)):
            with self.subTest(extensions=extensions), mock.patch.dict(os.environ, {"PATHEXT": extensions}):
                pr.fetch_pull_requests("owner/repo", {6}, self.runner)
                self.assertEqual(self.runner.call_args.args[0][0], str(expected))

    @unittest.skipIf(os.name == "nt", "POSIX executable permission")
    def test_non_executable_file_is_skipped(self):
        self.executable(self.bin).chmod(0o644)
        os.environ["PATH"] = str(self.bin)
        with self.assertRaisesRegex(pr.PRFetchError, "gh が見つかりません"):
            pr.fetch_pull_requests("owner/repo", {6}, self.runner)
        self.runner.assert_not_called()

    def test_missing_path_never_uses_platform_default(self):
        os.environ.pop("PATH", None)
        with self.assertRaisesRegex(pr.PRFetchError, "gh が見つかりません"):
            pr.fetch_pull_requests("owner/repo", {6}, self.runner)
        self.runner.assert_not_called()


class PRTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "backlog.yaml"
        self.project = {"id": "fixture", "name": "試験", "adapter": "winsmux-backlog",
                        "source": str(self.source), "build_py": str(self.root / "build.py"),
                        "github_repo": "owner/repo"}
        # Deliberately drops pr and changes order, like an external build.py is allowed to.
        self.write(self.root / "build.py", '''def validate(data):
    return []

def dashboard_rows(data):
    return {"tasks": [{k: v for k, v in task.items() if k != "pr"} for task in reversed(data["tasks"])],
            "phases": [], "versions": []}
''')
        self.data = {"meta": {"product": "試験", "updated": "2026-10-30", "current_phase": "P1"},
                     "tasks": [{"id": "T1", "title": "課題", "status": "doing", "pr": 6}]}
        self.save()
        self.find_gh = mock.patch("pr_status.find_gh", return_value=FAKE_GH).start()
        self.addCleanup(mock.patch.stopall)
        # A regression in runner injection must never invoke the host gh.
        self.real_runner = mock.patch("pr_status.subprocess.run", side_effect=AssertionError("Real gh is forbidden")).start()

    def write(self, path, text):
        with open(path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)

    def save(self):
        self.write(self.source, yaml.safe_dump(self.data, allow_unicode=True))

    def task(self, board):
        return board.projects["fixture"].snapshot()["last_good"]["rows"]["tasks"][0]

    def test_raw_pr_join_by_id_and_optional_key(self):
        self.data["tasks"].append({"id": "T2", "title": "別の課題", "status": "todo", "pr": [7, 8, 7]})
        self.data["tasks"].append({"id": "T3", "title": "未連携", "status": "todo"})
        self.save()
        tasks = winsmux_backlog(self.project)["rows"]["tasks"]
        self.assertEqual([(task["id"], task["pr"]) for task in tasks], [("T3", []), ("T2", [7, 8]), ("T1", [6])])

    def test_invalid_pr_shapes_are_japanese_load_errors_and_keep_good(self):
        state = ProjectState(self.project)
        state.check()
        good = state.snapshot()["last_good"]
        for value in (None, True, False, "6", 0, -1, 2.5, [], [6, "7"], [True], {"number": 6}, 2_147_483_648):
            with self.subTest(value=value):
                self.data["tasks"][0]["pr"] = value
                self.save()
                state.check()
                snapshot = state.snapshot()
                self.assertEqual(snapshot["last_good"], good)
                self.assertIn("課題 T1: pr は正の整数", snapshot["error"]["message"])

    def test_github_repo_validation(self):
        for value in (None, 6, [], {}, "", "owner", "owner/repo/extra", "https://github.com/a/b", " owner/repo", "owner/repo\n", "../repo", "owner/..", "-owner/repo"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "github_repo"):
                project_configs({"projects": [{**self.project, "github_repo": value}]})
        for value in ("owner/repo", "a-b/.repo_1", "Owner/repo.git"):
            self.assertEqual(project_configs({"projects": [{**self.project, "github_repo": value}]})[0]["github_repo"], value)
        without = {key: value for key, value in self.project.items() if key != "github_repo"}
        self.assertEqual(project_configs({"projects": [without]}), [without])

    def test_graphql_mapping_and_ci_states(self):
        for state in ("OPEN", "MERGED", "CLOSED"):
            for raw, expected in (("SUCCESS", "success"), ("FAILURE", "failure"), ("ERROR", "failure"), ("PENDING", "pending"), ("EXPECTED", "pending"), (None, "none")):
                with self.subTest(state=state, ci=raw):
                    mapped = pr.map_pull_request(pull(state=state, ci=raw, draft=True), "owner/repo", 6)
                    self.assertEqual(mapped, {"number": 6, "url": "https://github.com/owner/repo/pull/6", "title": "PR の題 <&>",
                                              "state": state, "is_draft": True, "merged_at": AT if state == "MERGED" else None, "ci": expected})
        empty = pull()
        empty["commits"]["nodes"] = []
        self.assertEqual(pr.map_pull_request(empty, "owner/repo", 6)["ci"], "none")

    def test_incomplete_response_never_means_closed_or_no_ci(self):
        for key in pull():
            broken = pull()
            del broken[key]
            with self.subTest(key=key), self.assertRaises(pr.PRFetchError):
                pr.map_pull_request(broken, "owner/repo", 6)
        for change in ({"state": "FUTURE"}, {"number": True}, {"isDraft": None}, {"url": "javascript:alert(1)"},
                       {"commits": {}}, {"commits": {"nodes": [{}]}}, {"title": "\ud800"}):
            with self.subTest(change=ascii(change)), self.assertRaises(pr.PRFetchError):
                pr.map_pull_request({**pull(), **change}, "owner/repo", 6)
        with self.assertRaises(pr.PRFetchError):
            pr.map_pull_request(pull(ci="UNKNOWN"), "owner/repo", 6)
        runner = mock.Mock(return_value=response({"pr_6": None, "pr_7": pull(7)}))
        mapped = pr.fetch_pull_requests("owner/repo", {6, 7}, runner)
        self.assertTrue(mapped[6]["error"])
        self.assertIsNone(mapped[6]["data"])
        self.assertIsNone(mapped[7]["error"])

    def test_merged_task_mismatch(self):
        for status in ("todo", "doing", "blocked", "done"):
            flags = pr.task_flags({"status": status}, [record("MERGED")])
            self.assertEqual(bool(flags), status != "done")
        self.assertIn("PR 6番はマージ済み・課題は進行中", pr.task_flags({"status": "doing"}, [record("MERGED")])[0]["message"])

    def test_graphql_nonzero_partial_not_found_preserves_healthy_pr(self):
        payload = {"data": {"repository": {"pr_6": pull(), "pr_99999": None}},
                   "errors": [{"type": "NOT_FOUND", "path": ["repository", "pr_99999"],
                               "message": "Could not resolve to a PullRequest with the number of 99999."}]}
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                runner = mock.Mock(return_value=subprocess.CompletedProcess(
                    [], exit_code, json.dumps(payload), "gh: Could not resolve to a PullRequest"))
                mapped = pr.fetch_pull_requests("owner/repo", {6, 99999}, runner)
                self.assertEqual(mapped[6], {"data": pr.map_pull_request(pull(), "owner/repo", 6), "error": None})
                self.assertEqual(mapped[99999], {"data": None, "error": "PR 99999番が見つかりません"})

    def test_gh_runs_without_a_console_window(self):
        runner = mock.Mock(return_value=response({"pr_6": pull()}))
        pr.fetch_pull_requests("owner/repo", {6}, runner)
        self.assertEqual(runner.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if sys.platform == "win32":
            self.assertNotEqual(runner.call_args.kwargs["creationflags"], 0)

    def test_graphql_field_errors_never_make_partial_data_current(self):
        for path, value in ((["repository", "pr_6"], pull()),
                            (["repository", "pr_6", "commits", "nodes", 0, "commit", "statusCheckRollup"], pull(ci=None))):
            for error_type in ("INTERNAL", "RATE_LIMITED", "FORBIDDEN", "UNKNOWN", None):
                for exit_code in (0, 1):
                    with self.subTest(path=path, error_type=error_type, exit_code=exit_code):
                        payload = {"data": {"repository": {"pr_6": value, "pr_7": pull(7)}},
                                   "errors": [{"type": error_type, "path": path, "message": "private details"}]}
                        runner = mock.Mock(return_value=subprocess.CompletedProcess([], exit_code, json.dumps(payload), ""))
                        with self.assertRaises(pr.PRFetchError) as caught:
                            pr.fetch_pull_requests("owner/repo", {6, 7}, runner)
                        self.assertNotIn("private", str(caught.exception))

    def test_graphql_nonzero_complete_data_requires_nonempty_errors_list(self):
        for extra in ({}, {"errors": None}, {"errors": []}, {"errors": {}}, {"errors": ""}):
            with self.subTest(extra=extra):
                payload = {"data": {"repository": {"pr_6": pull()}}, **extra}
                runner = mock.Mock(return_value=subprocess.CompletedProcess(
                    [], 1, json.dumps(payload), "API rate limit exceeded: private details"))
                with self.assertRaises(pr.PRFetchError) as caught:
                    pr.fetch_pull_requests("owner/repo", {6}, runner)
                self.assertNotIn("private", str(caught.exception))

    def test_graphql_partial_data_requires_every_error_to_be_scoped_not_found(self):
        missing = {"type": "NOT_FOUND", "path": ["repository", "pr_99999"]}
        for error in ({"type": "RATE_LIMITED", "path": ["repository", "pr_6"]},
                      {"type": "UNKNOWN", "path": ["repository", "pr_6"]},
                      {"path": ["repository", "pr_6"]},
                      {"type": "NOT_FOUND", "path": ["repository", "pr_6", "commits"]},
                      {"type": "NOT_FOUND", "path": ["repository", "pr_8"]},
                      {"type": "NOT_FOUND", "path": ["other", "pr_6"]},
                      {"type": "NOT_FOUND"}, "invalid error"):
            for exit_code in (0, 1):
                with self.subTest(error=error, exit_code=exit_code):
                    payload = {"data": {"repository": {"pr_6": pull(), "pr_99999": None}},
                               "errors": [missing, error]}
                    runner = mock.Mock(return_value=subprocess.CompletedProcess([], exit_code, json.dumps(payload), ""))
                    with self.assertRaises(pr.PRFetchError):
                        pr.fetch_pull_requests("owner/repo", {6, 99999}, runner)

    def test_graphql_null_repository_and_unscoped_errors_fail_whole_response(self):
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                payload = {"data": {"repository": None},
                           "errors": [{"type": "NOT_FOUND", "path": ["repository"], "message": "private repo"}]}
                runner = mock.Mock(return_value=subprocess.CompletedProcess([], exit_code, json.dumps(payload), "private stderr"))
                with self.assertRaisesRegex(pr.PRFetchError, "リポジトリを取得できません（名前・権限を確認してください）"):
                    pr.fetch_pull_requests("owner/repo", {6, 7}, runner)
        for error in ({"type": "RATE_LIMITED"}, {"path": ["repository"]}, {"path": ["other"]}, "invalid error"):
            with self.subTest(error=error):
                payload = {"data": {"repository": {"pr_6": pull()}}, "errors": [error]}
                runner = mock.Mock(return_value=subprocess.CompletedProcess([], 1, json.dumps(payload), ""))
                with self.assertRaises(pr.PRFetchError):
                    pr.fetch_pull_requests("owner/repo", {6}, runner)

    def test_open_todo_mismatch_including_draft(self):
        for draft in (False, True):
            for status in ("todo", "doing", "blocked", "done"):
                self.assertEqual(bool(pr.task_flags({"status": status}, [record(draft=draft)])), status == "todo")

    def test_all_closed_rule_requires_every_linked_pr_known_and_unmerged(self):
        for status in ("todo", "doing", "blocked", "done"):
            self.assertEqual(bool(pr.task_flags({"status": status}, [record("CLOSED"), record("CLOSED", 7)])), status in ("doing", "done"))
        for sibling in (record("OPEN", 7), record("MERGED", 7), record("CLOSED", 7, error={"message": "failed"}),
                        {"status": "pending", "data": None, "error": None}):
            flags = pr.task_flags({"status": "done"}, [record("CLOSED"), sibling])
            self.assertFalse(any("すべて" in flag["message"] for flag in flags))

    def test_ci_failure_only_warns_for_known_open_pr(self):
        for state in ("OPEN", "MERGED", "CLOSED"):
            flags = pr.task_flags({"status": "done"}, [record(state, ci="FAILURE")])
            self.assertEqual(any(flag["kind"] == "ci" for flag in flags), state == "OPEN")
        for state in ("OPEN", "MERGED", "CLOSED"):
            self.assertEqual(pr.task_flags({"status": "todo"}, [record(state, ci="FAILURE", error={"message": "failed"})]), [])

    def test_one_graphql_call_per_repo_uses_resolved_executable(self):
        runner = mock.Mock(return_value=response({"pr_6": pull(), "pr_7": pull(7)}))
        second_source = self.root / "second.yaml"
        second_data = copy.deepcopy(self.data)
        second_data["tasks"][0]["pr"] = [6, 7]
        self.write(second_source, yaml.safe_dump(second_data, allow_unicode=True))
        second = {**self.project, "id": "second", "source": str(second_source)}
        board = Board({"projects": [self.project, second]}, gh_runner=runner)
        board.refresh_pr_cycle()
        self.assertEqual(runner.call_count, 1)
        command = runner.call_args.args[0]
        self.assertEqual(command, [FAKE_GH, "api", "graphql", "--hostname", "github.com", "--input", "-"])
        payload = json.loads(runner.call_args.kwargs["input"])
        self.assertEqual(payload["variables"], {"owner": "owner", "name": "repo"})
        self.assertIn("pr_6: pullRequest(number: 6)", payload["query"])
        self.assertIn("pr_7: pullRequest(number: 7)", payload["query"])
        self.assertEqual(payload["query"].count("pr_6:"), 1)
        self.find_gh.assert_called_once_with()
        self.assertEqual(pr.PR_REFRESH_SECONDS, 120)

    def test_worker_uses_named_interval_and_fetches_after_timeout(self):
        board = Board({"projects": [self.project]}, gh_runner=mock.Mock())
        waits = []

        def wait(timeout):
            waits.append(timeout)
            if len(waits) == 2:
                board.stopping.set()
            return False  # No source event: elapsed interval alone must cause a cycle.

        board.pr_wakeup = mock.Mock()
        board.pr_wakeup.wait.side_effect = wait
        with mock.patch.object(board, "refresh_pr_cycle") as cycle:
            board.refresh_prs()
        self.assertEqual(waits, [pr.PR_REFRESH_SECONDS, pr.PR_REFRESH_SECONDS])
        cycle.assert_called_once_with()

    def test_failure_in_one_repo_does_not_hide_a_healthy_repo(self):
        second = {**self.project, "id": "second", "github_repo": "owner/other"}
        runner = mock.Mock(side_effect=[response({"pr_6": pull(state="MERGED")}), OSError()])
        board = Board({"projects": [self.project, second]}, gh_runner=runner)
        board.refresh_pr_cycle()
        self.assertEqual(runner.call_count, 2)
        self.assertTrue(self.task(board)["pr_flags"])
        other = board.projects["second"].snapshot()["last_good"]["rows"]["tasks"][0]
        self.assertEqual(other["prs"][0]["status"], "error")
        self.assertEqual(other["pr_flags"], [])

    def test_failures_preserve_last_good_age_and_suppress_flags_then_recover(self):
        runner = mock.Mock(return_value=response({"pr_6": pull(state="MERGED")}))
        board = Board({"projects": [self.project]}, gh_runner=runner)
        with mock.patch("server.now", return_value=AT):
            board.refresh_pr_cycle()
        self.assertTrue(self.task(board)["pr_flags"])
        good = self.task(board)["prs"][0]["data"]
        for failure in (OSError("private path"), RuntimeError("secret runner output"),
                        subprocess.TimeoutExpired("gh", 30),
                        subprocess.CompletedProcess([], 0, "bad JSON", ""),
                        subprocess.CompletedProcess([], 1, "", "not authenticated: private details"),
                        subprocess.CompletedProcess([], 1, "", "API rate limit exceeded: private details"),
                        subprocess.CompletedProcess([], 1, json.dumps({"data": {"repository": {"pr_6": pull()}}}),
                                                    "API rate limit exceeded: private details"),
                        subprocess.CompletedProcess([], 0, '{"data":null}', ""),
                        subprocess.CompletedProcess([], 0, '{"errors":[{"type":"RATE_LIMITED"}]}', "")):
            with self.subTest(failure=type(failure).__name__):
                runner.side_effect = failure if isinstance(failure, Exception) else None
                runner.return_value = failure
                with mock.patch("server.now", return_value="2026-10-31T00:03:00+00:00"):
                    board.refresh_pr_cycle()
                task = self.task(board)
                entry = task["prs"][0]
                self.assertEqual(entry["data"], good)
                self.assertEqual(entry["status"], "error")
                self.assertEqual(entry["age_seconds"], 180)
                self.assertIn("PR の状態を取得できません（", entry["error"]["message"])
                self.assertNotIn("private", entry["error"]["message"])
                self.assertNotIn("secret", entry["error"]["message"])
                self.assertEqual(task["pr_flags"], [])
                self.assertEqual(board.projects["fixture"].summary()["pr_summary"]["unknown_count"], 1)
        self.find_gh.return_value = None
        board.refresh_pr_cycle()
        self.assertIn("gh が見つかりません", self.task(board)["prs"][0]["error"]["message"])
        self.assertEqual(self.task(board)["prs"][0]["data"], good)
        self.find_gh.return_value = FAKE_GH
        runner.side_effect = None
        runner.return_value = response({"pr_6": pull(state="MERGED")})
        board.refresh_pr_cycle()
        self.assertEqual(self.task(board)["prs"][0]["status"], "ok")
        self.assertTrue(self.task(board)["pr_flags"])
        self.real_runner.assert_not_called()

    def test_completed_repo_results_publish_before_another_repo_finishes(self):
        self.data["tasks"][0]["pr"] = [6, 7]
        self.save()
        peer = {**self.project, "id": "peer"}
        second = {**self.project, "id": "second", "github_repo": "owner/other"}
        for outcome in ("failure", "partial", "success"):
            with self.subTest(outcome=outcome):
                entered, release = threading.Event(), threading.Event()
                phase = "seed"

                def runner(command, **kwargs):
                    repo = "owner/" + json.loads(kwargs["input"])["variables"]["name"]
                    if phase != "seed" and repo == "owner/other":
                        entered.set()
                        if not release.wait(5):
                            raise AssertionError("Slow repository was not released")
                    if phase == "failure" and repo == "owner/repo":
                        raise OSError("offline")
                    pulls = {f"pr_{number}": {**pull(number, "MERGED" if phase == "seed" else "OPEN"),
                                             "url": f"https://github.com/{repo}/pull/{number}"} for number in (6, 7)}
                    if phase == "partial" and repo == "owner/repo":
                        pulls["pr_6"] = None
                    return response(pulls)

                board = Board({"projects": [self.project, peer, second]}, gh_runner=runner)
                board.refresh_pr_cycle()
                self.assertTrue(self.task(board)["pr_flags"])
                before = board.revisions.copy()
                phase = outcome
                worker = threading.Thread(target=board.refresh_pr_cycle)
                worker.start()
                try:
                    self.assertTrue(entered.wait(5))
                    for project_id in ("fixture", "peer"):
                        project = board.projects[project_id]
                        project.check()  # An unchanged source must retain the newly published PR result.
                        task = project.snapshot()["last_good"]["rows"]["tasks"][0]
                        expected = {"failure": ["error", "error"], "partial": ["error", "ok"], "success": ["ok", "ok"]}[outcome]
                        self.assertEqual([entry["status"] for entry in task["prs"]], expected)
                        self.assertEqual(task["pr_flags"], [])
                        self.assertGreater(board.revisions[project_id], before[project_id])
                finally:
                    release.set()
                    worker.join(5)
                    board.stop()
                self.assertFalse(worker.is_alive())

    def test_unlink_and_relink_without_a_pr_cycle_does_not_restore_current_data(self):
        runner = mock.Mock(return_value=response({"pr_6": pull(state="MERGED")}))
        board = Board({"projects": [self.project]}, gh_runner=runner)
        board.refresh_pr_cycle()
        state = board.projects["fixture"]
        del self.data["tasks"][0]["pr"]
        self.save()
        state.check()
        self.assertEqual(state.pr_results, {})
        self.data["tasks"][0]["pr"] = 6
        self.save()
        state.check()
        entry = self.task(board)["prs"][0]
        self.assertEqual(entry["status"], "pending")
        self.assertIsNone(entry["data"])
        self.assertEqual(self.task(board)["pr_flags"], [])

    def test_unrequested_cached_pr_stays_unknown_until_its_own_fetch(self):
        entered, release = threading.Event(), threading.Event()
        queries = []
        blocking = False

        def runner(command, **kwargs):
            queries.append(json.loads(kwargs["input"])["query"])
            if blocking:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("PR fetch was not released")
            return response({"pr_6": pull(), "pr_7": pull(7)})

        board = Board({"projects": [self.project]}, gh_runner=runner)
        with mock.patch("server.now", return_value=AT):
            board.refresh_pr_cycle()
        previous = self.task(board)["prs"][0]
        del self.data["tasks"][0]["pr"]
        self.save()
        board.projects["fixture"].check()
        board.refresh_pr_cycle()
        self.assertIn(6, board.pr_cache["owner/repo"])
        self.data["tasks"][0]["pr"] = 7
        self.save()
        board.projects["fixture"].check()
        blocking = True
        worker = threading.Thread(target=board.refresh_pr_cycle)
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            self.data["tasks"][0]["pr"] = [6, 7]
            self.save()
            board.projects["fixture"].check()
            self.assertEqual(self.task(board)["prs"][0]["status"], "pending")
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("pr_6:", queries[-1])
        stale = self.task(board)["prs"][0]
        self.assertEqual(stale["status"], "error")
        self.assertEqual(stale["data"], previous["data"])
        self.assertEqual(stale["fetched_at"], previous["fetched_at"])
        self.assertEqual(stale["checked_at"], previous["checked_at"])
        self.assertIn("再取得を待っています", stale["error"]["message"])
        self.assertEqual(board.projects["fixture"].summary()["pr_summary"]["unknown_count"], 1)
        blocking = False
        with mock.patch("server.now", return_value="2026-10-31T00:03:00+00:00"):
            board.refresh_pr_cycle()
        self.assertEqual(self.task(board)["prs"][0]["status"], "ok")
        self.assertEqual(self.task(board)["prs"][0]["fetched_at"], "2026-10-31T00:03:00+00:00")

    def test_initial_failure_and_missing_repo_are_not_no_pr(self):
        for missing_repo in (False, True):
            project = dict(self.project)
            if missing_repo:
                del project["github_repo"]
            runner = mock.Mock(side_effect=OSError())
            board = Board({"projects": [project]}, gh_runner=runner)
            board.refresh_pr_cycle()
            task = self.task(board)
            self.assertEqual(len(task["prs"]), 1)
            self.assertIsNone(task["prs"][0]["data"])
            self.assertEqual(task["prs"][0]["status"], "error")
            self.assertEqual(task["pr_flags"], [])
            self.assertEqual(board.projects["fixture"].summary()["pr_summary"]["unknown_count"], 1)
            if missing_repo:
                runner.assert_not_called()

    def test_summary_counts_tasks_once_and_unknown_separately(self):
        self.data["tasks"][0]["pr"] = [6, 7, 8]
        self.save()
        runner = mock.Mock(return_value=response({"pr_6": pull(state="MERGED"), "pr_7": pull(7, "MERGED"), "pr_8": None}))
        board = Board({"projects": [self.project]}, gh_runner=runner)
        board.refresh_pr_cycle()
        summary = board.projects["fixture"].summary()["pr_summary"]
        self.assertEqual((summary["linked_count"], summary["mismatch_count"], summary["unknown_count"]), (3, 1, 1))

    def test_date_mismatch_uses_host_local_date_including_wbs_timestamp(self):
        mtime = datetime(2026, 10, 31, 12).timestamp()
        self.assertEqual(source_date_info("2026-10-30", mtime),
                         {"declared_local_date": "2026-10-30", "file_local_date": "2026-10-31", "mismatch": True})
        self.assertFalse(source_date_info("2026-10-31", mtime)["mismatch"])
        iso = datetime.fromtimestamp(mtime, timezone.utc).isoformat()
        self.assertFalse(source_date_info(iso, mtime)["mismatch"])
        self.assertIsNone(source_date_info("invalid", mtime)["mismatch"])
        os.utime(self.source, (mtime, mtime))
        state = ProjectState(self.project)
        state.check()
        self.assertTrue(state.snapshot()["last_good"]["source_date"]["mismatch"])
        wbs = self.root / "wbs.json"
        self.write(wbs, json.dumps({"schema": 1, "lane": "L", "repo": "r", "handoff": "h", "generated_at": iso,
                                   "done_hidden": 0, "cards": []}))
        os.utime(wbs, (mtime, mtime))
        state = ProjectState({"id": "wbs", "name": "WBS", "adapter": "wbs-cards", "source": str(wbs)})
        state.check()
        self.assertFalse(state.snapshot()["last_good"]["source_date"]["mismatch"])

    def test_local_date_conversion_failures_do_not_fail_source_loading(self):
        for field in ("declared_local_date", "file_local_date"):
            for error in (OSError, OverflowError, ValueError, TypeError):
                with self.subTest(field=field, error=error.__name__):
                    state = ProjectState(self.project)
                    with mock.patch("server.datetime", wraps=datetime) as dates:
                        if field == "declared_local_date":
                            declared = mock.Mock(tzinfo=timezone.utc)
                            declared.astimezone.side_effect = error("Unrepresentable local date")
                            dates.fromisoformat.return_value = declared
                        else:
                            dates.fromtimestamp.side_effect = error("Unrepresentable file date")
                        state.check()
                    snapshot = state.snapshot()
                    self.assertIsNone(snapshot["error"])
                    self.assertIsNotNone(snapshot["last_good"])
                    info = snapshot["last_good"]["source_date"]
                    self.assertIsNone(info[field])
                    self.assertIsNone(info["mismatch"])
                    other = "file_local_date" if field == "declared_local_date" else "declared_local_date"
                    self.assertIsNotNone(info[other])

    def test_background_fetch_does_not_block_source_watch_and_refreshes_immediately(self):
        started, release, refreshed = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def runner(command, **kwargs):
            calls.append(json.loads(kwargs["input"])["query"])
            if len(calls) == 1:
                started.set()
                release.wait(10)
            else:
                refreshed.set()
            return response({"pr_6": pull(), "pr_7": pull(7)})

        httpd = create_server({"projects": [self.project]}, port=0, gh_runner=runner)
        try:
            self.assertTrue(started.wait(5))
            before = httpd.board.revisions["fixture"]
            self.data["tasks"][0].update(title="監視が継続", pr=[6, 7])
            self.save()
            with httpd.board.condition:
                self.assertTrue(httpd.board.condition.wait_for(lambda: httpd.board.revisions["fixture"] > before, timeout=5))
            self.assertEqual(self.task(httpd.board)["title"], "監視が継続")
            self.assertTrue(httpd.board.pr_worker.is_alive())
            release.set()
            self.assertTrue(refreshed.wait(5), "A source change during a slow request must queue an immediate fetch")
            self.assertIn("pr_7: pullRequest(number: 7)", calls[-1])
        finally:
            release.set()
            httpd.server_close()
        self.assertFalse(httpd.board.watcher.is_alive())
        self.assertFalse(httpd.board.pr_worker.is_alive())

    def test_pr_only_change_increments_revision_and_reaches_sse_subscriber(self):
        runner = mock.Mock(return_value=response({"pr_6": pull()}))
        # Drive the real cycle explicitly so only the PR result changes after subscription.
        with mock.patch.object(Board, "refresh_prs", return_value=None):
            httpd = create_server({"projects": [self.project]}, port=0, gh_runner=runner)
        serving = threading.Thread(target=httpd.serve_forever)
        serving.start()
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
        stream = None
        try:
            board = httpd.board
            board.refresh_pr_cycle()
            before = board.projects["fixture"].snapshot()
            notified_before = board.revisions["fixture"]
            connection.request("GET", "/events")
            stream = connection.getresponse()
            self.assertEqual(stream.status, 200)
            self.assertEqual(stream.readline(), b": connected\n")
            self.assertEqual(stream.readline(), b"\n")
            runner.return_value = response({"pr_6": pull(state="MERGED")})
            board.refresh_pr_cycle()
            after = board.projects["fixture"].snapshot()
            self.assertEqual(after["last_good"]["loaded_at"], before["last_good"]["loaded_at"])
            self.assertEqual(after["source_mtime"], before["source_mtime"])
            self.assertGreater(after["pr_revision"], before["pr_revision"])
            self.assertGreater(board.revisions["fixture"], notified_before,
                               "PR-only changes must notify SSE subscribers")
            self.assertEqual(stream.readline(), b"event: project\n")
            self.assertEqual(json.loads(stream.readline().decode().removeprefix("data: ")), "fixture")
            self.assertEqual(stream.readline(), b"\n")
            self.assertEqual(self.task(board)["prs"][0]["data"]["state"], "MERGED")
            self.assertTrue(self.task(board)["pr_flags"])
        finally:
            if stream:
                stream.close()
            connection.close()
            httpd.shutdown()
            serving.join(5)
            httpd.server_close()
        self.assertFalse(serving.is_alive())


if __name__ == "__main__":
    unittest.main()
