---
name: live-board
description: Open or check a local read-only progress board for project backlogs, WBS JSON and GitHub PR status. Use when the user wants to view project progress or check the board's local connection and source status.
---

# live-board

1. Require Python 3.13+ with PyYAML in the Python environment used to start the board. Run `python -c "import yaml"`; if it fails, run `python -m pip install pyyaml`.
2. Read the config at `${CLAUDE_PLUGIN_DATA}/projects.json`. If it is missing, create its parent folder and copy `${CLAUDE_PLUGIN_ROOT}/projects.example.json` there. Ask the user for the real absolute source paths and any required `build_py` path; never guess paths or start with the example placeholders. Keep this config in the per-plugin data folder, which survives plugin updates.
3. Start this command as a background process using the execution tool's background facility:

   ```sh
   python "${CLAUDE_SKILL_DIR}/scripts/server.py" --config "${CLAUDE_PLUGIN_DATA}/projects.json" --port 8795
   ```

   The server binds only to `127.0.0.1`. Check `http://127.0.0.1:8795/healthz`, then open `http://127.0.0.1:8795/` in the browser pane or preview. If a board is already running on this port, check its health before starting another process. Report startup or source errors. The UI text is Japanese.
4. Choose the adapter that matches the user's source:
   - `winsmux-backlog`: read a YAML backlog through its trusted `build_py` module's `validate` and `dashboard_rows` functions.
   - `wbs-cards`: read schema 1 WBS JSON with `cards` and `done_hidden`.
5. Explain WBS states as `p-run` (in progress), `p-hold` (not started), `p-wait` (waiting), and `p-owner` (waiting for the owner). Accept `p-sora` for older files with a warning. To complete a card, remove it from `cards` and increment `done_hidden`; do not use `status: "done"`.

Treat source files as read-only. The `build_py` setting executes that local Python script, so only point it at files the user trusts. Nothing is sent anywhere except optional GitHub PR status requests through the user's `gh` authentication. The board writes its own card-description history in `tmp/card-history.json` next to the resolved config file (`${CLAUDE_PLUGIN_DATA}/tmp/card-history.json` for the command above), or at the path specified by `--history PATH`; it never edits source files.

live-board is meant for single-user machines. On a machine where other people log in, their accounts can read what the board shows, so do not run it there with private projects.

Do not register hooks automatically. Only if the user asks, show how to register `${CLAUDE_SKILL_DIR}/scripts/tools/wbs_hook.py` as a Claude Code `PostToolUse` command hook: use `python "${CLAUDE_SKILL_DIR}/scripts/tools/wbs_hook.py"` with matcher `Write|Edit|MultiEdit|Bash|PowerShell`. Resolve the skill path to its actual absolute path when writing settings; do not assume skill-body substitutions work in hook settings. Explain that it checks existing files named exactly `wbs.json`, returns problems on stderr with exit 2, and otherwise returns silently with exit 0.

## Writing cards

- Keep the title (`text` in WBS, `title` in winsmux-backlog) to the work's name, within 40 characters as counted by Python `len`. Put background, numbers and PR references in `note`.
- Keep WBS `label` to a short badge of 12 characters or fewer; put sentences in `note`.
- Write WBS `status` as exactly one of these four strings: `p-run` (in progress), `p-hold` (not started), `p-wait` (waiting), `p-owner` (waiting for the owner).
- Legacy `p-sora` and `ball: true` keep their meaning but warn; write `status: "p-owner"` instead. `ball` is optional and defaults to `false`; when present, it must be a boolean.
- WBS card keys are `code`, `section`, `text`, `note`, `status`, `label`, `ball`, `progress`; top-level keys are `schema`, `lane`, `repo`, `handoff`, `generated_at`, `done_hidden`, `cards`. Other keys warn that their content belongs in `note` because the board does not show them.

These writing warnings still allow the card to load. The WBS checker and optional hook report them too; `check_wbs.py` exits 1 on any warning.
