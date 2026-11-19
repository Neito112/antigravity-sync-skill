# Antigravity Sync (`antigravity-sync`)

An Antigravity Agent Skill + Topbar **"Syn"** CDP daemon for backing up and synchronizing your entire Antigravity environment (`~/.gemini/config`, `antigravity`, `antigravity-cli`, `antigravity-ide`) to a **private** GitHub repository (`antigravity-system-backup`).

## Features
- **Smart Private Repo Discovery & Creation**: Automatically checks the linked GitHub account (`gh`) for an existing private Antigravity backup repository (by name, topic `antigravity-sync-backup`, or description) and **reuses it directly** without creating duplicates. Only creates a new `--private` repository (`antigravity-system-backup`) when none exists.
- **Topbar `"Syn"` Button**: Injects a native-looking **`Syn`** button directly into Antigravity's topbar (`[data-testid="pane-titlebar-toolbar"]`).
- **Conflict-Free 2-Way Smart Sync (Multi-Machine Safe)**: Pulls before pushing, merges files non-destructively (newer `mtime` wins, union of conversations/brain/skills), skips actively-locked SQLite `.db-wal` files, and retries with jitter on concurrent pushes.
- **Configurable Auto-Sync Timer (Minutes)**: Set `Off`, `5m`, `15m`, `30m`, or any custom interval in minutes directly from the **`Syn`** popover.
- **Automatic Large File Chunking (> 45 MB)**: Gzips & splits large conversation `.db` files into `*.agychunk.*` parts so GitHub's 100 MB file limit is never exceeded.
- **Update-Proof Persistence**: Includes `scripts/ensure_daemons.py` + `SessionStart` hook (`~/.gemini/config/hooks.json`) and Windows Startup script so the topbar **`Syn`** button & custom theme automatically revive after any Antigravity update/restart.

## Quick Install

### Windows (PowerShell)
```powershell
git clone https://github.com/Neito112/antigravity-sync-skill.git "$env:USERPROFILE\.gemini\config\skills\antigravity-sync"
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\.gemini\config\skills\antigravity-sync\scripts\install-win.ps1"
```

### macOS / Linux (Bash)
```bash
git clone https://github.com/Neito112/antigravity-sync-skill.git "$HOME/.gemini/config/skills/antigravity-sync"
bash "$HOME/.gemini/config/skills/antigravity-sync/scripts/install-unix.sh"
```

## CLI Usage
```bash
# Discover existing private backup repo on the linked GitHub account (read-only)
python ~/.gemini/config/skills/antigravity-sync/scripts/sync_engine.py resolve --json

# 2-way conflict-free sync (reuses existing private repo, or auto-creates private repo if none exists)
python ~/.gemini/config/skills/antigravity-sync/scripts/sync_engine.py sync

# Full backup & push
python ~/.gemini/config/skills/antigravity-sync/scripts/sync_engine.py backup

# Restore onto a new machine
python ~/.gemini/config/skills/antigravity-sync/scripts/sync_engine.py restore
```

## Custom storage drive (virtual path)
Store the sync repo on any drive while `~/.gemini/antigravity-sync-repo` stays valid (NTFS junction on Windows, symlink on macOS/Linux):
```bash
python scripts/sync_engine.py set-location --path D:\AntigravitySync
```
Or use the folder field in the topbar **Syn** popover. Existing data is copied then removed from C: only after a successful copy. The choice is saved in `syn-config.json` (machine-local, not synced) and the link self-heals on each run; if the drive is missing it falls back to the standard path. On a new machine run `set-location` before the first sync/restore.
