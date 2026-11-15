"""Loopback provider and lifecycle for Strata processes."""

import json
import os
import re
import secrets
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.request import Request, urlopen

import psutil


PROVIDER = "strata-backend"
PROVIDER_NAME = "Strata Backend"
ENGINE_NAMES = {"strata", "strata.exe", "strata-dsv41", "strata-dsv41.exe"}


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=path.parent) as directory:
        staged = Path(directory) / "state.json"
        staged.write_text(json.dumps(value, indent=2), encoding="utf-8")
        staged.chmod(0o600)
        os.replace(staged, path)


def service_root(root):
    path = root / "serving"
    if path.resolve() != path:
        raise ValueError("The Strata serving folder moved outside the managed installation.")
    return path


def profile_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Choose a saved model profile.")
    return value


def model_roster(library):
    models = {item["id"]: item for item in library.get("models", [])}
    roster = {}
    for profile in library.get("profiles", []):
        model = models.get(profile.get("model_id"))
        if model is None:
            continue
        key = profile_id(profile["id"])
        label = re.sub(r"[^A-Za-z0-9_.-]+", "-", profile["name"]).strip("-.") or "profile"
        alias = f"{label[:80]}-{key[:8]}"
        context = profile.get("controls", {}).get("--max-context")
        roster[alias] = {"profile_id": key, "name": profile["name"], "model_title": model["title"],
                         **({"context_length": int(context)} if context else {})}
    return roster


def instance_file(directory, key):
    return directory / "instances" / f"{profile_id(key)}.json"


def process_for(record):
    try:
        process = psutil.Process(int(record["pid"]))
        if abs(process.create_time() - float(record["started"])) < 0.01 and process.status() != psutil.STATUS_ZOMBIE:
            return process
    except (psutil.Error, KeyError, TypeError, ValueError):
        pass
    return None


def loopback_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def spawn(command, cwd, env, log):
    return subprocess.Popen(
        command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt",
    )


def process_record(process):
    return {"pid": process.pid, "started": psutil.Process(process.pid).create_time()}


def online(record, path, timeout=0.6):
    try:
        request = Request(f"http://127.0.0.1:{int(record['port'])}{path}",
                          headers={"Authorization": f"Bearer {record['key']}"})
        with urlopen(request, timeout=timeout) as result:
            if path == "/health":
                health = json.load(result)
                return health.get("service") == "strata" and health.get("model") == record.get("model") and bool(health.get("loaded"))
            return result.status == 200
    except (OSError, KeyError, TypeError, ValueError):
        return False


def write_provider_config(provider, roster):
    from hermes_cli.config import _CONFIG_LOCK, get_config_path, require_readable_config_before_write, save_config

    with _CONFIG_LOCK:
        config = require_readable_config_before_write(get_config_path())
        providers = config.setdefault("providers", {})
        existing = providers.get(PROVIDER)
        if existing and (not isinstance(existing, dict) or existing.get("api_key") != provider.get("key")):
            raise ValueError("A different Hermes provider already uses the Strata Backend name.")
        if roster:
            providers[PROVIDER] = {
                "name": PROVIDER_NAME, "api": f"http://127.0.0.1:{provider['port']}/v1",
                "api_key": provider["key"], "transport": "chat_completions", "discover_models": False,
                "models": {alias: ({"context_length": item["context_length"]} if "context_length" in item else {})
                           for alias, item in roster.items()},
            }
        elif existing:
            del providers[PROVIDER]
        save_config(config)
        check = require_readable_config_before_write(get_config_path()).get("providers", {})
        if roster and check.get(PROVIDER, {}).get("api_key") != provider["key"]:
            raise ValueError("Hermes did not save the Strata provider configuration.")


def sync_provider(root, roster):
    directory = service_root(root)
    provider = read_json(directory / "provider.json")
    if provider.get("key"):
        write_provider_config(provider, roster)
        write_json(directory / "models.json", roster)


def configure_provider(root, roster):
    if not roster:
        raise ValueError("Save a profile linked to a downloaded model first.")
    directory = service_root(root)
    file = directory / "provider.json"
    provider = read_json(file)
    process = None
    try:
        if process_for(provider) is None:
            provider = {"port": loopback_port(), "key": provider.get("key") or secrets.token_urlsafe(32)}
            write_json(file, provider)
            write_json(directory / "models.json", roster)
            with (directory / "provider.log").open("ab") as log:
                process = spawn([sys.executable, "-B", __file__, "bridge", str(directory)], directory, os.environ.copy(), log)
            provider.update(process_record(process))
            write_json(file, provider)
            deadline = monotonic() + 10
            while not online(provider, "/v1/models"):
                if process.poll() is not None or monotonic() >= deadline:
                    raise ValueError("The local Strata provider did not start. See the provider log.")
                sleep(0.2)
        elif not online(provider, "/v1/models"):
            raise ValueError("The Strata provider is not responding. Stop its process and set it up again.")
        sync_provider(root, roster)
    except (OSError, ValueError):
        if process is not None and process.poll() is None:
            process.terminate()
        raise
    return {"ok": True, "models": list(roster)}


def catalog_shards(root, model):
    import hashlib

    folder = root / "models" / hashlib.sha256(model["id"].encode("utf-8")).hexdigest()[:20]
    paths = [(folder / name).resolve() for name in model["files"] if isinstance(name, str) and name.lower().endswith(".gguf")]
    if not paths or any(not path.is_relative_to(folder) or not path.is_file() for path in paths):
        raise ValueError("The downloaded GGUF files are missing or outside Strata’s managed folder.")
    weights = [path for path in paths if not path.name.lower().startswith("mmproj")]
    if not weights or len({p.parent for p in weights}) != 1:
        raise ValueError("Starting this model requires its GGUF weights in one folder.")
    return weights


def reject_routing_args(args, reserved):
    for arg in args:
        if arg.split("=", 1)[0] in reserved:
            raise ValueError(f"{arg.split('=', 1)[0]} is controlled by the managed Strata instance.")


def windows_commit_available():
    if os.name != "nt":
        return None
    import ctypes
    from ctypes.wintypes import DWORD

    class PerformanceInformation(ctypes.Structure):
        _fields_ = [("size", DWORD), *((name, ctypes.c_size_t) for name in (
            "commit_total", "commit_limit", "commit_peak", "physical_total", "physical_available", "system_cache",
            "kernel_total", "kernel_paged", "kernel_nonpaged", "page_size",
        )), ("handle_count", DWORD), ("process_count", DWORD), ("thread_count", DWORD)]

    info = PerformanceInformation()
    size = ctypes.sizeof(info)
    if ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(info), size):
        return max(0, info.commit_limit - info.commit_total) * info.page_size
    return None


def prepare_config(root, profile, model, destination):
    source = root / "source"
    sys.path.insert(0, str(source))
    import setup as strata

    weights = catalog_shards(root, model)
    if model["source"] == "catalog":
        family, size = model["family"], model["model"]
    else:
        match = strata.gguf_choice(weights[0].name)
        if match is None:
            raise ValueError("This external GGUF does not match a Strata catalog pack. Its serving compatibility needs a separate test.")
        family, size = match
    fam = (strata.FAMILIES.get(family) or strata.OWN_PATH_FAMILIES.get(family) or {})
    if not fam or fam.get("ready"):
        raise ValueError("This model needs a separate Strata engine or ready-pack workflow.")
    extra = [*profile.get("launch_args", []), *profile.get("setup_args", [])]
    reject_routing_args(extra, {"--host", "--port", "--api-key", "--family", "--model", "--gguf-dir", "--models-dir",
                                "--data-dir", "--move-data", "--set-default-data-dir", "--rollback-engine", "--update",
                                "--check", "--inspect", "--prepare-pack"})
    commit_available = windows_commit_available()
    arena = float(strata.MODELS.get(size, {}).get("arena_gb") or 0) * 1e9
    if (commit_available is not None and arena and commit_available < arena
            and not any(arg.split("=", 1)[0] == "--low-ram" for arg in extra)):
        print(f"Windows commit headroom is {commit_available / 1e9:.1f} GB for a {arena / 1e9:.1f} GB expert arena. "
              "Using Strata's resident low-RAM mode for this launch.", flush=True)
        extra = ["--low-ram", "resident", *extra]
    sys.argv = [str(source / "setup.py"), "--setup", "--yes", "--build", "--no-start", "--no-browser",
                "--host", "127.0.0.1", "--vision", "none", "--mtp", "off", "--family", family, "--model", size,
                "--gguf-dir", str(weights[0].parent), "--data-dir", str(root / "run-data"), *extra]
    context = profile.get("controls", {}).get("--max-context")
    if context:
        sys.argv.extend(("--context", str(context)))
    if strata.main():
        raise ValueError("Strata could not prepare this model.")
    config_path = source / f"strata-{(fam['tag'] + size).lower()}.json"
    config = read_json(config_path)
    if not config.get("exe") or not config.get("args"):
        raise ValueError("Strata setup did not produce a run config.")
    write_json(destination, config)


def start_profile(root, python, env, profile, model, alias, log):
    directory = service_root(root)
    key = profile_id(profile["id"])
    record_path = instance_file(directory, key)
    if process_for(read_json(record_path)):
        raise ValueError("This Strata profile is already running.")
    source = root / "source"
    config_path = directory / "configs" / f"{key}.json"
    spec_path = directory / "configs" / f"{key}.prepare.json"
    write_json(spec_path, {"profile": profile, "model": model})
    record = {"profile_id": key, "name": profile["name"], "model": alias, "phase": "preparing"}
    prepare = spawn([str(python), "-B", __file__, "prepare", str(root), str(spec_path), str(config_path)], source, env, log)
    write_json(record_path, {**record, **process_record(prepare)})
    if prepare.wait():
        raise ValueError("Strata could not prepare this profile. See the activity log.")

    config = read_json(config_path)
    engine = config["exe"]
    config.update(profile.get("config") or {})
    port, secret = loopback_port(), secrets.token_urlsafe(24)
    config.update(exe=engine, host="127.0.0.1", port=port, api_key=secret, model_name=alias, open_browser=False,
                  user_args=profile.get("user_args", []), env={**(config.get("env") or {}), **profile.get("env", {})})
    write_json(config_path, config)
    args = profile.get("server_args", [])
    reject_routing_args(args, {"--config", "--host", "--port", "--api-key", "--engine"})
    logfile = directory / "instances" / f"{key}.log"
    with logfile.open("wb") as server_log:
        server = spawn([str(python), "-B", "-m", "serve.server", "--engine", "strata", "--config", str(config_path),
                        "--host", "127.0.0.1", "--port", str(port), *args], source, env, server_log)
    write_json(record_path, {**record, **process_record(server), "phase": "starting", "port": port, "key": secret})
    log.write(f"Started {profile['name']} on loopback port {port}. Waiting for the engine to load.\n".encode("utf-8"))


def external_strata():
    for process in psutil.process_iter(["name", "exe", "cmdline", "create_time"]):
        try:
            info = process.info
            args = info.get("cmdline") or []
            name = (info.get("name") or "").lower()
            if name in ENGINE_NAMES and any(arg in {"--serve", "--native", "--pack", "generate"} for arg in args[1:]):
                yield process, info
            elif name.startswith("python") and ("serve.server" in args or any(arg.replace("\\", "/").endswith("/serve/server.py") for arg in args)):
                cwd = Path(process.cwd())
                if (cwd / "setup.py").is_file() and (cwd / "strata_control").is_dir():
                    yield process, info
        except (psutil.Error, OSError):
            continue


def instance_rows(root):
    directory = service_root(root)
    rows = []
    owned_pids = set()
    bridge = read_json(directory / "provider.json")
    if process_for(bridge):
        owned_pids.add(bridge["pid"])
        rows.append({"pid": bridge["pid"], "started": bridge["started"], "name": "Hermes provider bridge",
                     "managed": True, "state": "ready" if online(bridge, "/v1/models") else "starting", "provider": True})
    for file in (directory / "instances").glob("*.json"):
        config = read_json(file)
        process = process_for(config)
        state = ("ready" if online(config, "/health") else config.get("phase", "starting")) if process else "exited"
        if process:
            owned_pids.add(process.pid)
            try:
                owned_pids.update(child.pid for child in process.children(recursive=True))
            except psutil.Error:
                pass
        row = {"pid": config.get("pid"), "started": config.get("started"), "name": config.get("name"),
               "profile_id": config.get("profile_id"), "managed": True, "state": state, "port": config.get("port")}
        logfile = file.with_suffix(".log")
        if state in ("starting", "exited") and logfile.is_file():
            try:
                with logfile.open("rb") as handle:
                    handle.seek(max(0, logfile.stat().st_size - 1800))
                    row["log"] = handle.read(1800).decode("utf-8", errors="replace")
            except OSError:
                pass
        rows.append(row)
    for process, info in external_strata():
        if process.pid not in owned_pids:
            rows.append({"pid": process.pid, "started": info["create_time"], "name": info.get("name"),
                         "path": info.get("exe"), "managed": False, "state": "running"})
    return rows


def stop_instance(root, payload):
    payload = payload or {}
    row = next((item for item in instance_rows(root)
                if item["pid"] == payload.get("pid") and item["started"] == payload.get("started")
                and item["state"] != "exited"), None)
    process = process_for(row) if row else None
    if process is None:
        raise ValueError("That Strata process has already exited or changed identity.")
    try:
        children = process.children(recursive=True) if row["managed"] else []
        for candidate in (process, *reversed(children)):
            try:
                candidate.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs([process, *children], timeout=4)
        for candidate in alive:
            try:
                candidate.kill()
            except psutil.NoSuchProcess:
                pass
        _, remaining = psutil.wait_procs(alive, timeout=2)
        if remaining:
            raise ValueError("A Strata process did not exit. Refresh the process list and try again.")
    except psutil.AccessDenied as exc:
        raise ValueError("The current Hermes account cannot stop that Strata process.") from exc
    return {"ok": True}


def serve_bridge(directory):
    import httpx
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request as ApiRequest
    from fastapi.responses import StreamingResponse

    provider = read_json(directory / "provider.json")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def require_key(request):
        actual = request.headers.get("authorization", "")
        if not secrets.compare_digest(actual, f"Bearer {provider['key']}"):
            raise HTTPException(401, "Invalid Strata provider credential.")

    @app.get("/v1/models")
    async def list_models(request: ApiRequest):
        require_key(request)
        return {"object": "list", "data": [{"id": name, "object": "model", "owned_by": PROVIDER}
                                           for name in read_json(directory / "models.json")]}

    @app.post("/v1/chat/completions")
    async def chat(request: ApiRequest):
        require_key(request)
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "Expected an OpenAI chat request.") from exc
        alias = body.get("model") if isinstance(body, dict) else None
        if not isinstance(alias, str):
            raise HTTPException(400, "Choose a Strata model profile.")
        selected = read_json(directory / "models.json").get(alias)
        if not selected:
            raise HTTPException(404, "Unknown Strata profile. Refresh the Hermes model picker.")
        instance = read_json(instance_file(directory, selected["profile_id"]))
        if process_for(instance) is None or instance.get("model") != alias or not instance.get("port"):
            raise HTTPException(503, "Start this profile in the Strata Backend tab before sending a message.")
        client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=5), trust_env=False)
        try:
            upstream = await client.send(client.build_request(
                "POST", f"http://127.0.0.1:{int(instance['port'])}/v1/chat/completions", json=body,
                headers={"Authorization": f"Bearer {instance['key']}"},
            ), stream=True)
        except httpx.HTTPError:
            await client.aclose()
            raise HTTPException(503, "Strata is still starting or has stopped. Check its instance status.") from None

        async def response():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(response(), status_code=upstream.status_code,
                                 media_type=upstream.headers.get("content-type", "application/json"))

    uvicorn.run(app, host="127.0.0.1", port=int(provider["port"]), access_log=False)


if __name__ == "__main__":
    operation, folder, *args = sys.argv[1:]
    root = Path(folder)
    if operation == "bridge":
        serve_bridge(root)
    elif operation == "prepare":
        input_path, output_path = map(Path, args)
        inputs = read_json(input_path)
        prepare_config(root, inputs["profile"], inputs["model"], output_path)
