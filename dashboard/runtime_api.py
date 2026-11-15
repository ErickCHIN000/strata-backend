import json
import os
import re
import runpy
import shutil
import subprocess
import sys
from contextvars import copy_context
from functools import partial
from http.client import HTTPException
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4

from plugins.plugin_storage import plugin_data_dir
from ruamel.yaml import YAML, YAMLError


PLUGIN_ID = "strata-backend"
PLUGIN_DIR = Path(__file__).resolve().parents[1]
MODEL_HELPER = Path(__file__).with_name("model_download.py")
STRATA_REPO = "Niko1221/Strata"
STRATA_ORIGIN = f"https://github.com/{STRATA_REPO}.git"
STRATA_BRANCH = "main"
STRATA_CATALOG_READER = """
import json
from pathlib import Path
from tools.strata_mcp import Strata

strata = Strata(Path.cwd())
hardware = strata.hardware()
pick = strata.recommend(hardware)
recommendation = {key: pick.get(key) for key in
                  ("family", "model", "title", "context", "download_gb", "why", "notes")}
families = [
    {"family": family["family"], "title": family["title"],
     "unavailable_reason": family.get("unavailable_reason"),
     "sizes": [{key: size.get(key) for key in
                ("model", "fit_gb", "download_gb", "on_this_pc", "installed", "experimental")}
               for size in family["sizes"]]}
    for family in strata.model_table(hardware)
]
print(json.dumps({"recommendation": recommendation, "families": families}))
"""
# Strata's unattended defaults can install missing system tools. The managed
# setup uses the host's existing toolchain so wiping Strata stays self-contained.
STRATA_SETUP_RUNNER = r"""
import os
import re
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

# Use Strata's build helpers directly. setup.main() selects and downloads a model.
import setup as strata

cuda_cards = sorted((gpu for gpu in strata.gpus() if not strata.gpu_problem(gpu)), key=strata.gpu_rank)
hip_cards = [] if cuda_cards else [gpu for gpu in strata.amd_gpus() if not strata.amd_problem(gpu)]
if not cuda_cards and not hip_cards:
    sys.exit("No GPU supported by this Strata source setup was found.")
if hip_cards and os.name == "nt":
    sys.exit("Windows AMD source builds need Strata's separate HIP build workflow.")

print("Installing Strata's Python runtime packages.", flush=True)
strata.pip_install(strata.runtime_packages(), "Strata Python runtime packages")
llama = strata.get_llama_cpp()
if cuda_cards:
    gpu = cuda_cards[0]
    driver = str(gpu.get("driver", "")).partition(".")[0]
    toolkit, _ = strata.cuda_choice([gpu["arch"]], driver=int(driver) if driver.isdecimal() else None)
    print(f"Preparing the Strata engine for {gpu['name']}.", flush=True)
    engine = strata.build_engine(gpu, "none", True, llama, toolkit=toolkit)
else:
    gpu = hip_cards[0]
    print(f"Preparing the Strata engine for {gpu['name']}.", flush=True)
    engine = strata.build_engine_hip(gpu, llama, "none")

executable = engine / strata.EXE
if not executable.is_file():
    sys.exit("Strata's build did not produce an engine executable.")
print(f"Engine ready: {executable}", flush=True)
"""
GITHUB_ORIGIN = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?",
    re.IGNORECASE,
)
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
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


def strata_latest():
    try:
        revision = strata_head(strata_directory())
    except (ValueError, OSError, subprocess.SubprocessError):
        return {"latest": None, "update_available": False}
    latest, update_available = github_revision(STRATA_REPO, STRATA_BRANCH, revision)
    return {"latest": latest, "update_available": update_available}


def strata_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def managed_environment(root: Path) -> dict[str, str]:
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
    return env


def strata_models():
    try:
        root = strata_directory()
        if not strata_python(root).is_file() or not strata_head(root):
            return {"error": "Set up the Strata engine to see model suggestions."}
        env = managed_environment(root)
        env["STRATA_NO_ACQUIRE"] = "1"
        result = subprocess.run(
            [str(strata_python(root)), "-B", "-c", STRATA_CATALOG_READER],
            cwd=root / "source", env=env, stdin=subprocess.DEVNULL, capture_output=True,
            encoding="utf-8", timeout=45, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return {"error": "Strata could not read its hardware and model catalog."}


def hf_environment(root):
    env = managed_environment(root)
    env["HF_ENDPOINT"] = "https://huggingface.co"
    # The Hub client resolves the existing login and download cache itself.
    if "HF_HOME" in os.environ:
        env["HF_HOME"] = os.environ["HF_HOME"]
    else:
        env.pop("HF_HOME", None)
    env.setdefault("HF_XET_CACHE", str(root / "hf-xet"))
    return env


def ensure_hf(root, env, log=None):
    python = str(strata_python(root))
    options = {"env": env, "cwd": root, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    probe = subprocess.run(
        [python, "-B", "-c", "from huggingface_hub import HfApi, hf_hub_download, get_token, try_to_load_from_cache"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, **options,
    )
    if probe.returncode:
        if log is not None:
            log.write(b"Installing the Hugging Face client in the managed venv.\n")
        subprocess.run(
            [python, "-m", "pip", "--disable-pip-version-check", "--no-input", "install", "huggingface-hub>=1,<2"],
            stdout=log if log is not None else subprocess.DEVNULL, stderr=subprocess.STDOUT,
            timeout=100, check=True, **options,
        )
    return python


def hf_files(payload):
    from hermes_cli.plugins_auto_update import plugin_update_lock
    from hermes_cli.plugins_cmd import PluginOperationError

    try:
        payload = payload or {}
        repo = runpy.run_path(str(MODEL_HELPER))["validate_repo"](payload.get("repo"))
        filename = payload.get("filename") or ""
        if not isinstance(filename, str) or len(filename) > 750:
            raise ValueError("Enter a GGUF filename or quantization.")
        root = strata_directory()
        if not strata_python(root).is_file():
            raise ValueError("Set up Strata’s venv first.")
        with plugin_update_lock(root):
            env = hf_environment(root)
            python = ensure_hf(root, env)
            result = subprocess.run(
                [python, "-B", str(MODEL_HELPER), "files", str(root)], cwd=root,
                input=json.dumps({"repo": repo, "filename": filename.strip()}),
                capture_output=True, encoding="utf-8", env=env, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        if result.returncode:
            error = result.stderr.strip()
            raise ValueError(error if error.startswith("Hugging Face") or error.startswith("Could not read")
                             else "Could not read Hugging Face files.")
        return {**json.loads(result.stdout), "existing_cli": bool(shutil.which("hf"))}
    except ValueError as exc:
        return {"error": str(exc)}
    except PluginOperationError:
        return {"error": "Wait for the current Strata operation to finish."}
    except (OSError, subprocess.SubprocessError):
        return {"error": "The Hugging Face client could not be prepared or the Hub did not respond."}


def downloaded_models(root):
    records = []
    for marker in (root / "models").glob("*/model.json"):
        try:
            record = json.loads(marker.read_text(encoding="utf-8"))
            if (marker.parent.resolve() == marker.parent and isinstance(record, dict)
                    and isinstance(record.get("title"), str) and isinstance(record.get("id"), str)
                    and isinstance(record.get("files"), list) and record["files"]
                    and all((marker.parent / name).is_file() for name in record["files"])):
                records.append(record)
        except (OSError, ValueError, TypeError):
            continue
    return sorted(records, key=lambda record: record["title"].casefold())


def profile_directory(root):
    directory = root.parent / "model-profiles"
    if directory.resolve() != directory:
        raise ValueError("The model profiles folder moved outside the plugin’s data directory.")
    return directory


def model_library():
    try:
        root = strata_directory()
        models_folder = root / "models"
        if models_folder.resolve() != models_folder:
            raise ValueError("The managed models folder moved outside the Strata installation.")
        directory = profile_directory(root)
        profiles = []
        for file in directory.glob("*.json"):
            try:
                profile = json.loads(file.read_text(encoding="utf-8"))
                if isinstance(profile, dict) and profile.get("id") == file.stem and isinstance(profile.get("name"), str):
                    profiles.append(profile)
            except (OSError, ValueError):
                continue
        return {"models": downloaded_models(root), "models_folder": str(models_folder),
                "profiles": sorted(profiles, key=lambda p: p["name"].casefold())}
    except (OSError, ValueError):
        return {"error": "Could not read managed models and profiles."}


PROFILE_OPTIONS = {
    "--max-context": (int, 1),
    "--memory-limit-mib": (int, 0),
    "--resident-budget-gib": (float, 0.001),
    "--vram-reserve-mib": (int, 0),
}


def profile_arguments(value):
    if not isinstance(value, list) or any(not isinstance(arg, str) or "\x00" in arg for arg in value):
        raise ValueError("Arguments must be a JSON array of strings, one element per argument.")
    return value


def save_model_profile(payload):
    try:
        import math

        payload = payload or {}
        profile_id = payload.get("id") or uuid4().hex
        name, selected_model = payload.get("name"), payload.get("model_id")
        if not isinstance(profile_id, str) or not re.fullmatch(r"[a-f0-9]{32}", profile_id):
            raise ValueError("Invalid model profile ID.")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise ValueError("Enter a profile name up to 120 characters.")
        if selected_model is not None and (not isinstance(selected_model, str) or len(selected_model) > 1200):
            raise ValueError("Invalid model selection.")
        controls = payload.get("controls") or {}
        if not isinstance(controls, dict) or any(flag not in PROFILE_OPTIONS for flag in controls):
            raise ValueError("Invalid resource option.")
        user_args = []
        for flag, value in controls.items():
            kind, minimum = PROFILE_OPTIONS[flag]
            if value in (None, ""):
                continue
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError(f"Invalid value for {flag}.")
            parsed = kind(value) if not isinstance(value, float) or kind is float else math.nan
            if not math.isfinite(parsed) or parsed < minimum:
                raise ValueError(f"Invalid value for {flag}.")
            user_args.extend((flag, str(parsed)))
        arguments = {key: profile_arguments(payload.get(key, []))
                     for key in ("engine_args", "launch_args", "setup_args", "server_args")}
        env, config = payload.get("env", {}), payload.get("config", {})
        if (not isinstance(env, dict) or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                                              or not isinstance(value, str) or "\x00" in value for key, value in env.items())
                or not isinstance(config, dict)):
            raise ValueError("Environment and config must be JSON objects. Environment values must be strings.")
        profile = {"id": profile_id, "name": name.strip(), "model_id": selected_model, "controls": controls,
                   **arguments, "user_args": user_args + arguments["engine_args"], "env": env, "config": config}
        content = json.dumps(profile, indent=2, allow_nan=False)
        if len(content) > 256_000:
            raise ValueError("This model profile is too large.")
        directory = profile_directory(strata_directory())
        directory.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=directory) as temp:
            staged = Path(temp) / "profile.json"
            staged.write_text(content, encoding="utf-8")
            os.replace(staged, directory / f"{profile_id}.json")
        return {"ok": True, "profile": profile}
    except (TypeError, ValueError, OverflowError) as exc:
        return {"ok": False, "error": str(exc)}
    except OSError:
        return {"ok": False, "error": "Could not save the model profile."}


def model_option_reference():
    try:
        root = strata_directory()
        source = root / "source"
        python = str(strata_python(root))
        engine = next((source / name / ("strata.exe" if os.name == "nt" else "strata")
                       for name in ("engine", "engine-cuda12")
                       if (source / name / ("strata.exe" if os.name == "nt" else "strata")).is_file()), None)
        commands = {"Engine": [str(engine), "--help"] if engine else None,
                    "Setup": [python, "-B", "setup.py", "--help"],
                    "Launch": [python, "-B", "strata.py", "launch", "--help"],
                    "Server": [python, "-B", "-m", "serve.server", "--help"]}
        reference = {}
        for title, command in commands.items():
            if command:
                result = subprocess.run(command, cwd=source, env=managed_environment(root), capture_output=True,
                                        encoding="utf-8", errors="replace", timeout=12,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                output = (result.stdout or result.stderr).strip()
                if output and (result.returncode == 0 or output.startswith("strata generate ")):
                    reference[title] = output
        registry = json.loads((source / "data" / "settings-register.json").read_text(encoding="utf-8"))
        reference["Environment"] = "\n".join(
            " · ".join((item["name"], str(item.get("status") or ""), ", ".join(item.get("backends", [])))).rstrip(" ·")
            for item in registry["settings"] if item.get("name")
        )
        return {"sections": reference}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return {"error": "Could not read this Strata build’s option reference."}


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
    env = managed_environment(root)
    options = {
        "cwd": source, "env": env, "stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0), "check": True,
    }
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")], **options)
    env["VIRTUAL_ENV"] = str(root / ".venv")
    env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
    subprocess.run(
        [str(python), "-c", STRATA_SETUP_RUNNER],
        **options,
    )
    prepared.write_text(git_at(source, "rev-parse", "HEAD"), encoding="ascii")


def run_strata_job(root: Path, operation: str, payload: dict | None) -> None:
    try:
        from hermes_cli.plugins_auto_update import plugin_update_lock
        from hermes_cli.plugins_cmd import PluginOperationError

        try:
            with plugin_update_lock(root):
                change_strata(root, operation, payload)
        except PluginOperationError:
            # Another Hermes process already owns the managed directory.
            return
    finally:
        with strata_jobs_lock:
            strata_jobs.pop(root, None)


def change_strata(root: Path, operation: str, payload: dict | None) -> None:
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
            try:
                if operation == "download":
                    log.write(b"Preparing the model download.\n")
                    env = hf_environment(root)
                    python = ensure_hf(root, env, log)
                    subprocess.run(
                        [python, "-B", str(MODEL_HELPER), "download", str(root)], cwd=root,
                        input=json.dumps(payload), stdout=log, stderr=subprocess.STDOUT, env=env,
                        encoding="utf-8", check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                else:
                    log.write(b"Preparing the managed Strata checkout and Python environment.\n")
                    prepare_strata(root, operation, log)
            except subprocess.CalledProcessError as exc:
                if exc.stderr:
                    log.write(exc.stderr.encode("utf-8", errors="replace"))
                message = "Model download failed." if operation == "download" else "Strata setup failed."
                raise ValueError(f"{message} See the activity log for details.") from exc
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Could not complete the Strata operation."
        if root.is_dir() and root.resolve() == root:
            (root / "setup-error.txt").write_text(message, encoding="utf-8")


def strata_action(operation: str, payload: dict | None = None):
    try:
        root = strata_directory()
        if operation == "download":
            if not strata_python(root).is_file() or not strata_head(root):
                raise ValueError("Set up the Strata runtime before downloading a model.")
            payload = runpy.run_path(str(MODEL_HELPER))["model_selection"](payload)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "error": "Could not access the managed Strata folder."}
    with strata_jobs_lock:
        if root in strata_jobs:
            return {"ok": False, "error": "A Strata operation is already running."}
        strata_jobs[root] = operation
        context = copy_context()
        try:
            Thread(target=context.run, args=(run_strata_job, root, operation, payload), daemon=True).start()
        except RuntimeError:
            strata_jobs.pop(root, None)
            return {"ok": False, "error": "Could not start the Strata operation."}
    return {"ok": True, "operation": operation}


GET_HANDLERS = {"status": strata_status, "latest": strata_latest, "models": strata_models,
                "library": model_library, "model-options": model_option_reference}
POST_HANDLERS = {action: partial(strata_action, action) for action in ("setup", "update", "wipe", "download")}
POST_HANDLERS.update({"hf-files": hf_files, "save-profile": save_model_profile})
