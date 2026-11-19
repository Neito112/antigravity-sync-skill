---
name: antigravity-sync
description: Backup and restore the entire Antigravity system (~/.gemini config, skills, plugins, projects, conversations, brain, knowledge, MCP configs) to/from a private GitHub repository, and manage the topbar "Syn" button daemon.
---

# Antigravity System Backup & Sync (`antigravity-sync`)

Synchronizes the entire Antigravity environment (`~/.gemini/config`, `~/.gemini/antigravity`, `~/.gemini/antigravity-cli`, `~/.gemini/antigravity-ide`) with a private GitHub repository (`antigravity-system-backup`).

## Capabilities

- **Existing Private Repo Reuse (`resolve` / `ensure_git_repo`)**: Automatically inspects the linked GitHub account (`gh`) for an existing private Antigravity backup repository (matching default name, `antigravity-sync-backup` topic, description, or backup naming) and reuses it directly; only creates a new `--private` repo if none exists.
- **Conflict-Free Smart Sync (`sync`)**: Pulls before pushing, merges 2-way non-destructively (union + newer-mtime-wins), skips active SQLite WAL locks, and retries concurrent pushes.
- **Backup & Push (`backup`)**: Mirrors Antigravity data into `~/.gemini/antigravity-sync-repo`, automatically excludes transient caches/logs/binaries/`node_modules`/locked `.db-wal` files, gzip-chunks any file > 45 MB (to respect GitHub's 100 MB file limit), commits, and pushes to GitHub.
- **Pull & Restore (`restore`)**: Clones or pulls the backup repo, reassembles chunked files, snapshots existing `config.json`, and restores all skills, plugins, conversations, brain artifacts, and settings to `~/.gemini/`.
- **Topbar "Syn" Button (`daemon/syn_topbar_daemon.js`)**: Connects to Antigravity's CDP (`DevToolsActivePort`) and injects a native **"Syn"** button directly on the Antigravity topbar (`[data-testid="pane-titlebar-toolbar"]`).

## Quick Commands

### Check Existing Private Backup Repo
```bash
python ~/.gemini/config/skills/antigravity-sync/scripts/sync_engine.py resolve --json
```

### 2-Way Smart Sync / Backup to GitHub
```bash
python ~/.gemini/config/skills/antigravity-sync/scripts/sync_engine.py sync
python ~/.gemini/config/skills/antigravity-sync/scripts/sync_engine.py backup
```

### Restore on a New Machine
```bash
python ~/.gemini/config/skills/antigravity-sync/scripts/sync_engine.py restore --remote-url https://github.com/<USER>/antigravity-system-backup.git
```

### Install / Restart Topbar "Syn" Button
- **Windows:** `powershell -ExecutionPolicy Bypass -File ~/.gemini/config/skills/antigravity-sync/scripts/install-win.ps1`
- **macOS / Linux:** `bash ~/.gemini/config/skills/antigravity-sync/scripts/install-unix.sh`

### Store sync folder on another drive
`python ~/.gemini/config/skills/antigravity-sync/scripts/sync_engine.py set-location --path D:\AntigravitySync`
The standard path `~/.gemini/antigravity-sync-repo` remains as a junction/symlink to it. Run before first sync on a new machine.
