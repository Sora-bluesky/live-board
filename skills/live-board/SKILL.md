---
name: live-board
description: Open or check a local read-only progress board for project backlogs, WBS JSON and GitHub PR status. Use when the user wants to view project progress, manage board projects, or check the board's local connection and source status.
---

# live-board

1. Require Python 3.13+ with PyYAML in the Python environment used to start the board. Run `python -c "import yaml"`; if it fails, run `python -m pip install pyyaml`.
2. Read the config at `${CLAUDE_PLUGIN_DATA}/projects.json`. If it is missing, create its parent folder and write a config with one default project: `{"projects": [{"id": "sessions", "name": "Claude Code sessions", "adapter": "claude-sessions", "source": "${CLAUDE_PLUGIN_DATA}/sessions"}]}`. Resolve `${CLAUDE_PLUGIN_DATA}` to its actual absolute path when writing JSON; the server does not expand variables. If the config exists but has no entry with `"adapter": "claude-sessions"`, tell the user automatic session tracking is not on the board yet and ask once in the conversation whether to add the same default entry, using a unique `id` (`sessions` unless taken). If they agree, add it while preserving every other entry and setting, write valid JSON, and read it back to verify. If they decline, do not ask again in that conversation. This entry shows session tasks recorded automatically by the plugin hooks, including prompt-only sessions. The sessions folder may not exist yet; it loads as an empty board until a hook records a session. Offer to add optional user projects using real absolute source paths and any required `build_py` path; never guess paths or copy example placeholders. Keep this config in the per-plugin data folder, which survives plugin updates.
3. Start this command as a background process using the execution tool's background facility:

   ```sh
   python "${CLAUDE_SKILL_DIR}/scripts/server.py" --config "${CLAUDE_PLUGIN_DATA}/projects.json" --port 8795
   ```

   The server binds only to `127.0.0.1`. Check `http://127.0.0.1:8795/healthz`, then open `http://127.0.0.1:8795/` in the browser pane or preview. If a board is already running on this port, check its health before starting another process. Report startup or source errors. The UI text is Japanese.
4. Choose the adapter that matches the user's source:
   - `winsmux-backlog`: read a YAML backlog through its trusted `build_py` module's `validate` and `dashboard_rows` functions.
   - `wbs-cards`: read schema 1 WBS JSON with `cards` and `done_hidden`.
   - `claude-sessions`: one config entry reads the plugin's sessions folder and expands into a separate board project for each recorded `project` folder (Git root, or working folder outside Git). The entry's id/name identify the source configuration; displayed projects use the folder name and an id formed from the lowercased folder name with characters outside `[a-z0-9_-]` replaced by `-`, followed by `-` and the first six SHA-256 hex characters of the normalized absolute path (case-normalized on Windows, `/` separators). New folders appear and vanished folders disappear on the next poll. Active folders come first, ordered by latest session update. Tasks from all sessions in a folder share that project, with an eight-character session id in each note. Completed tasks count without showing completed cards. Records older than seven days and sessions ended more than one day ago are omitted. In-progress tasks waiting for an answer appear as owner-waiting; ended sessions show their in-progress tasks as todo with the label `休止中`. Sessions without tasks show their latest prompt (or `作業中のセッション`), also as todo with `休止中` when ended. Each folder's source date uses its latest session `updated_at`; content fingerprints and card-description history are scoped to its stable project id, so unrelated folders do not reset them.
5. Explain WBS states as `p-run` (in progress), `p-hold` (not started), `p-wait` (waiting), and `p-owner` (waiting for the owner). Accept `p-sora` for older files with a warning. To complete a card, remove it from `cards` and increment `done_hidden`; do not use `status: "done"`.

Treat source files as read-only. The `build_py` setting executes that local Python script, so only point it at files the user trusts. Nothing is sent anywhere except optional GitHub PR status requests through the user's `gh` authentication. The board writes its own card-description history in `tmp/card-history.json` next to the resolved config file (`${CLAUDE_PLUGIN_DATA}/tmp/card-history.json` for the command above), or at the path specified by `--history PATH`; it never edits source files.

live-board is meant for single-user machines. While the board runs, other local accounts can read what it shows, including recorded prompt beginnings and task names, so do not run it on shared machines with private projects.

The plugin's `hooks/hooks.json` automatically records session task lists and the first 120 characters of the latest prompt in `${CLAUDE_PLUGIN_DATA}/sessions`. Common secret shapes are masked before saving. It never blocks tools or sends this data over the network. Records are removed after seven days on a subsequent hook write. Do not add these hooks to user settings; the plugin already loads them. The optional WBS validation hook remains unregistered. Only if the user asks, show how to register `${CLAUDE_SKILL_DIR}/scripts/tools/wbs_hook.py` as a Claude Code `PostToolUse` command hook: use `python "${CLAUDE_SKILL_DIR}/scripts/tools/wbs_hook.py"` with matcher `Write|Edit|MultiEdit|Bash|PowerShell`. Resolve the skill path to its actual absolute path when writing settings; do not assume skill-body substitutions work in hook settings. Explain that it checks existing files named exactly `wbs.json`, returns problems on stderr with exit 2, and otherwise returns silently with exit 0.

## Manage projects

Read `${CLAUDE_PLUGIN_DATA}/projects.json` before each request; initialize it as in step 2 if missing. For changes, edit the parsed JSON, preserve other entries and settings, write valid JSON back to that same file, and read it back to verify. Resolve all paths to absolute paths. Never edit source files.

- **"add <folder or project> to the board"**: inspect that folder's `.claude/local/wbs/wbs.json`, or the `wbs.json` or YAML backlog the user names. If found, add a `wbs-cards` or `winsmux-backlog` entry with a unique `id`, a `name`, and the absolute `source`. For YAML, obtain the trusted absolute `build_py` path required by the adapter; never invent one. Avoid duplicate entries for the same source. If nothing is found, say Claude Code sessions in that folder appear automatically only if a `claude-sessions` entry exists; otherwise offer to add the default sessions entry as in step 2.
- **"remove <project> from the board"**: remove the matching configured entry. For an automatic folder-project (also for "hide <folder>"), keep the `claude-sessions` entry and add the folder's absolute path to its optional `hidden` array; match paths using the same normalization as folder grouping above and avoid duplicates. If several session sources contain the folder, update each matching entry.
- **"show the board's projects"**: list configured entries with their ids and sources, automatic folders from valid unexpired session records using the grouping above, and paths in each `hidden` list separately. Do not change the config.
- **"show <folder> again"**: remove all normalized matches for the folder from the `hidden` arrays. Preserve other hidden folders.

After any change, explain that the running board reads config only at startup and needs a restart. If you started it, stop only the process you started, start it again with the same command, and check `/healthz` at its address; report any source errors. Otherwise tell the user to restart the existing board; do not stop a process you do not own.

## Writing cards

- Keep the title (`text` in WBS, `title` in winsmux-backlog) to the work's name, within 40 characters as counted by Python `len`. Put background, numbers and PR references in `note`.
- Keep WBS `label` to a short badge of 12 characters or fewer; put sentences in `note`.
- Write WBS `status` as exactly one of these four strings: `p-run` (in progress), `p-hold` (not started), `p-wait` (waiting), `p-owner` (waiting for the owner).
- Legacy `p-sora` and `ball: true` keep their meaning but warn; write `status: "p-owner"` instead. `ball` is optional and defaults to `false`; when present, it must be a boolean.
- WBS card keys are `code`, `section`, `text`, `note`, `status`, `label`, `ball`, `progress`; top-level keys are `schema`, `lane`, `repo`, `handoff`, `generated_at`, `done_hidden`, `cards`. Other keys warn that their content belongs in `note` because the board does not show them.

These writing warnings still allow the card to load. The WBS checker and optional hook report them too; `check_wbs.py` exits 1 on any warning.
