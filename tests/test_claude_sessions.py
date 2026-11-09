from datetime import datetime, timedelta, timezone
import hashlib
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


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/live-board/scripts"))

from adapters import claude_sessions, quote_file_value, session_project_id, session_project_path
from server import Board, ProjectState, create_server, project_configs


ABSOLUTE_ROOT = "C:/" if os.name == "nt" else "/"


def absolute_path(relative):
    return ABSOLUTE_ROOT + relative


class ClaudeSessionsTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "sessions"
        self.folder.mkdir()
        self.project = {"id": "sessions", "name": "Claude Code sessions", "adapter": "claude-sessions", "source": str(self.folder)}
        self.now = datetime.now(timezone.utc)

    def write(self, session_id="abcdefgh-session", **extra):
        stamp = self.now.isoformat()
        record = {"schema": 1, "session_id": session_id, "cwd": absolute_path("work/alpha/subfolder"),
                  "project": absolute_path("work/alpha"), "started_at": stamp, "updated_at": stamp,
                  "ended_at": None, "waiting_for_user": False, "last_prompt": "依頼内容", "tasks": [], **extra}
        (self.folder / f"{session_id}.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8", newline="\n")
        return record

    def task(self, status="in_progress", task_id="1", text="実装"):
        return {"id": task_id, "text": text, "status": status}

    def test_group_by_project_across_sessions_and_distinguish_same_folder_names(self):
        self.write(tasks=[self.task()])
        self.write("second", cwd=absolute_path("work/alpha/other"), tasks=[self.task(text="確認")])
        self.write("third", cwd=absolute_path("elsewhere/alpha"), project=absolute_path("elsewhere/alpha"), tasks=[self.task()])
        board = Board({"projects": [self.project]})
        alpha_path = absolute_path("work/alpha")
        other_path = absolute_path("elsewhere/alpha")
        if os.name == "nt":
            alpha_path, other_path = alpha_path.lower(), other_path.lower()
        alpha_id = "alpha-" + hashlib.sha256(alpha_path.encode("utf-8")).hexdigest()[:6]
        other_id = "alpha-" + hashlib.sha256(other_path.encode("utf-8")).hexdigest()[:6]
        self.assertEqual(set(board.projects), {alpha_id, other_id})
        self.assertEqual([project.config["name"] for project in board.projects.values()], ["alpha", "alpha"])
        result = board.projects[alpha_id].snapshot()["last_good"]
        self.assertEqual(len(result["rows"]["phases"]), 1)
        tasks = result["rows"]["tasks"]
        self.assertEqual([task["phase"] for task in tasks], ["alpha", "alpha"])
        self.assertEqual(len({task["id"] for task in tasks}), 2)
        self.assertEqual(tasks[0]["note"], "abcdefgh")
        self.assertEqual(tasks[1]["note"], "second")
        self.assertEqual(result["warnings"] + result["style_warnings"], [])
        self.assertEqual(result["rows"]["versions"], [])

    def test_stable_url_safe_ids_and_normalized_folder_grouping(self):
        normalized = absolute_path("work/alpha")
        value = absolute_path("work/My Project_日本")
        canonical_value = value
        if os.name == "nt":
            normalized, canonical_value = normalized.lower(), canonical_value.lower()
        self.assertEqual(session_project_path(absolute_path("work/alpha/../alpha/")), normalized)
        self.assertEqual(session_project_id(absolute_path("work/alpha/")), session_project_id(normalized))
        expected = "my-project_---" + hashlib.sha256(canonical_value.encode("utf-8")).hexdigest()[:6]
        self.assertEqual(session_project_id(value), expected)
        self.assertRegex(session_project_id(value), r"^[a-z0-9_-]+-[0-9a-f]{6}$")
        self.write()
        self.write("second", project=absolute_path("work/alpha/../alpha/"), cwd=absolute_path("work/alpha/other"))
        board = Board({"projects": [self.project]})
        self.assertEqual(len(board.projects), 1)
        self.assertEqual(next(iter(board.projects.values())).summary()["total"], 2)

    @unittest.skipUnless(os.name == "nt", "Windows path rules require Windows")
    def test_windows_case_separators_and_drive_paths(self):
        backslashes = absolute_path("WORK/alpha/").replace("/", "\\")
        self.assertEqual(session_project_path(backslashes), absolute_path("work/alpha").lower())
        self.assertEqual(session_project_id(backslashes), session_project_id(absolute_path("work/alpha")))
        self.write()
        self.write("second", project=backslashes)
        board = Board({"projects": [self.project]})
        self.assertEqual(len(board.projects), 1)
        self.assertEqual(next(iter(board.projects.values())).summary()["total"], 2)
        provider = {**self.project, "hidden": [backslashes]}
        self.assertEqual(project_configs({"projects": [provider]}), [provider])
        self.assertEqual(Board({"projects": [provider]}).projects, {})
        with self.assertRaisesRegex(ValueError, "hidden は絶対パスの文字列の配列にしてください"):
            project_configs({"projects": [{**self.project, "hidden": ["C:alpha"]}]})

    @unittest.skipUnless(os.name != "nt", "POSIX paths preserve case")
    def test_posix_case_is_preserved_in_grouping_ids_and_hidden_paths(self):
        upper = absolute_path("work/Alpha")
        lower = absolute_path("work/alpha")
        self.assertEqual(session_project_path(upper + "/../Alpha/"), upper)
        self.assertEqual(session_project_id(upper), "alpha-" + hashlib.sha256(upper.encode("utf-8")).hexdigest()[:6])
        self.assertNotEqual(session_project_id(upper), session_project_id(lower))
        self.write("upper", project=upper)
        self.write("lower", project=lower)
        board = Board({"projects": [self.project]})
        self.assertEqual(set(board.projects), {session_project_id(upper), session_project_id(lower)})
        self.assertTrue(all(state.summary()["total"] == 1 for state in board.projects.values()))
        provider = {**self.project, "hidden": [lower + "/../alpha/"]}
        self.assertEqual(project_configs({"projects": [provider]}), [provider])
        visible = Board({"projects": [provider]})
        self.assertEqual(set(visible.projects), {session_project_id(upper)})
        self.assertEqual(visible.projects[session_project_id(upper)].summary()["total"], 1)

    def test_overlapping_sources_merge_sessions_and_refresh_as_one_project(self):
        self.write("first", tasks=[self.task()])
        second = self.root / "other-sessions"
        second.mkdir()
        provider = {**self.project, "id": "other", "source": str(second)}
        board = Board({"projects": [self.project, provider]})
        state = next(iter(board.projects.values()))
        record = self.write("second", project=absolute_path("work/alpha/../alpha/"), tasks=[self.task(text="Second")])
        (self.folder / "second.json").unlink()
        (second / "second.json").write_text(json.dumps(record), encoding="utf-8", newline="\n")
        try:
            board.refresh_projects()
        except ValueError as error:
            self.fail(f"Overlapping session sources must merge: {error}")
        self.assertEqual(len(board.projects), 1)
        self.assertIs(next(iter(board.projects.values())), state)
        self.assertEqual({task["id"] for task in state.snapshot()["last_good"]["rows"]["tasks"]},
                         {"first:1", "second:1"})
        self.assertEqual(state.summary()["total"], 2)
        restarted = Board({"projects": [self.project, provider]})
        self.assertEqual(next(iter(restarted.projects.values())).summary()["total"], 2)
        (self.folder / "first.json").unlink()
        board.refresh_projects()
        self.assertEqual(len(board.projects), 1)
        self.assertEqual(state.summary()["total"], 1)

    def test_same_source_listed_twice_does_not_duplicate_sessions(self):
        self.write(tasks=[self.task()])
        try:
            board = Board({"projects": [self.project, {**self.project, "id": "alias"}]})
        except ValueError as error:
            self.fail(f"Overlapping session sources must merge: {error}")
        self.assertEqual(len(board.projects), 1)
        self.assertEqual(next(iter(board.projects.values())).summary()["total"], 1)

    def session_provider(self, name, **settings):
        folder = self.root / name
        folder.mkdir()
        return {**self.project, "id": name, "name": name, "source": str(folder), **settings}

    def write_provider(self, provider, session_id, **extra):
        record = self.write(session_id, **extra)
        (self.folder / f"{session_id}.json").replace(Path(provider["source"]) / f"{session_id}.json")

    def test_failed_source_preserves_only_its_folders_while_siblings_update(self):
        import adapters

        alpha = self.session_provider("alpha")
        beta = self.session_provider("beta")
        for failure in (OSError("unreadable"), ValueError("invalid source")):
            with self.subTest(failure=type(failure).__name__):
                self.write_provider(alpha, "alpha", project=absolute_path("work/alpha"), last_prompt="Before")
                self.write_provider(beta, "beta", project=absolute_path("work/beta"), last_prompt="Last good")
                board = Board({"projects": [alpha, beta]})
                alpha_state = board.projects[session_project_id(absolute_path("work/alpha"))]
                beta_state = board.projects[session_project_id(absolute_path("work/beta"))]
                before = beta_state.snapshot()["last_good"]
                self.write_provider(alpha, "alpha", project=absolute_path("work/alpha"), last_prompt="Updated")
                self.write_provider(alpha, "new", project=absolute_path("work/new"))
                read = adapters.read_sessions

                def read_source(source):
                    if source == beta["source"]:
                        raise failure
                    return read(source)

                with mock.patch("adapters.read_sessions", side_effect=read_source):
                    board.refresh_projects()
                    started = Board({"projects": [alpha, beta]})
                    self.assertEqual(set(started.projects), {session_project_id(absolute_path("work/alpha")),
                                                             session_project_id(absolute_path("work/new"))})
                    self.assertTrue(all(state.snapshot()["error"] is None for state in started.projects.values()))
                self.assertIsNone(alpha_state.snapshot()["error"])
                self.assertEqual(alpha_state.snapshot()["last_good"]["rows"]["tasks"][0]["title"], "Updated")
                self.assertIsNotNone(beta_state.snapshot()["error"])
                self.assertEqual(beta_state.snapshot()["last_good"], before)
                self.assertIs(board.projects[beta_state.config["id"]], beta_state)
                self.assertIsNone(board.projects[session_project_id(absolute_path("work/new"))].snapshot()["error"])
                board.refresh_projects()
                self.assertIsNone(beta_state.snapshot()["error"])

    def test_shared_folder_keeps_last_good_until_failed_provider_recovers(self):
        providers = [self.session_provider(name) for name in ("A", "B")]
        stamp = self.now.isoformat()
        records = {provider["source"]: [{
            "session_id": provider["id"], "project": absolute_path("work/shared"),
            "updated_at": stamp, "ended_at": None, "waiting_for_user": False,
            "last_prompt": provider["id"], "tasks": [],
        }] for provider in providers}
        shared_id = session_project_id(absolute_path("work/shared"))
        for failure in (OSError("unreadable"), ValueError("invalid source")):
            failed = False
            alpha_records = records[providers[0]["source"]]

            def read_source(source):
                if failed and source == providers[1]["source"]:
                    raise failure
                return records[source], []

            with self.subTest(failure=type(failure).__name__), \
                    mock.patch("adapters.read_sessions", side_effect=read_source):
                board = Board({"projects": providers})
                state = board.projects[shared_id]
                before = state.snapshot()["last_good"]
                self.assertEqual([task["id"] for task in before["rows"]["tasks"]],
                                 ["A:prompt", "B:prompt"])
                failed = True
                board.refresh_projects()
                with self.subTest(phase="partial read"):
                    self.assertEqual(state.snapshot()["last_good"], before)
                    self.assertIsNotNone(state.snapshot()["error"])
                    self.assertEqual(state.config["session_providers"], providers)
                records[providers[0]["source"]] = []
                board.refresh_projects()
                with self.subTest(phase="healthy records disappear"):
                    self.assertEqual(list(board.projects), [shared_id])
                    self.assertIs(board.projects[shared_id], state)
                    self.assertEqual(state.snapshot()["last_good"], before)
                    self.assertIsNotNone(state.snapshot()["error"])
                    self.assertEqual(state.config["session_providers"], providers)
                failed = False
                board.refresh_projects()
                with self.subTest(phase="recovery"):
                    self.assertEqual(list(board.projects), [shared_id])
                    self.assertIs(board.projects[shared_id], state)
                    self.assertIsNone(state.snapshot()["error"])
                    self.assertEqual([task["id"] for task in state.snapshot()["last_good"]["rows"]["tasks"]],
                                     ["B:prompt"])
                    self.assertEqual(state.config["session_providers"], [providers[1]])
            records[providers[0]["source"]] = alpha_records

    def test_direct_shared_folder_check_reports_any_failed_provider(self):
        providers = [self.session_provider(name) for name in ("A", "B")]
        record = {"session_id": "A", "project": absolute_path("work/shared"), "updated_at": self.now.isoformat(),
                  "ended_at": None, "waiting_for_user": False, "last_prompt": "A", "tasks": []}
        config = {**providers[0], "session_project": session_project_path(record["project"]),
                  "session_providers": providers}
        for failure in (OSError("unreadable"), ValueError("invalid source")):
            def read_source(source):
                if source == providers[1]["source"]:
                    raise failure
                return [record], []

            with self.subTest(failure=type(failure).__name__), \
                    mock.patch("adapters.read_sessions", side_effect=read_source):
                with self.assertRaises(type(failure)) as raised:
                    claude_sessions(config)
                self.assertIs(raised.exception, failure)

    def test_folder_configs_and_direct_checks_use_their_own_providers(self):
        import adapters

        providers = [self.session_provider(name, activity_dir=str(self.root / f"{name}-activity"),
                                           github_repo=f"example/{name}", hidden=[absolute_path(f"hidden/{name}")],
                                           custom_setting=name)
                     for name in ("alpha", "beta")]
        for provider in providers:
            self.write_provider(provider, provider["id"], project=absolute_path(f"work/{provider['id']}"))
        board = Board({"projects": providers})
        for provider in providers:
            with self.subTest(provider=provider["id"]):
                state = board.projects[session_project_id(absolute_path(f"work/{provider['id']}"))]
                for key in ("activity_dir", "github_repo", "hidden", "custom_setting", "source"):
                    self.assertEqual(state.config[key], provider[key])
                self.assertEqual(state.config["name"], provider["id"])
                self.assertEqual(state.config["session_providers"], [provider])
                with mock.patch("adapters.read_sessions", wraps=adapters.read_sessions) as read, \
                        mock.patch("server.read_activity", return_value=([], [])) as activity:
                    state.check(session_result=None)
                read.assert_called_once_with(provider["source"])
                activity.assert_called_once_with(provider["activity_dir"])
                self.assertEqual(state.pr_request()[0], provider["github_repo"])

    def test_shared_folder_keeps_first_owner_and_newest_record_per_session(self):
        alpha = self.session_provider("alpha", github_repo="example/alpha")
        beta = self.session_provider("beta", github_repo="example/beta")
        self.write_provider(alpha, "shared", last_prompt="Older")
        self.write_provider(beta, "shared", project=absolute_path("work/alpha/../alpha/"), last_prompt="Newest",
                            updated_at=(self.now + timedelta(seconds=1)).isoformat())
        self.write_provider(beta, "unique", last_prompt="Beta only")
        board = Board({"projects": [alpha, beta]})
        self.assertEqual(len(board.projects), 1)
        state = next(iter(board.projects.values()))
        self.assertEqual(state.config["github_repo"], alpha["github_repo"])
        self.assertEqual(state.config["session_providers"], [alpha, beta])
        tasks = state.snapshot()["last_good"]["rows"]["tasks"]
        self.assertEqual({task["id"]: task["title"] for task in tasks},
                         {"shared:prompt": "Newest", "unique:prompt": "Beta only"})
        state.check()
        self.assertIsNone(state.snapshot()["error"])
        self.assertEqual(state.summary()["total"], 2)

    def test_all_failed_sources_keep_every_previous_folder_with_last_good_and_error(self):
        alpha = self.session_provider("alpha")
        beta = self.session_provider("beta")
        self.write_provider(alpha, "alpha", project=absolute_path("work/alpha"))
        self.write_provider(beta, "beta", project=absolute_path("work/beta"))
        board = Board({"projects": [alpha, beta]})
        before = {key: state.snapshot()["last_good"] for key, state in board.projects.items()}
        with mock.patch("adapters.read_sessions", side_effect=OSError("unreadable")):
            board.refresh_projects()
        self.assertEqual(set(board.projects), set(before))
        for key, state in board.projects.items():
            self.assertEqual(state.snapshot()["last_good"], before[key])
            self.assertIsNotNone(state.snapshot()["error"])
        board.refresh_projects()
        self.assertTrue(all(state.snapshot()["error"] is None for state in board.projects.values()))

    def test_failed_source_does_not_keep_a_disappeared_healthy_folder(self):
        import adapters

        alpha = self.session_provider("alpha")
        beta = self.session_provider("beta")
        self.write_provider(alpha, "alpha", project=absolute_path("work/alpha"))
        self.write_provider(beta, "beta", project=absolute_path("work/beta"))
        board = Board({"projects": [alpha, beta]})
        (Path(alpha["source"]) / "alpha.json").unlink()
        read = adapters.read_sessions

        def read_source(source):
            if source == beta["source"]:
                raise OSError("unreadable")
            return read(source)

        with mock.patch("adapters.read_sessions", side_effect=read_source):
            board.refresh_projects()
        self.assertEqual(set(board.projects), {session_project_id(absolute_path("work/beta"))})
        self.assertIsNotNone(next(iter(board.projects.values())).snapshot()["error"])

    def test_shared_folder_refreshes_its_owner_and_contributing_providers(self):
        import adapters

        alpha = self.session_provider("alpha", github_repo="example/alpha")
        beta = self.session_provider("beta", github_repo="example/beta")
        self.write_provider(alpha, "alpha")
        board = Board({"projects": [alpha, beta]})
        state = next(iter(board.projects.values()))
        self.write_provider(beta, "beta")
        board.refresh_projects()
        self.assertEqual(state.config["session_providers"], [alpha, beta])
        before = state.snapshot()["last_good"]
        (Path(alpha["source"]) / "alpha.json").unlink()
        read = adapters.read_sessions

        def read_source(source):
            if source == beta["source"]:
                raise OSError("unreadable")
            return read(source)

        with mock.patch("adapters.read_sessions", side_effect=read_source):
            board.refresh_projects()
        self.assertEqual(len(board.projects), 1)
        self.assertEqual(state.snapshot()["last_good"], before)
        self.assertIsNotNone(state.snapshot()["error"])
        board.refresh_projects()
        self.assertEqual(state.config["github_repo"], beta["github_repo"])
        self.assertEqual(state.config["session_providers"], [beta])
        self.assertIsNone(state.snapshot()["error"])
        # A failed old owner must no longer affect a direct check of this folder.
        with mock.patch("adapters.read_sessions", wraps=read) as reads:
            state.check()
        reads.assert_called_once_with(beta["source"])

    def test_hidden_records_do_not_claim_folder_ownership(self):
        alpha = self.session_provider("alpha", hidden=[absolute_path("work/alpha")], github_repo="example/alpha")
        beta = self.session_provider("beta", github_repo="example/beta")
        self.write_provider(alpha, "alpha")
        self.write_provider(beta, "beta")
        board = Board({"projects": [alpha, beta]})
        self.assertEqual(len(board.projects), 1)
        state = next(iter(board.projects.values()))
        self.assertEqual(state.config["github_repo"], beta["github_repo"])
        self.assertEqual(state.config["session_providers"], [beta])
        self.assertEqual(state.snapshot()["last_good"]["rows"]["tasks"][0]["id"], "beta:prompt")

    def test_hidden_folders_use_normalized_paths_and_leave_other_projects_visible(self):
        self.write(tasks=[self.task()])
        self.write("same-folder", project=absolute_path("work/alpha/../alpha/"), tasks=[self.task()])
        self.write("sibling", project=absolute_path("work/alpha-other"))
        self.write("same-name", project=absolute_path("elsewhere/alpha"))
        configured = {"id": "configured", "name": "Configured", "adapter": "wbs-cards",
                      "source": str(self.root / "wbs.json")}
        provider = {**self.project, "hidden": [absolute_path("work/alpha/../alpha/"), absolute_path("work/missing")]}
        board = Board({"projects": [configured, provider]})
        expected = {"configured", session_project_id(absolute_path("work/alpha-other")),
                    session_project_id(absolute_path("elsewhere/alpha"))}
        self.assertEqual(set(board.projects), expected)
        self.write("later", project=absolute_path("work/alpha"))
        board.refresh_projects()
        self.assertEqual(set(board.projects), expected)
        self.assertEqual(len(list(self.folder.glob("*.json"))), 5)
        restored = Board({"projects": [{**self.project, "hidden": []}]})
        self.assertIn(session_project_id(absolute_path("work/alpha")), restored.projects)
        self.assertEqual(len(restored.projects), 3)

    def test_hidden_requires_a_list_of_absolute_path_strings(self):
        for hidden in (None, False, 42, absolute_path("work/alpha"), {}, [None], [False], [42], [[]],
                       [""], ["alpha"], [absolute_path("work/alpha"), "relative"]):
            with self.subTest(hidden=hidden):
                with self.assertRaisesRegex(ValueError, "hidden は絶対パスの文字列の配列にしてください"):
                    project_configs({"projects": [{**self.project, "hidden": hidden}]})
        for hidden in ([], [absolute_path("work/alpha")], [absolute_path("work/alpha/../alpha/"), absolute_path("work/beta")]):
            with self.subTest(hidden=hidden):
                project = {**self.project, "hidden": hidden}
                self.assertEqual(project_configs({"projects": [project]}), [project])

    def test_refresh_adds_removes_and_orders_folders_without_restart(self):
        self.write("old-active", project=absolute_path("work/alpha"), updated_at=(self.now - timedelta(hours=2)).isoformat())
        self.write("new-ended", project=absolute_path("work/beta"), ended_at=self.now.isoformat())
        board = Board({"projects": [self.project]})
        alpha, beta, gamma = [session_project_id(absolute_path(f"work/{name}")) for name in ("alpha", "beta", "gamma")]
        self.assertEqual(list(board.projects), [alpha, beta])
        original = board.projects[alpha]
        self.write("new-active", project=absolute_path("work/gamma"), updated_at=(self.now - timedelta(hours=1)).isoformat())
        revision = board.catalog_revision
        board.refresh_projects()
        self.assertEqual(list(board.projects), [gamma, alpha, beta])
        self.assertIs(board.projects[alpha], original)
        self.assertGreater(board.catalog_revision, revision)
        (self.folder / "new-active.json").unlink()
        board.refresh_projects()
        self.assertEqual(list(board.projects), [alpha, beta])
        with mock.patch("adapters.datetime", wraps=datetime) as dates:
            dates.now.return_value = self.now + timedelta(days=8)
            board.refresh_projects()
        self.assertEqual(set(board.projects), {alpha})
        self.assertEqual(set(board.revisions), {alpha})

    def test_folder_fingerprint_dates_and_history_are_independent_and_persisted(self):
        self.write(tasks=[self.task()])
        self.write("beta", project=absolute_path("work/beta"), tasks=[self.task()])
        history = self.root / "history.json"
        board = Board({"projects": [self.project]}, history_path=history)
        alpha_id = session_project_id(absolute_path("work/alpha"))
        alpha = board.projects[alpha_id]
        original = alpha.snapshot()["last_good"]
        self.assertFalse(original["source_date"]["mismatch"])
        self.assertEqual(original["source_mtime"], self.now.timestamp())
        fingerprint = alpha.content_hash
        self.write("beta", project=absolute_path("work/beta"), tasks=[self.task(text="別フォルダの変更")],
                   updated_at=(self.now + timedelta(seconds=1)).isoformat())
        board.refresh_projects()
        self.assertEqual(alpha.content_hash, fingerprint)
        self.assertEqual(alpha.snapshot()["last_good"], original)
        self.write(tasks=[self.task(text="変更した課題")], updated_at=(self.now + timedelta(seconds=2)).isoformat())
        board.refresh_projects()
        changed = alpha.snapshot()["last_good"]["rows"]["tasks"][0]
        self.assertFalse(changed["description_baseline"])
        self.assertEqual(changed["description_changed_at"], (self.now + timedelta(seconds=2)).isoformat())
        restarted = Board({"projects": [self.project]}, history_path=history)
        self.assertEqual(restarted.projects[alpha_id].snapshot()["last_good"]["rows"]["tasks"][0], changed)
        self.assertEqual(set(json.loads(history.read_text(encoding="utf-8"))), set(board.projects))

    def test_mapping_hidden_completed_and_summary_counts(self):
        self.write(tasks=[self.task("pending", "1"), self.task("in_progress", "2"), self.task("completed", "3")])
        state = ProjectState(self.project)
        state.check()
        self.assertIsNone(state.snapshot()["error"])
        tasks = state.snapshot()["last_good"]["rows"]["tasks"]
        self.assertEqual([task["status"] for task in tasks], ["todo", "doing"])
        self.assertEqual([task["raw_status"] for task in tasks], ["p-hold", "p-run"])
        self.assertEqual(state.summary()["counts"], {"todo": 1, "doing": 1, "blocked": 0, "done": 1})
        self.assertEqual(state.summary()["total"], 3)
        self.assertEqual(state.summary()["done_hidden"], 1)
        self.assertNotIn("source_fields", tasks[0])

    def test_waiting_only_changes_in_progress_tasks(self):
        self.write(waiting_for_user=True, tasks=[self.task("pending", "1"), self.task("in_progress", "2")])
        state = ProjectState(self.project)
        state.check()
        tasks = state.snapshot()["last_good"]["rows"]["tasks"]
        self.assertEqual([(task["status"], task["raw_status"]) for task in tasks], [("todo", "p-hold"), ("blocked", "p-owner")])
        self.assertEqual(state.summary()["owner_wait"], 1)
        self.assertEqual(state.snapshot()["last_good"]["rows"]["phases"][0]["status"], "blocked")

    def test_waiting_prompt_only_session_counts_as_owner_wait(self):
        self.write(waiting_for_user=True)
        board = Board({"projects": [self.project]})
        state = next(iter(board.projects.values()))
        tasks = state.snapshot()["last_good"]["rows"]["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual((tasks[0]["status"], tasks[0]["raw_status"]), ("blocked", "p-owner"))
        self.assertEqual(state.summary()["owner_wait"], 1)
        self.assertEqual(state.summary()["counts"]["blocked"], 1)

    def test_prompt_only_active_sessions_and_ended_empty_session(self):
        self.write(waiting_for_user=True)
        self.write("empty", last_prompt="")
        self.write("ended", ended_at=self.now.isoformat())
        tasks = claude_sessions(self.project)["rows"]["tasks"]
        self.assertEqual([task["title"] for task in tasks], ["依頼内容", "作業中のセッション", "依頼内容"])
        self.assertEqual([task["status"] for task in tasks], ["blocked", "doing", "todo"])
        self.assertEqual(tasks[-1]["label"], "休止中")

    def test_ended_session_pauses_only_in_progress_and_overrides_waiting(self):
        self.write(ended_at=self.now.isoformat(), waiting_for_user=True,
                   tasks=[self.task("pending", "1"), self.task("in_progress", "2"), self.task("completed", "3")])
        state = ProjectState(self.project)
        state.check()
        tasks = state.snapshot()["last_good"]["rows"]["tasks"]
        self.assertEqual([(task["status"], task["raw_status"], task["label"]) for task in tasks],
                         [("todo", "p-hold", ""), ("todo", "p-hold", "休止中")])
        self.assertEqual(state.summary()["owner_wait"], 0)
        self.assertEqual(state.summary()["counts"], {"todo": 2, "doing": 0, "blocked": 0, "done": 1})
        self.write(ended_at=self.now.isoformat(), last_prompt="")
        state.check()
        task = state.snapshot()["last_good"]["rows"]["tasks"][0]
        self.assertEqual((task["title"], task["status"], task["label"]), ("作業中のセッション", "todo", "休止中"))

    def test_recent_ended_and_running_sessions_stay_but_old_ended_are_hidden(self):
        self.write("recent", ended_at=(self.now - timedelta(hours=23)).isoformat(), tasks=[self.task()])
        self.write("old", ended_at=(self.now - timedelta(days=1, seconds=1)).isoformat(), tasks=[self.task()])
        self.write("stale", updated_at=(self.now - timedelta(days=7, seconds=1)).isoformat(), tasks=[self.task()])
        self.write("active", updated_at=(self.now - timedelta(days=6)).isoformat(), tasks=[self.task()])
        tasks = claude_sessions(self.project)["rows"]["tasks"]
        self.assertEqual([task["id"] for task in tasks], ["active:1", "recent:1", "stale:1"])
        self.assertEqual(len(list(self.folder.glob("*.json"))), 4)

    def test_expiry_boundaries_and_no_file_change(self):
        self.write("ended", ended_at=(self.now - timedelta(days=1)).isoformat(), tasks=[self.task()])
        self.write("active", updated_at=(self.now - timedelta(days=7)).isoformat(), tasks=[self.task()])
        state = ProjectState(self.project)
        with mock.patch("adapters.datetime", wraps=datetime) as dates:
            dates.now.return_value = self.now
            state.check()
            self.assertEqual(state.summary()["total"], 2)
            dates.now.return_value = self.now + timedelta(seconds=1)
            self.assertTrue(state.check())
            self.assertEqual(state.summary()["total"], 1)

    def test_running_sessions_never_expire_by_age(self):
        self.write("running", started_at=(self.now - timedelta(days=60)).isoformat(),
                   updated_at=(self.now - timedelta(days=30)).isoformat(), tasks=[self.task()])
        board = Board({"projects": [self.project]})
        self.assertEqual(len(board.projects), 1)
        state = next(iter(board.projects.values()))
        self.assertEqual(state.summary()["total"], 1)
        with mock.patch("adapters.datetime", wraps=datetime) as dates:
            dates.now.return_value = self.now + timedelta(days=90)
            board.refresh_projects()
        self.assertIs(next(iter(board.projects.values())), state)
        self.assertEqual(state.summary()["total"], 1)

    def test_missing_source_is_empty_and_hooks_can_populate_it(self):
        self.folder.rmdir()
        state = ProjectState(self.project)
        state.check()
        self.assertIsNone(state.snapshot()["error"])
        self.assertEqual(state.summary()["total"], 0)
        self.assertFalse(self.folder.exists())
        event = {"hook_event_name": "UserPromptSubmit", "session_id": "end-to-end", "cwd": str(self.root), "prompt": "自動で表示"}
        result = subprocess.run([sys.executable, str(ROOT / "skills/live-board/scripts/tools/session_hook.py"), str(self.root)],
                                input=json.dumps(event), text=True, capture_output=True, timeout=5, cwd=ROOT)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertTrue(state.check())
        self.assertEqual(state.snapshot()["last_good"]["rows"]["tasks"][0]["title"], "自動で表示")

    def test_reload_child_without_directory_mtime_change_and_delete(self):
        self.write(tasks=[self.task()])
        state = ProjectState(self.project)
        state.check()
        before = self.folder.stat()
        self.write(tasks=[self.task(text="更新した課題")])
        os.utime(self.folder, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertTrue(state.check())
        self.assertEqual(state.snapshot()["last_good"]["rows"]["tasks"][0]["title"], "更新した課題")
        revision = state.source_revision
        state.check()
        self.assertEqual(state.source_revision, revision)
        (self.folder / "abcdefgh-session.json").unlink()
        self.assertTrue(state.check())
        self.assertEqual(state.summary()["total"], 0)

    def test_malformed_record_does_not_hide_valid_sessions_and_warnings_are_quoted(self):
        self.write()
        invalid = self.folder / "broken.json"
        invalid.write_text("{", encoding="utf-8", newline="\n")
        result = claude_sessions(self.project)
        self.assertEqual(len(result["rows"]["tasks"]), 1)
        self.assertIn(quote_file_value(invalid.name), result["warnings"][0])
        self.assertEqual(len(result["warnings"][0].splitlines()), 1)
        for patch in ({"tasks": [self.task("unknown")]}, {"updated_at": "yesterday"}, {"ended_at": 123},
                      {"tasks": None}, {"schema": True}, {"waiting_for_user": "yes"}, {"cwd": None},
                      {"updated_at": "2026-11-09T00:00:00"}):
            with self.subTest(patch=patch):
                self.write("bad", **patch)
                self.assertEqual(len(claude_sessions(self.project)["rows"]["tasks"]), 1)

    def test_source_file_is_reported_as_error(self):
        self.project["source"] = str(self.root / "source.json")
        Path(self.project["source"]).write_text("{}", encoding="utf-8", newline="\n")
        state = ProjectState(self.project)
        state.check()
        self.assertIsNotNone(state.snapshot()["error"])

    def test_server_loads_config_and_serves_sessions(self):
        self.write(tasks=[self.task(), self.task("completed", "2")])
        config_path = self.root / "projects.json"
        config_path.write_text(json.dumps({"projects": [self.project]}), encoding="utf-8", newline="\n")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(project_configs(config), [self.project])
        httpd = create_server(config, port=0)
        thread = threading.Thread(target=httpd.serve_forever)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
            try:
                connection.request("GET", "/api/summary")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read())
                self.assertEqual(payload[0]["counts"]["doing"], 1)
                self.assertEqual(payload[0]["counts"]["done"], 1)
                self.assertIsNone(payload[0]["error"])
            finally:
                connection.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(httpd.board.watcher.is_alive())
        self.assertFalse(httpd.board.pr_worker.is_alive())


if __name__ == "__main__":
    unittest.main()
