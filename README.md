# Antigravity Sync (`antigravity-sync`)

An Antigravity Agent Skill + Topbar **"Syn"** CDP daemon for backing up and synchronizing your entire Antigravity environment (`~/.gemini/config`, `antigravity`, `antigravity-cli`, `antigravity-ide`) to a **private** GitHub repository (`antigravity-system-backup`).

## Features
- **Auto-creates a Private GitHub Repo (`--private`)**: First run automatically creates `antigravity-system-backup` as a **private** repository on your GitHub account via `gh`.
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
# 2-way conflict-free sync (auto-creates private repo on first run)
python ~/.gemini/config/skills/antigravity-sync/scripts/sync_engine.py sync

# Full backup & push
python ~/.gemini/config/skills/antigravity-sync/scripts/sync_engine.py backup

# Restore onto a new machine
python ~/.gemini/config/skills/antigravity-sync/scripts/sync_engine.py restore
```
