import json
import os
import re
import subprocess
import sys
from contextvars import copy_context
from http.client import HTTPException
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from typing import Literal
from urllib.parse import quote
from urllib.request import Request, urlopen

from fastapi import APIRouter
from plugins.plugin_storage import plugin_data_dir
from ruamel.yaml import YAML, YAMLError


PLUGIN_ID = "strata-backend"
PLUGIN_DIR = Path(__file__).resolve().parents[1]
STRATA_REPO = "Niko1221/Strata"
STRATA_ORIGIN = f"https://github.com/{STRATA_REPO}.git"
STRATA_BRANCH = "main"
# Strata's unattended defaults can install missing system tools. The managed
# setup uses the host's existing toolchain so wiping Strata stays self-contained.
STRATA_SETUP_RUNNER = r"""
import os
import re
import runpy
import sys

def require_existing_toolchain(event, args):
    if event != "subprocess.Popen":
        return
    executable, command, *_ = args
    if executable is None:
        # On Windows CreateProcess can receive only the serialized command line.
        match = re.match(r'\s*(?:"([^"]+)"|(\S+))', command)
        if match is None:
            return
        executable = match[1] or match[2]
    program = os.path.splitext(os.path.basename(os.fsdecode(executable)))[0].lower()
    if program in {"winget", "sudo", "apt", "apt-get", "dpkg", "dnf", "pacman", "brew"}:
        raise RuntimeError("Install Strata's native build prerequisites separately, then retry setup.")

sys.addaudithook(require_existing_toolchain)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
GITHUB_ORIGIN = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?",
    re.IGNORECASE,
)
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
update_lock = Lock()
strata_jobs_lock = Lock()
strata_jobs: dict[Path, str] = {}
router = APIRouter()


def git(*args: str) -> str:
    return git_at(PLUGIN_DIR, *args)


def git_at(directory: Path, *args: str, timeout: int = 5) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True, text=True, check=True, timeout=timeout,
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


def github_revision(repo: str, branch: str, revision: str | None) -> tuple[str | None, bool]:
    ref = quote(branch, safe="")
    # The branch is the base, so GitHub reports "behind" when HEAD can fast-forward.
    endpoint = f"compare/{ref}...{revision}?per_page=1" if revision else f"commits/{ref}"
    request = Request(
        f"https://api.github.com/repos/{repo}/{endpoint}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": PLUGIN_ID},
    )
    try:
        with urlopen(request, timeout=5) as response:
            data = json.load(response)
        latest = data["base_commit"]["sha"] if revision else data["sha"]
        if isinstance(latest, str) and COMMIT_SHA.fullmatch(latest):
            return latest, bool(revision and data["status"] == "behind" and latest != revision)
    except (OSError, HTTPException, ValueError, KeyError, TypeError):
        # Offline, rate-limited, or the API cannot see the local commit.
        pass
    return None, False


@router.get("/version-status")
def version_status():
    status = {"current": None, "latest": None, "update_available": False, "release_url": None}
    try:
        status["current"] = local_version()
        repo, branch, revision = checkout()
    except (OSError, YAMLError, ValueError) as exc:
        return {**status, "error": str(exc) if isinstance(exc, ValueError) else "Could not read the plugin version."}

    status["current"] = status["current"] or revision[:7]
    latest, update_available = github_revision(repo, branch, revision)
    if latest:
        status.update(
            latest=latest[:7],
            update_available=update_available,
            release_url=f"https://github.com/{repo}/commit/{latest}",
        )
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


def strata_directory() -> Path:
    root = plugin_data_dir(PLUGIN_ID).resolve() / "strata-runtime"
    if root.resolve() != root or root.is_symlink() or not (not root.exists() or root.is_dir()):
        raise ValueError("The managed Strata folder is not a normal directory.")
    return root


def strata_head(root: Path) -> str | None:
    source = root / "source"
    if not source.exists() and not source.is_symlink():
        return None
    if source.resolve() != source or source.is_symlink() or not (source / ".git").is_dir():
        raise ValueError("Use Fresh wipe to clear the incomplete Strata checkout.")
    if git_at(source, "remote", "get-url", "origin") != STRATA_ORIGIN:
        raise ValueError("The managed checkout no longer points to the official Strata repository.")
    return git_at(source, "rev-parse", "HEAD")


@router.get("/strata/status")
def strata_status():
    try:
        root = strata_directory()
        with strata_jobs_lock:
            operation = strata_jobs.get(root)
        if operation == "wipe":
            return {"operation": operation}
        revision = strata_head(root)
        record = root / "prepared-commit"
        error = root / "setup-error.txt"
        log = root / "setup.log"
        output = ""
        if log.is_file():
            with log.open("rb") as handle:
                handle.seek(max(0, log.stat().st_size - 3072))
                output = handle.read(3072).decode("utf-8", errors="replace")
        return {
            "revision": revision,
            "ready": bool(revision and record.is_file() and record.read_text().strip() == revision
                          and strata_python(root).is_file()),
            "has_data": root.exists(),
            "operation": operation,
            "error": error.read_text(encoding="utf-8") if error.is_file() else None,
            "log": output,
        }
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return {"has_data": True, "error": str(exc) if isinstance(exc, ValueError) else "Could not read Strata setup status."}


@router.get("/strata/latest")
def strata_latest():
    try:
        revision = strata_head(strata_directory())
    except (ValueError, OSError, subprocess.SubprocessError):
        return {"latest": None, "update_available": False}
    latest, update_available = github_revision(STRATA_REPO, STRATA_BRANCH, revision)
    return {"latest": latest, "update_available": update_available}


def strata_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def prepare_strata(root: Path, operation: str, log) -> None:
    revision = strata_head(root)
    source = root / "source"
    if not revision:
        with TemporaryDirectory(prefix=".clone-", dir=root) as staging:
            candidate = Path(staging) / "source"
            git_at(root, "clone", "--filter=blob:none", "--single-branch", "--branch", STRATA_BRANCH,
                   STRATA_ORIGIN, str(candidate), timeout=900)
            candidate.rename(source)
    else:
        if git_at(source, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError("Save or stash tracked Strata changes before setup or update.")
        if operation == "update":
            if git_at(source, "symbolic-ref", "--short", "HEAD") != STRATA_BRANCH:
                raise ValueError("The managed Strata checkout must be on main before updating.")
            git_at(source, "fetch", STRATA_ORIGIN, f"refs/heads/{STRATA_BRANCH}", timeout=900)
            git_at(source, "merge", "--ff-only", "FETCH_HEAD", timeout=900)

    # Clear readiness before touching setup so a retry can resume an incomplete install.
    prepared = root / "prepared-commit"
    prepared.unlink(missing_ok=True)
    python = strata_python(root)
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("STRATA_")}
    for key in ("PYTHONHOME", "PYTHONPATH", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"):
        env.pop(key, None)
    env.update(
        PYTHONNOUSERSITE="1",
        PYTHONUNBUFFERED="1",
        PYTHONUTF8="1",
        PIP_CACHE_DIR=str(root / "pip-cache"),
        HF_HOME=str(root / "hf-cache"),
        STRATA_SETTINGS_FILE=str(root / "settings.json"),
        STRATA_COMPONENTS_DIR=str(root / "components"),
    )
    options = {
        "cwd": source, "env": env, "stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0), "check": True,
    }
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")], **options)
    env["VIRTUAL_ENV"] = str(root / ".venv")
    env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
    subprocess.run(
        [str(python), "-c", STRATA_SETUP_RUNNER, "setup.py",
         "--yes", "--build", "--no-start", "--data-dir", str(root / "data")],
        **options,
    )
    prepared.write_text(git_at(source, "rev-parse", "HEAD"), encoding="ascii")


def run_strata_job(root: Path, operation: str) -> None:
    try:
        from hermes_cli.plugins_auto_update import plugin_update_lock
        from hermes_cli.plugins_cmd import PluginOperationError

        try:
            with plugin_update_lock(root):
                change_strata(root, operation)
        except PluginOperationError:
            # Another Hermes process already owns the managed directory.
            return
    finally:
        with strata_jobs_lock:
            strata_jobs.pop(root, None)


def change_strata(root: Path, operation: str) -> None:
    try:
        if root != strata_directory():
            raise ValueError("The managed Strata folder changed before the operation.")
        if operation == "wipe":
            from utils import rmtree_readonly

            if root.exists():
                rmtree_readonly(root)
            return

        root.mkdir(parents=True, exist_ok=True)
        (root / "setup-error.txt").unlink(missing_ok=True)
        with (root / "setup.log").open("wb", buffering=0) as log:
            log.write(b"Preparing the managed Strata checkout and Python environment.\n")
            try:
                prepare_strata(root, operation, log)
            except subprocess.CalledProcessError as exc:
                if exc.stderr:
                    log.write(exc.stderr.encode("utf-8", errors="replace"))
                raise ValueError("Strata setup failed. See the setup log for details.") from exc
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Could not complete the Strata operation."
        if root.is_dir() and root.resolve() == root:
            (root / "setup-error.txt").write_text(message, encoding="utf-8")


@router.post("/strata/action/{operation}")
def strata_action(operation: Literal["setup", "update", "wipe"]):
    try:
        root = strata_directory()
    except (OSError, ValueError):
        return {"ok": False, "error": "Could not access the managed Strata folder."}
    with strata_jobs_lock:
        if root in strata_jobs:
            return {"ok": False, "error": "A Strata operation is already running."}
        strata_jobs[root] = operation
        context = copy_context()
        try:
            Thread(target=context.run, args=(run_strata_job, root, operation), daemon=True).start()
        except RuntimeError:
            strata_jobs.pop(root, None)
            return {"ok": False, "error": "Could not start the Strata operation."}
    return {"ok": True, "operation": operation}
