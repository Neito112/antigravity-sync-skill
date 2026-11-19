#!/usr/bin/env python3
"""
Antigravity System Backup & Sync Engine
Mirrors ~/.gemini/{config,antigravity,antigravity-cli,antigravity-ide} into a Git repository,
automatically gzip-chunks any file > 45 MB (to stay well below GitHub's 100 MB limit),
strips transient caches/binaries/nested .git folders, and pushes/pulls via Git.
"""

import argparse
import gzip
import json
import os
import platform
import random
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

GEMINI_HOME = Path.home() / ".gemini"
DEFAULT_REPO_DIR = GEMINI_HOME / "antigravity-sync-repo"
DEFAULT_REPO_NAME = "antigravity-system-backup"
BACKUP_REPO_TOPIC = "antigravity-sync-backup"
BACKUP_REPO_DESC = "Full backup of Antigravity system data (skills, plugins, conversations, brain, configs) with human & agent restore guide."
CHUNK_THRESHOLD_BYTES = 45 * 1024 * 1024  # 45 MB
CHUNK_SIZE_BYTES = 40 * 1024 * 1024       # 40 MB per chunk after gzip

SOURCE_ROOTS = [
    "config",
    "antigravity",
    "antigravity-cli",
    "antigravity-ide",
]

# --- App-level system settings living outside ~/.gemini (per-OS app-data dir) ---
APPDATA_PREFIX = "_appdata"


def _app_data_base() -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))


# logical name -> (folder under app-data base, whitelist of relative files/dirs to sync)
# Machine-bound / secret / cache items (Local State, Network cookies, credential-user-scope,
# Cache, Code Cache, GPUCache, Session Storage, logs, DevToolsActivePort, lockfile) are NOT synced.
APPDATA_SOURCES = {
    "Antigravity": {
        "dir": "Antigravity",
        "include": ["app_storage.json", "window_state.json", "Preferences", "Local Storage"],
    },
    "gemini": {
        "dir": "gemini",
        "include": ["settings.json"],
    },
}


def iter_appdata_files():
    """Yield (rel_key_posix, absolute_path) for whitelisted app-level settings files."""
    base = _app_data_base()
    for logical, spec in APPDATA_SOURCES.items():
        root = base / spec["dir"]
        if not root.exists():
            continue
        for item in spec["include"]:
            p = root / item
            if p.is_file():
                yield f"{APPDATA_PREFIX}/{logical}/{item}", p
            elif p.is_dir():
                for cur, _dirs, files in os.walk(p):
                    for f in files:
                        if f.upper() == "LOCK":
                            continue
                        fp = Path(cur) / f
                        rel = fp.relative_to(root).as_posix()
                        yield f"{APPDATA_PREFIX}/{logical}/{rel}", fp


def resolve_local(rel) -> Path:
    """Map a repo-relative key back to the real local path on this machine/OS."""
    rel_posix = Path(rel).as_posix()
    parts = rel_posix.split("/")
    if parts and parts[0] == APPDATA_PREFIX and len(parts) >= 3:
        spec = APPDATA_SOURCES.get(parts[1])
        if spec:
            return _app_data_base() / spec["dir"] / Path(*parts[2:])
    return GEMINI_HOME / rel_posix

# Relative directory names to exclude anywhere under a source root
EXCLUDE_DIR_NAMES = {
    ".git",
    "node_modules",
    "cache",
    "crashes",
    "daemon",
    "log",
    "logs",
    "presence",
    "updater",
    "antigravity-sync-repo",
}

# Exact relative paths (from ~/.gemini) to exclude
EXCLUDE_REL_PATHS = {
    "antigravity/bin",
    "antigravity-cli/bin",
    "antigravity-ide/bin",
    "antigravity/plugin_data/gdrive/bin",
    "antigravity/cli.log",
    "antigravity/installation_id",
}

EXCLUDE_FILE_SUFFIXES = (
    ".db-shm",
    ".db-wal",
    ".lock",
    ".pid",
)

EXCLUDE_FILE_CONTAINS = (
    ".tmp.",
    "DevToolsActivePort",
)


def run_cmd(cmd, cwd=None, check=True):
    res = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and res.returncode != 0:
        raise RuntimeError(
            f"Command failed ({res.returncode}): {' '.join(cmd)}\n"
            f"STDOUT: {res.stdout.strip()}\nSTDERR: {res.stderr.strip()}"
        )
    return res


def should_exclude_dir(rel_from_gemini: Path, dir_name: str) -> bool:
    if dir_name in EXCLUDE_DIR_NAMES:
        return True
    rel_posix = rel_from_gemini.as_posix()
    if rel_posix in EXCLUDE_REL_PATHS:
        return True
    # Exclude any plugin_data/*/bin directory
    parts = rel_from_gemini.parts
    if len(parts) >= 4 and parts[1] == "plugin_data" and parts[-1] == "bin":
        return True
    return False


def should_exclude_file(rel_from_gemini: Path, file_name: str) -> bool:
    rel_posix = rel_from_gemini.as_posix()
    if rel_posix in EXCLUDE_REL_PATHS:
        return True
    if any(file_name.endswith(s) for s in EXCLUDE_FILE_SUFFIXES):
        return True
    if any(tok in file_name for tok in EXCLUDE_FILE_CONTAINS):
        return True
    if file_name in {"daemon.pid", "daemon.log", "syn-daemon.pid", "syn-daemon.log", "syn-state.json"}:
        return True
    return False


def write_chunked_gzip(src_file: Path, dst_base: Path) -> list[str]:
    """Gzip-compress src_file and split into <= CHUNK_SIZE_BYTES parts."""
    dst_base.parent.mkdir(parents=True, exist_ok=True)
    tmp_gz = dst_base.with_suffix(dst_base.suffix + ".tmpgz")
    with open(src_file, "rb") as f_in, gzip.open(tmp_gz, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out, length=1024 * 1024)

    parts = []
    idx = 0
    with open(tmp_gz, "rb") as f_gz:
        while True:
            buf = f_gz.read(CHUNK_SIZE_BYTES)
            if not buf:
                break
            part_path = Path(f"{dst_base}.agychunk.{idx:03d}")
            with open(part_path, "wb") as f_part:
                f_part.write(buf)
            parts.append(part_path.name)
            idx += 1
    tmp_gz.unlink(missing_ok=True)
    return parts


def reassemble_chunked_gzip(part_paths: list[Path], dst_file: Path):
    """Reassemble sorted .agychunk.NNN parts and gunzip to dst_file."""
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_gz = dst_file.with_suffix(dst_file.suffix + ".tmpgz")
    with open(tmp_gz, "wb") as f_out:
        for p in sorted(part_paths, key=lambda x: x.name):
            with open(p, "rb") as f_in:
                shutil.copyfileobj(f_in, f_out, length=1024 * 1024)
    with gzip.open(tmp_gz, "rb") as f_gz, open(dst_file, "wb") as f_dst:
        shutil.copyfileobj(f_gz, f_dst, length=1024 * 1024)
    tmp_gz.unlink(missing_ok=True)


def collect_nested_git_meta(src_dir: Path) -> dict | None:
    git_dir = src_dir / ".git"
    if not git_dir.exists():
        return None
    try:
        remote = run_cmd(["git", "remote", "get-url", "origin"], cwd=src_dir, check=False).stdout.strip()
        commit = run_cmd(["git", "rev-parse", "HEAD"], cwd=src_dir, check=False).stdout.strip()
        branch = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=src_dir, check=False).stdout.strip()
        return {"remote": remote, "commit": commit, "branch": branch}
    except Exception:
        return None


def stage_backup(repo_dir: Path) -> dict:
    data_root = repo_dir / "data"
    if data_root.exists():
        shutil.rmtree(data_root)
    data_root.mkdir(parents=True, exist_ok=True)

    copied_files = 0
    chunked_files = []
    skipped_locked = []
    nested_repos = []

    for root_name in SOURCE_ROOTS:
        src_root = GEMINI_HOME / root_name
        if not src_root.exists():
            continue

        for current_dir, dirnames, filenames in os.walk(src_root):
            cur_path = Path(current_dir)
            rel_dir = cur_path.relative_to(GEMINI_HOME)

            # Record nested git metadata if present
            if (cur_path / ".git").exists() and cur_path != src_root:
                meta = collect_nested_git_meta(cur_path)
                if meta:
                    nested_repos.append({"path": rel_dir.as_posix(), **meta})

            # Prune excluded directories in-place
            kept_dirs = []
            for d in dirnames:
                child_rel = rel_dir / d
                if not should_exclude_dir(child_rel, d):
                    kept_dirs.append(d)
            dirnames[:] = kept_dirs

            for fname in filenames:
                rel_file = rel_dir / fname
                if should_exclude_file(rel_file, fname):
                    continue
                src_file = cur_path / fname
                dst_file = data_root / rel_file
                try:
                    size = src_file.stat().st_size
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    if size > CHUNK_THRESHOLD_BYTES:
                        parts = write_chunked_gzip(src_file, dst_file)
                        chunked_files.append({
                            "path": rel_file.as_posix(),
                            "original_bytes": size,
                            "chunks": parts,
                        })
                    else:
                        shutil.copy2(src_file, dst_file)
                    copied_files += 1
                except PermissionError:
                    skipped_locked.append(rel_file.as_posix())
                except OSError as e:
                    skipped_locked.append(f"{rel_file.as_posix()} ({e})")

    # App-level system settings stored outside ~/.gemini
    for rel_key, src_file in iter_appdata_files():
        dst_file = data_root / rel_key
        try:
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            copied_files += 1
        except (PermissionError, OSError) as e:
            skipped_locked.append(f"{rel_key} ({e})")

    manifest = {
        "schema_version": 1,
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
        "source_host": socket.gethostname(),
        "source_os": platform.system(),
        "source_home": str(GEMINI_HOME),
        "copied_files": copied_files,
        "chunked_files": chunked_files,
        "nested_repos": nested_repos,
        "skipped_locked": skipped_locked,
    }
    (repo_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def _extract_topic_names(repo_obj: dict) -> set[str]:
    topics = set()
    for item in (repo_obj.get("repositoryTopics") or []):
        if isinstance(item, dict):
            name = item.get("name")
        elif isinstance(item, str):
            name = item
        else:
            name = None
        if name:
            topics.add(name.lower())
    return topics


def discover_existing_private_backup_repo(gh_user: str, preferred_name: str) -> dict | None:
    """Find an existing Antigravity backup repo on the linked GitHub account and ensure it is private."""
    full_repo = f"{gh_user}/{preferred_name}"
    view_res = run_cmd(
        ["gh", "repo", "view", full_repo, "--json", "name,nameWithOwner,url,isPrivate"],
        check=False,
    )
    if view_res.returncode == 0:
        try:
            info = json.loads(view_res.stdout or "{}")
            if info.get("isPrivate") is False:
                run_cmd(
                    ["gh", "repo", "edit", full_repo, "--visibility", "private", "--accept-visibility-change-consequences"],
                    check=False,
                )
            nwo = info.get("nameWithOwner") or full_repo
            url = info.get("url") or f"https://github.com/{nwo}"
            return {"nameWithOwner": nwo, "url": url, "isPrivate": True}
        except Exception:
            pass

    list_res = run_cmd(
        [
            "gh", "repo", "list", gh_user,
            "--visibility", "private",
            "--limit", "100",
            "--json", "name,nameWithOwner,description,url,isPrivate,repositoryTopics",
        ],
        check=False,
    )
    if list_res.returncode != 0 or not list_res.stdout.strip():
        return None

    try:
        repos = json.loads(list_res.stdout)
    except Exception:
        return None

    candidates = []
    for r in repos:
        if r.get("isPrivate") is not True:
            continue
        name_lower = (r.get("name") or "").lower()
        if name_lower in {"antigravity-sync-skill", "antigravity-custom-theme-skill"} or name_lower.endswith("-skill"):
            continue
        topics = _extract_topic_names(r)
        desc_lower = (r.get("description") or "").lower()

        priority = None
        if BACKUP_REPO_TOPIC in topics:
            priority = 0
        elif "antigravity system data" in desc_lower or "antigravity backup" in desc_lower:
            priority = 1
        elif "antigravity" in name_lower and ("backup" in name_lower or "sync" in name_lower):
            priority = 2

        if priority is not None:
            candidates.append((priority, r))

    if not candidates:
        return None

    candidates.sort(key=lambda pair: pair[0])
    chosen = candidates[0][1]
    nwo = chosen.get("nameWithOwner") or f"{gh_user}/{chosen.get('name')}"
    url = chosen.get("url") or f"https://github.com/{nwo}"
    return {"nameWithOwner": nwo, "url": url, "isPrivate": True}


def _ensure_git_identity(repo_dir: Path, gh_user: str):
    name_res = run_cmd(["git", "config", "user.name"], cwd=repo_dir, check=False)
    email_res = run_cmd(["git", "config", "user.email"], cwd=repo_dir, check=False)
    if not name_res.stdout.strip():
        run_cmd(["git", "config", "user.name", gh_user or "antigravity-sync"], cwd=repo_dir)
    if not email_res.stdout.strip():
        login = gh_user or "antigravity-sync"
        run_cmd(["git", "config", "user.email", f"{login}@users.noreply.github.com"], cwd=repo_dir)


def _set_remote_origin(repo_dir: Path, remote_url: str):
    cur = run_cmd(["git", "remote", "get-url", "origin"], cwd=repo_dir, check=False)
    if cur.returncode == 0:
        run_cmd(["git", "remote", "set-url", "origin", remote_url], cwd=repo_dir)
    else:
        run_cmd(["git", "remote", "add", "origin", remote_url], cwd=repo_dir)


def _create_private_backup_repo(gh_user: str, repo_name: str) -> str:
    full_repo = f"{gh_user}/{repo_name}"
    run_cmd([
        "gh", "repo", "create", full_repo,
        "--private",
        "--description", BACKUP_REPO_DESC,
    ])
    run_cmd(["gh", "repo", "edit", full_repo, "--add-topic", BACKUP_REPO_TOPIC], check=False)
    return f"https://github.com/{full_repo}.git"


def ensure_git_repo(repo_dir: Path, repo_name: str) -> tuple[str, bool]:
    """Ensure local git repo is connected to an existing private backup repo (reused) or creates one."""
    gh_user = run_cmd(["gh", "api", "user", "--jq", ".login"], check=True).stdout.strip()

    if (repo_dir / ".git").exists():
        origin = run_cmd(["git", "remote", "get-url", "origin"], cwd=repo_dir, check=False).stdout.strip()
        user_lower = gh_user.lower()
        origin_lower = origin.lower()
        if origin and (f"/{user_lower}/" in origin_lower or f":{user_lower}/" in origin_lower):
            _ensure_git_identity(repo_dir, gh_user)
            return origin, True

        found = discover_existing_private_backup_repo(gh_user, repo_name)
        if found:
            remote_url = f"https://github.com/{found['nameWithOwner']}.git"
            reused = True
        else:
            remote_url = _create_private_backup_repo(gh_user, repo_name)
            reused = False
        _set_remote_origin(repo_dir, remote_url)
        _ensure_git_identity(repo_dir, gh_user)
        return remote_url, reused

    found = discover_existing_private_backup_repo(gh_user, repo_name)
    if found:
        reused = True
        remote_url = f"https://github.com/{found['nameWithOwner']}.git"
        is_empty_dir = (not repo_dir.exists()) or (not any(repo_dir.iterdir()))
        if is_empty_dir:
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            run_cmd(["git", "clone", remote_url, str(repo_dir)])
            head_res = run_cmd(["git", "rev-parse", "--verify", "HEAD"], cwd=repo_dir, check=False)
            if head_res.returncode != 0:
                run_cmd(["git", "checkout", "-B", "main"], cwd=repo_dir, check=False)
        else:
            run_cmd(["git", "init", "-b", "main"], cwd=repo_dir)
            _set_remote_origin(repo_dir, remote_url)
            run_cmd(["git", "fetch", "origin", "main"], cwd=repo_dir, check=False)
            has_remote_main = run_cmd(
                ["git", "rev-parse", "--verify", "origin/main"], cwd=repo_dir, check=False
            ).returncode == 0
            if has_remote_main:
                run_cmd(["git", "reset", "--mixed", "origin/main"], cwd=repo_dir, check=False)
    else:
        reused = False
        remote_url = _create_private_backup_repo(gh_user, repo_name)
        repo_dir.mkdir(parents=True, exist_ok=True)
        run_cmd(["git", "init", "-b", "main"], cwd=repo_dir)
        _set_remote_origin(repo_dir, remote_url)

    _ensure_git_identity(repo_dir, gh_user)
    return remote_url, reused


def cmd_backup(args):
    repo_dir = Path(args.repo_dir).expanduser().resolve()
    remote_url, reused_repo = ensure_git_repo(repo_dir, args.repo_name)
    ls_remote = run_cmd(["git", "ls-remote", "--heads", "origin", "main"], cwd=repo_dir, check=False)
    if ls_remote.stdout.strip():
        run_cmd(["git", "fetch", "origin", "main"], cwd=repo_dir, check=False)
        run_cmd(["git", "pull", "--rebase", "--autostash", "origin", "main"], cwd=repo_dir, check=False)

    manifest = stage_backup(repo_dir)

    run_cmd(["git", "add", "-A"], cwd=repo_dir)
    status = run_cmd(["git", "status", "--porcelain"], cwd=repo_dir).stdout.strip()
    pushed = False
    if status:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        host = socket.gethostname()
        run_cmd(["git", "commit", "-m", f"backup({host}): {ts}"], cwd=repo_dir)
        run_cmd(["git", "push", "-u", "origin", "main"], cwd=repo_dir)
        pushed = True
    else:
        # Ensure remote has main even if no new changes since last local commit
        ls_remote = run_cmd(["git", "ls-remote", "--heads", "origin", "main"], cwd=repo_dir, check=False)
        if not ls_remote.stdout.strip():
            run_cmd(["git", "push", "-u", "origin", "main"], cwd=repo_dir)
            pushed = True

    result = {
        "ok": True,
        "action": "backup",
        "pushed": pushed,
        "reused_existing_repo": reused_repo,
        "remote_url": remote_url,
        "repo_dir": str(repo_dir),
        "copied_files": manifest["copied_files"],
        "chunked_count": len(manifest["chunked_files"]),
        "skipped_locked": manifest["skipped_locked"],
        "backed_up_at": manifest["backed_up_at"],
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        state = "Pushed changes to GitHub" if pushed else "Already up to date"
        print(f"[antigravity-sync] {state} ({manifest['copied_files']} files, {len(manifest['chunked_files'])} chunked)")
        if manifest["skipped_locked"]:
            print(f"[antigravity-sync] Skipped locked files: {len(manifest['skipped_locked'])}")


def cmd_restore(args):
    repo_dir = Path(args.repo_dir).expanduser().resolve()
    if not (repo_dir / ".git").exists():
        if not args.remote_url:
            gh_user = run_cmd(["gh", "api", "user", "--jq", ".login"], check=True).stdout.strip()
            found = discover_existing_private_backup_repo(gh_user, args.repo_name)
            if found:
                args.remote_url = f"https://github.com/{found['nameWithOwner']}.git"
            else:
                args.remote_url = f"https://github.com/{gh_user}/{args.repo_name}.git"
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        run_cmd(["git", "clone", args.remote_url, str(repo_dir)])
    else:
        run_cmd(["git", "pull", "--rebase", "origin", "main"], cwd=repo_dir)

    data_root = repo_dir / "data"
    if not data_root.exists():
        raise RuntimeError(f"Backup data folder not found at {data_root}")

    # Safety snapshot of existing config.json
    cfg_file = GEMINI_HOME / "config" / "config.json"
    if cfg_file.exists():
        bak = GEMINI_HOME / "config" / f"config.json.pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak"
        shutil.copy2(cfg_file, bak)

    restored = 0
    skipped_locked = []
    chunk_groups: dict[Path, list[Path]] = {}

    for current_dir, _, filenames in os.walk(data_root):
        cur_path = Path(current_dir)
        rel_dir = cur_path.relative_to(data_root)
        for fname in filenames:
            src_file = cur_path / fname
            if ".agychunk." in fname:
                base_name = fname.split(".agychunk.")[0]
                target_rel = rel_dir / base_name
                chunk_groups.setdefault(target_rel, []).append(src_file)
                continue

            dst_file = resolve_local(rel_dir / fname)
            try:
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)
                restored += 1
            except (PermissionError, OSError) as e:
                skipped_locked.append(f"{(rel_dir / fname).as_posix()} ({e})")

    for target_rel, parts in chunk_groups.items():
        dst_file = resolve_local(target_rel)
        try:
            reassemble_chunked_gzip(parts, dst_file)
            restored += 1
        except (PermissionError, OSError) as e:
            skipped_locked.append(f"{target_rel.as_posix()} ({e})")

    result = {
        "ok": True,
        "action": "restore",
        "repo_dir": str(repo_dir),
        "restored_files": restored,
        "reassembled_chunks": len(chunk_groups),
        "skipped_locked": skipped_locked,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"[antigravity-sync] Restored {restored} files ({len(chunk_groups)} chunked files reassembled).")
        if skipped_locked:
            print("[antigravity-sync] Some active/locked files could not be overwritten while Antigravity is running:")
            for item in skipped_locked[:10]:
                print(f"  - {item}")


def _remove_existing_chunks(base_path: Path):
    for p in base_path.parent.glob(base_path.name + ".agychunk.*"):
        p.unlink(missing_ok=True)


def _stage_file_to_repo(src_file: Path, dst_file: Path):
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    size = src_file.stat().st_size
    if size > CHUNK_THRESHOLD_BYTES:
        if dst_file.exists():
            dst_file.unlink(missing_ok=True)
        _remove_existing_chunks(dst_file)
        write_chunked_gzip(src_file, dst_file)
        return True
    else:
        _remove_existing_chunks(dst_file)
        shutil.copy2(src_file, dst_file)
        return False


def _merge_two_way(repo_dir: Path) -> dict:
    """
    Conflict-free 2-way union & newer-mtime-wins merge between ~/.gemini and repo/data.
    Never deletes files created by another machine.
    """
    data_root = repo_dir / "data"
    data_root.mkdir(parents=True, exist_ok=True)

    # 1. Index local files
    local_files: dict[str, Path] = {}
    for root_name in SOURCE_ROOTS:
        src_root = GEMINI_HOME / root_name
        if not src_root.exists():
            continue
        for current_dir, dirnames, filenames in os.walk(src_root):
            cur_path = Path(current_dir)
            rel_dir = cur_path.relative_to(GEMINI_HOME)
            dirnames[:] = [d for d in dirnames if not should_exclude_dir(rel_dir / d, d)]
            for fname in filenames:
                rel_file = rel_dir / fname
                if not should_exclude_file(rel_file, fname):
                    local_files[rel_file.as_posix()] = cur_path / fname

    for rel_key, p in iter_appdata_files():
        local_files[rel_key] = p

    # 2. Index remote files in repo/data (grouping .agychunk.* under base rel path)
    remote_plain: dict[str, Path] = {}
    remote_chunks: dict[str, list[Path]] = {}
    for current_dir, _, filenames in os.walk(data_root):
        cur_path = Path(current_dir)
        rel_dir = cur_path.relative_to(data_root)
        for fname in filenames:
            p = cur_path / fname
            if ".agychunk." in fname:
                base_name = fname.split(".agychunk.")[0]
                rel_key = (rel_dir / base_name).as_posix()
                remote_chunks.setdefault(rel_key, []).append(p)
            else:
                rel_key = (rel_dir / fname).as_posix()
                remote_plain[rel_key] = p

    pushed_up = 0
    pulled_down = 0
    skipped_locked = []

    all_keys = set(local_files.keys()) | set(remote_plain.keys()) | set(remote_chunks.keys())
    for rel_key in all_keys:
        l_path = local_files.get(rel_key)
        r_plain = remote_plain.get(rel_key)
        r_parts = remote_chunks.get(rel_key)
        has_remote = bool(r_plain or r_parts)

        if l_path and not has_remote:
            # Created on this machine -> stage to repo
            try:
                _stage_file_to_repo(l_path, data_root / rel_key)
                pushed_up += 1
            except (PermissionError, OSError) as e:
                skipped_locked.append(f"{rel_key} ({e})")
        elif has_remote and not l_path:
            # Created on the other machine -> pull to local
            dst = resolve_local(rel_key)
            try:
                if r_parts:
                    reassemble_chunked_gzip(r_parts, dst)
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(r_plain, dst)
                pulled_down += 1
            except (PermissionError, OSError) as e:
                skipped_locked.append(f"{rel_key} ({e})")
        elif l_path and has_remote:
            # Exists on both -> newer mtime wins (2s tolerance)
            try:
                l_stat = l_path.stat()
                if r_parts:
                    r_mtime = max(p.stat().st_mtime for p in r_parts)
                    r_size = None
                else:
                    r_stat = r_plain.stat()
                    r_mtime = r_stat.st_mtime
                    r_size = r_stat.st_size

                if r_size is not None and l_stat.st_size == r_size and abs(l_stat.st_mtime - r_mtime) <= 2.0:
                    continue

                if l_stat.st_mtime > r_mtime + 2.0:
                    _stage_file_to_repo(l_path, data_root / rel_key)
                    pushed_up += 1
                elif r_mtime > l_stat.st_mtime + 2.0:
                    # Never overwrite an actively-open SQLite DB that has a non-empty WAL
                    if l_path.suffix == ".db":
                        wal = Path(str(l_path) + "-wal")
                        if wal.exists() and wal.stat().st_size > 0:
                            skipped_locked.append(f"{rel_key} (active SQLite WAL)")
                            continue
                    if r_parts:
                        reassemble_chunked_gzip(r_parts, l_path)
                    else:
                        shutil.copy2(r_plain, l_path)
                    pulled_down += 1
            except (PermissionError, OSError) as e:
                skipped_locked.append(f"{rel_key} ({e})")

    # Per-machine non-conflicting heartbeat file
    machines_dir = repo_dir / "machines"
    machines_dir.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()
    (machines_dir / f"{host}.json").write_text(
        json.dumps({
            "host": host,
            "os": platform.system(),
            "last_sync_at": datetime.now(timezone.utc).isoformat(),
            "local_files": len(local_files),
            "pushed_up": pushed_up,
            "pulled_down": pulled_down,
        }, indent=2),
        encoding="utf-8",
    )

    return {
        "pushed_up": pushed_up,
        "pulled_down": pulled_down,
        "local_files": len(local_files),
        "skipped_locked": skipped_locked,
    }


def cmd_sync(args):
    """Conflict-free 2-way smart sync with pull-before-push & automatic retry."""
    repo_dir = Path(args.repo_dir).expanduser().resolve()
    remote_url, reused_repo = ensure_git_repo(repo_dir, args.repo_name)

    stats = {"pushed_up": 0, "pulled_down": 0, "local_files": 0, "skipped_locked": []}
    pushed = False

    for attempt in range(3):
        # 1. Pull latest changes from the other machine first
        run_cmd(["git", "fetch", "origin", "main"], cwd=repo_dir, check=False)
        ls_remote = run_cmd(["git", "ls-remote", "--heads", "origin", "main"], cwd=repo_dir, check=False)
        if ls_remote.stdout.strip():
            run_cmd(["git", "pull", "--rebase", "--autostash", "origin", "main"], cwd=repo_dir, check=False)

        # 2. Non-destructive 2-way merge
        stats = _merge_two_way(repo_dir)

        # 3. Commit & push if any local changes staged
        run_cmd(["git", "add", "-A"], cwd=repo_dir)
        status = run_cmd(["git", "status", "--porcelain"], cwd=repo_dir).stdout.strip()
        if not status:
            break

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        host = socket.gethostname()
        run_cmd(["git", "commit", "-m", f"sync({host}): {ts}"], cwd=repo_dir)

        push_res = run_cmd(["git", "push", "-u", "origin", "main"], cwd=repo_dir, check=False)
        if push_res.returncode == 0:
            pushed = True
            break

        # Concurrent push race from another machine: rebase & retry with jitter
        if attempt < 2:
            time.sleep(random.uniform(1.0, 3.0))
        else:
            raise RuntimeError(f"git push failed after 3 attempts: {push_res.stderr.strip()}")

    result = {
        "ok": True,
        "action": "sync",
        "pushed": pushed,
        "reused_existing_repo": reused_repo,
        "remote_url": remote_url,
        "repo_dir": str(repo_dir),
        "pushed_up": stats["pushed_up"],
        "pulled_down": stats["pulled_down"],
        "copied_files": stats["local_files"],
        "skipped_locked": stats["skipped_locked"],
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(
            f"[antigravity-sync] Smart Sync complete: ↑{stats['pushed_up']} staged, "
            f"↓{stats['pulled_down']} pulled, pushed={pushed}"
        )


def cmd_resolve(args):
    """Read-only discovery of an existing private backup repo on the linked GitHub account."""
    gh_user = run_cmd(["gh", "api", "user", "--jq", ".login"], check=True).stdout.strip()
    found = discover_existing_private_backup_repo(gh_user, args.repo_name)
    result = {
        "ok": True,
        "action": "resolve",
        "gh_user": gh_user,
        "found": bool(found),
        "nameWithOwner": found["nameWithOwner"] if found else None,
        "remote_url": f"https://github.com/{found['nameWithOwner']}.git" if found else None,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        if found:
            print(f"[antigravity-sync] Found existing private repo: {found['nameWithOwner']} ({result['remote_url']})")
        else:
            print(f"[antigravity-sync] No existing private backup repo found for {gh_user}.")


def main():
    parser = argparse.ArgumentParser(description="Antigravity System Backup & Sync Engine")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("backup", "restore", "sync", "resolve"):
        p = sub.add_parser(name)
        p.add_argument("--repo-dir", default=str(DEFAULT_REPO_DIR))
        p.add_argument("--repo-name", default=DEFAULT_REPO_NAME)
        p.add_argument("--json", action="store_true")
        if name == "restore":
            p.add_argument("--remote-url", default="")

    args = parser.parse_args()
    try:
        if args.command == "backup":
            cmd_backup(args)
        elif args.command == "sync":
            cmd_sync(args)
        elif args.command == "resolve":
            cmd_resolve(args)
        else:
            cmd_restore(args)
    except Exception as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"[antigravity-sync] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
