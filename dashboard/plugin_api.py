import json
import os
import re
import subprocess
from http.client import HTTPException
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from urllib.parse import quote
from urllib.request import Request, urlopen

from fastapi import APIRouter
from ruamel.yaml import YAML, YAMLError


PLUGIN_ID = "strata-backend"
PLUGIN_DIR = Path(__file__).resolve().parents[1]
GITHUB_ORIGIN = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?",
    re.IGNORECASE,
)
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
update_lock = Lock()
router = APIRouter()


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(PLUGIN_DIR), *args],
        capture_output=True, text=True, check=True, timeout=5,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def local_version() -> str | None:
    manifest = YAML(typ="safe").load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    version = manifest.get("version") if isinstance(manifest, dict) else None
    return str(version) if isinstance(version, (str, int, float)) else None


def checkout() -> tuple[str, str, str]:
    if not (PLUGIN_DIR / ".git").is_dir():
        raise ValueError("Install Strata Backend from its GitHub repository to enable updates.")
    if (PLUGIN_DIR / ".hermes-catalog.json").is_file():
        raise ValueError("Update this catalog install from Capabilities → Plugins.")
    try:
        origin = GITHUB_ORIGIN.fullmatch(git("remote", "get-url", "origin"))
        branch = git("symbolic-ref", "--short", "HEAD")
        remote = git("config", "--get", f"branch.{branch}.remote")
        upstream = git("config", "--get", f"branch.{branch}.merge")
        revision = git("rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Strata Backend needs a Git checkout tracking a GitHub branch.") from exc
    if not origin or remote != "origin" or not upstream.startswith("refs/heads/"):
        raise ValueError("Strata Backend must track a branch on its GitHub origin.")
    return origin[1], upstream.removeprefix("refs/heads/"), revision


@router.get("/version-status")
def version_status():
    status = {"current": None, "latest": None, "update_available": False, "release_url": None}
    try:
        status["current"] = local_version()
        repo, branch, revision = checkout()
    except (OSError, YAMLError, ValueError) as exc:
        return {**status, "error": str(exc) if isinstance(exc, ValueError) else "Could not read the plugin version."}

    status["current"] = status["current"] or revision[:7]
    # The branch is the base, so GitHub reports "behind" when HEAD can fast-forward.
    compare = f"https://api.github.com/repos/{repo}/compare/{quote(branch, safe='')}...{revision}?per_page=1"
    request = Request(compare, headers={"Accept": "application/vnd.github+json", "User-Agent": PLUGIN_ID})
    try:
        with urlopen(request, timeout=5) as response:
            comparison = json.load(response)
        latest = comparison["base_commit"]["sha"]
        if isinstance(latest, str) and COMMIT_SHA.fullmatch(latest):
            status.update(
                latest=latest[:7],
                update_available=comparison["status"] == "behind" and latest != revision,
                release_url=f"https://github.com/{repo}/commit/{latest}",
            )
    except (OSError, HTTPException, ValueError, KeyError, TypeError):
        # Offline, rate-limited, or the API cannot see the local commit.
        pass
    return status


def sync_desktop() -> bool:
    from hermes_cli.desktop_plugin_halves import (
        PACKAGE_MARKER,
        ROOT_WRITER,
        materialize_desktop_half,
        read_package_marker,
        reconcile_desktop_halves_report,
    )
    from hermes_constants import get_default_hermes_root
    from pm.filesystem import is_junction

    root = get_default_hermes_root()
    desktop_root = root / "desktop-plugins"
    published = desktop_root / PLUGIN_ID
    source = PLUGIN_DIR / "desktop"

    with ROOT_WRITER:
        marker = read_package_marker(published)
        if (marker and Path(marker["source"]) == source and not published.is_symlink()
                and not is_junction(published) and source.is_dir()
                and set(p.name for p in source.iterdir()) == {"plugin.js"}
                and set(p.name for p in published.iterdir()) == {"plugin.js", PACKAGE_MARKER}):
            # Hermes watches the directory containing plugin.js. Keep the directory
            # alive across updates so every replacement still reaches that watcher.
            with TemporaryDirectory(prefix=".strata-desktop-", dir=root) as directory:
                staged = materialize_desktop_half(PLUGIN_DIR, Path(directory), PLUGIN_ID)
                if staged and (staged / "plugin.js").read_bytes() != (published / "plugin.js").read_bytes():
                    os.replace(staged / "plugin.js", published / "plugin.js")
                if staged:
                    os.replace(staged / PACKAGE_MARKER, published / PACKAGE_MARKER)

    report = reconcile_desktop_halves_report(root, desktop_root)
    return not any(item["name"] == PLUGIN_ID for item in report["refused"]) and (
        (source / "plugin.js").is_file() and (published / "plugin.js").is_file()
        and (source / "plugin.js").read_bytes() == (published / "plugin.js").read_bytes()
    )


@router.post("/apply-update")
def apply_update():
    if not update_lock.acquire(blocking=False):
        return {"ok": False, "error": "A Strata Backend update is already running."}
    try:
        checkout()
        if git("status", "--porcelain", "--untracked-files=normal"):
            return {"ok": False, "error": "Save or stash your Strata Backend changes before updating."}

        from hermes_cli.plugins_cmd_update import dashboard_update_user_plugin
        from hermes_constants import get_hermes_home

        if (get_hermes_home() / "plugins" / PLUGIN_ID).resolve() != PLUGIN_DIR:
            return {"ok": False, "error": "This profile does not own the Strata Backend checkout."}
        result = dashboard_update_user_plugin(PLUGIN_ID, force=True)
        if not result["ok"]:
            return {"ok": False, "error": result.get("error") or "Hermes could not update Strata Backend."}

        try:
            synced = sync_desktop()
        except OSError:
            synced = False
        if not synced:
            return {"ok": False, "error": "The Git update completed, but Hermes could not sync the Desktop tab."}
        return {"ok": True, "version": local_version() or git("rev-parse", "--short", "HEAD")}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except ImportError:
        return {"ok": False, "error": "This Hermes build does not support the Desktop plugin updater."}
    except (OSError, subprocess.SubprocessError, YAMLError):
        return {"ok": False, "error": "Could not read or sync the Strata Backend installation."}
    finally:
        update_lock.release()
