"""Loopback provider and lifecycle for Strata processes."""

import json
import os
import re
import runpy
import secrets
import socket
import subprocess
import sys
from threading import Thread
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep, time
from urllib.request import Request, urlopen

import psutil


PROVIDER = "strata-backend"
PROVIDER_NAME = "Strata Backend"
ENGINE_NAMES = {"strata", "strata.exe", "strata-dsv41", "strata-dsv41.exe", "strata-inkling", "strata-inkling.exe"}
PROVIDER_PROTOCOL = 6
PROVIDER_REVISION = 10
DESKTOP_LEASE_SECONDS = 65
paths = runpy.run_path(str(Path(__file__).with_name("runtime_paths.py")))
terminal_directory, engine_sources = paths["terminal_directory"], paths["engine_sources"]


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
    models = {item["id"]: item for item in library.get("models", []) if item.get("available") is not False}
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


def touch_desktop(root, payload):
    client = (payload or {}).get("client")
    if not isinstance(client, str) or not re.fullmatch(r"[a-f0-9-]{36}", client):
        raise ValueError("Invalid Hermes Desktop window identity.")
    model = payload.get("model")
    if model is not None and (not isinstance(model, str) or len(model) > 512):
        raise ValueError("Invalid Hermes model selection.")
    directory = service_root(root) / "windows"
    path = directory / f"{client}.json"
    previous = read_json(path)
    if model is None:
        model = previous.get("model", "")
    sequence = payload.get("sequence", 0)
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise ValueError("Invalid Hermes model selection order.")
    if sequence and sequence <= previous.get("sequence", 0):
        return
    now = time()
    order = now if payload.get("focus") or previous.get("model") != model or not previous.get("order") else previous["order"]
    same_model = previous.get("model") == model
    load = payload.get("load") is not False or (same_model and previous.get("load", True) and bool(previous))
    write_json(path, {"model": model, "seen": now, "order": order, "sequence": sequence, "closed": payload.get("closed") is True,
                      "profile": payload.get("session_profile", payload.get("profile")),
                      "load": load,
                      "intent": previous["intent"] if same_model and previous.get("intent") else secrets.token_hex(12)})
    if payload.get("closed") is True and desktop_window_choice(service_root(root))[0] is None:
        (service_root(root) / "warm-request.json").unlink(missing_ok=True)


def desktop_window_choice(directory):
    windows = [read_json(path) for path in (directory / "windows").glob("*.json")]
    cutoff = time() - DESKTOP_LEASE_SECONDS
    active = [window for window in windows if not window.get("closed") and window.get("seen", 0) >= cutoff]
    return max(active, key=lambda window: window.get("order", 0)) if active else None, bool(windows)


def retain_shared_model(root, lease):
    directory = service_root(root)
    if read_json(directory / "hold.json").get("intent") == lease["intent"]:
        return
    window, _ = desktop_window_choice(directory)
    previous = lease.get("desktop")
    if window and window["model"] == lease["model"]:
        write_json(directory / "demand.json", {"selection": window["intent"], "intent": lease["intent"]})
    elif window:
        if not previous or (window["intent"], window.get("order")) != (previous["intent"], previous.get("order")):
            return
    elif previous:
        return
    write_json(directory / "warm-request.json",
               {"model": lease["model"], "intent": lease["intent"], "load": True, "desktop": window})


def desktop_choice(root, *, include_dormant=False, request_alias=None, requested_at=0):
    directory = service_root(root)
    window, tracked = desktop_window_choice(directory)
    choice = window
    warm = read_json(directory / "warm-request.json")
    warm_selected = False
    request = read_json(directory / "active-request.json")
    if request.get("seen", 0) >= time() - DESKTOP_LEASE_SECONDS:
        choice = request
    elif warm:
        previous = warm.get("desktop")
        if window and window["model"] == warm["model"]:
            warm_selected = True
        elif (window and previous and (window["intent"], window.get("order")) == (previous["intent"], previous.get("order"))
              or not window and not previous):
            choice = warm
        else:
            (directory / "warm-request.json").unlink(missing_ok=True)
    if not choice:
        return None, tracked
    roster = read_json(directory / "models.json")
    alias = choice["model"]
    selected = roster.get(alias)
    if not selected:
        return None, False
    selection = choice["intent"]
    held = read_json(directory / "hold.json")
    demand = read_json(directory / "demand.json")
    intent = demand.get("intent", selection) if demand.get("selection") == selection else selection
    if held.get("intent") == intent:
        if request_alias != alias or requested_at <= held.get("stopped_at", 0):
            return None, False
        restart = held.get("restart")
        if restart is None and "stopped_at" not in held and not (root / "setup-error.txt").is_file():
            # Older plugin versions stored only the held intent for manual stops.
            restart = f"{intent}-resumed"
        if not restart:
            return None, False
        intent = restart
        demand = {"selection": selection, "intent": intent}
        write_json(directory / "demand.json", demand)
    loading = choice.get("load", True) or demand.get("intent") == intent or warm_selected
    if not loading and request_alias == alias:
        write_json(directory / "demand.json", {"intent": intent})
        loading = True
    if loading or include_dormant:
        return {**selected, "alias": alias, "intent": intent, "restarted": intent != selection,
                "request": choice.get("request", False)}, False
    return None, False


def hold_desktop_choice(root, intent, *, manual_stop=False):
    if intent:
        hold = {"intent": intent, "restart": None, "stopped_at": time()}
        if manual_stop:
            hold["restart"] = secrets.token_hex(12)
        write_json(service_root(root) / "hold.json", hold)


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


def loopback_port(preferred=0):
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", preferred))
        except OSError:
            listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def spawn(command, cwd, env, log):
    return subprocess.Popen(
        command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt",
    )


def process_record(process):
    return {"pid": process.pid, "started": psutil.Process(process.pid).create_time()}


def online(record, path, timeout=0.6, *, allow_unloaded=False):
    try:
        request = Request(f"http://127.0.0.1:{int(record['port'])}{path}",
                          headers={"Authorization": f"Bearer {record['key']}"})
        with urlopen(request, timeout=timeout) as result:
            if path == "/health":
                health = json.load(result)
                return health.get("service") == "strata" and health.get("model") == record.get("model") and (allow_unloaded or bool(health.get("loaded")))
            return result.status == 200
    except (OSError, KeyError, TypeError, ValueError):
        return False


def performance_stats(record):
    try:
        request = Request(f"http://127.0.0.1:{int(record['port'])}/metrics",
                          headers={"Authorization": f"Bearer {record['key']}", "Accept": "application/json"})
        with urlopen(request, timeout=0.6) as response:
            metrics = json.load(response)
        if metrics["engine"]["model"] != record["model"] and not online(record, "/health"):
            return None
        hardware, live = metrics["hardware"], metrics["live"]
        reading = [slot for slot in live.get("slots", []) if slot.get("state") == "reading"]
        prompt_read, prompt_total = live.get("prompt_read"), live.get("prompt_total")
        if prompt_total is None and reading:
            prompt_read = sum(slot.get("prompt_read") or 0 for slot in reading)
            prompt_total = sum(slot.get("prompt_tokens") or 0 for slot in reading)
        recent = []
        for sample in metrics["requests"][:3]:
            prompt_tokens, prompt_ms = sample.get("prompt_read"), sample.get("prompt_ms")
            recent.append({
                "generation_tps": sample.get("decode_tok_s"),
                "generated": sample.get("engine_generated"),
                "prompt_tps": round(1000 * prompt_tokens / prompt_ms, 1) if prompt_tokens and prompt_ms else None,
                "prompt_read": prompt_tokens,
                "prompt_total": sample.get("prompt_total"),
            })
        return {
            "generation_tps": live.get("tok_s_mean"),
            "prompt_tps": live.get("prefill_tok_s_mean"),
            "ram_used": hardware.get("ram_used"), "ram_total": hardware.get("ram_total"),
            "vram_used": hardware.get("gpu_mem_used"), "vram_total": hardware.get("gpu_mem_total"),
            "expert_cache_mib": metrics["engine"].get("expert_cache_mib"),
            "images": metrics["engine"].get("images"),
            "max_context": metrics["engine"].get("max_context"),
            "kv_format": metrics["engine"].get("kv_dtype") or metrics["engine"].get("kv"),
            "backend": metrics["engine"].get("backend"),
            "gpu_experts": metrics["engine"].get("vram_experts"),
            "ram_experts": metrics["engine"].get("ram_experts"),
            "engine_version": metrics["engine"].get("version"),
            "prompt_read": prompt_read, "prompt_total": prompt_total,
            "recent": recent,
        }
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        return None


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
        access = runpy.run_path(str(Path(__file__).with_name("profile_access.py")))
        for profile in access["provider_profiles"](root):
            with access["profile_scope"](profile.name):
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
        old_process = process_for(provider)
        if old_process is not None and (provider.get("protocol") != PROVIDER_PROTOCOL
                                        or (provider.get("revision") or 0) < PROVIDER_REVISION):
            stop_process_tree(old_process, managed=True)
        if process_for(provider) is None:
            provider = {"port": loopback_port(provider.get("port", 0)), "key": provider.get("key") or secrets.token_urlsafe(32),
                        "protocol": PROVIDER_PROTOCOL, "revision": PROVIDER_REVISION}
            write_json(file, provider)
            write_json(directory / "models.json", roster)
            with (directory / "provider.log").open("ab") as log:
                env = {**os.environ, "HERMES_HOME": str(root.parent.parent.parent)}
                process = spawn([sys.executable, "-B", __file__, "bridge", str(directory)], directory, env, log)
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

    if model.get("source") == "local":
        paths = [Path(name) for name in model["files"]]
        if not paths or any(not path.is_absolute() or path.suffix.lower() != ".gguf" or not path.is_file() for path in paths):
            raise ValueError("The local GGUF files are missing. Restore them or add the model from its new folder.")
        if len({path.parent for path in paths}) != 1:
            raise ValueError("Keep every shard of the local model in the same folder.")
        return paths
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


def prepare_vision_reader(root, mode, model, weights):
    if mode == "off":
        return None
    projector = Path(model.get("projector") or "")
    if not projector.is_absolute() or not projector.is_file():
        raise ValueError("Attach an existing vision projector to this model before enabling image input.")
    source = terminal_directory(root)
    sys.path.insert(0, str(source))
    import setup as strata

    if source.name == "terminal":
        from strata_control import engine_component

        found = engine_component.resolve(full=False)
        reader = found.root / ("strata-vision.exe" if os.name == "nt" else "strata-vision")
        if reader.is_file():
            return {"exe": str(reader), "mmproj": str(projector), "model": str(weights[0]),
                    "gpu": mode == "gpu", "max_tokens": 1024 if mode == "gpu" else 300}
    directory = root / "vision-readers" / mode
    executable = directory / ("strata-vision.exe" if os.name == "nt" else "strata-vision")
    stamp = directory / "build.json"
    revision = (root / "prepared-commit").read_text(encoding="ascii").strip()
    if not executable.is_file() or read_json(stamp).get("revision") != revision:
        print(f"Preparing the Strata vision reader ({mode}).", flush=True)
        llama = strata.get_llama_cpp() or root / "source" / "third_party" / "llama.cpp"
        definitions = [f"-DLLAMA_DIR={llama}", f"-DSTRATA_VISION_CUDA={'ON' if mode == 'gpu' else 'OFF'}",
                       "-DSTRATA_PORTABLE=OFF", *strata.host_compiler_defs(mode == "gpu")]
        vcvars = strata.find_vcvars() if os.name == "nt" else None
        if mode == "gpu":
            gpu = strata.gpu_info(0)
            nvcc, vcvars = strata.install_build_tools(gpu, True)
            definitions.extend((f"-DCMAKE_CUDA_ARCHITECTURES={gpu['arch']}", f"-DCMAKE_CUDA_COMPILER={nvcc}",
                                *strata.toolkit_root_defs(nvcc)))
        build = source / f"build-plugin-vision-{mode}"
        strata.cmake_build(engine_sources(source) / "tools" / "vision", build, "strata-vision", definitions, vcvars,
                           f"build-plugin-vision-{mode}.bat" if os.name == "nt" else "")
        built = build / "bin" / executable.name
        if not built.is_file():
            raise ValueError("The vision reader build finished without its executable. Check the activity log.")
        import shutil

        directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built, executable)
        write_json(stamp, {"revision": revision})
    return {"exe": str(executable), "mmproj": str(projector), "model": str(weights[0]),
            "gpu": mode == "gpu", "max_tokens": 1024 if mode == "gpu" else 300}


def prepare_external_flash_next(root, profile, weights, model=None):
    source = terminal_directory(root)
    tools = source / "tools"
    sys.path.insert(0, str(tools))
    from gguf_reader import GGUFFile

    headers = [GGUFFile(path) for path in weights]
    architecture = headers[0].metadata.get("general.architecture")
    if architecture != "qwen4exp":
        raise ValueError(f"External GGUF architecture {architecture or 'unknown'} is unsupported. The custom pack path requires Qwen Flash-Next (qwen4exp).")
    tables = [header.path for header in headers if any(tensor.name == "per_layer_token_embd.weight" for tensor in header.tensors)]
    if len(tables) != 1:
        raise ValueError("The Flash-Next GGUF shards must contain exactly one per-layer token embedding table.")
    if profile.get("setup_args") or profile.get("launch_args"):
        raise ValueError("For a custom Flash-Next pack, use Engine arguments and Server arguments in the profile.")

    binary = "strata.exe" if os.name == "nt" else "strata"
    component = None
    if source.name == "terminal":
        from strata_control import engine_component

        component = engine_component.resolve(full=False)
    engine = Path(component.exe) if component else next((source / name / binary for name in ("engine", "engine-cuda12")
                   if (source / name / binary).is_file()), None)
    if engine is None:
        raise ValueError("Set up the native Strata engine before preparing a custom Flash-Next pack.")
    gguf_py = source / "third_party" / "llama.cpp" / "gguf-py"
    if model:
        import hashlib

        model_folder = hashlib.sha256(model["id"].encode("utf-8")).hexdigest()[:20]
    else:
        model_folder = weights[0].relative_to(root / "models").parts[0]
    pack = root / "run-data" / "packs" / f"external-{model_folder}"
    if pack.resolve() != pack:
        raise ValueError("The custom pack folder moved outside the managed Strata installation.")
    revision = (root / "prepared-commit").read_text(encoding="ascii").strip()
    stamp = pack / "strata-revision.json"
    tokenizer = pack / "tokenizer"
    fingerprints = [{"path": str(path), "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns} for path in weights]
    expected = {"revision": revision, "weights": fingerprints}
    if (read_json(stamp) != expected or not (pack / "native_experts.txt").is_file()
            or not (tokenizer / "chat_template.jinja").is_file() or not (tokenizer / "vocab.json").is_file()):
        print("Preparing custom Flash-Next pack from the downloaded GGUF shards.", flush=True)
        subprocess.run([sys.executable, "-B", str(tools / "iq_pack.py"), "--gguf", str(weights[0]),
                        "--out", str(pack), "--compat-bf16"], cwd=source,
                       env={**os.environ, "STRATA_GGUF_PY": str(gguf_py)}, check=True)
        subprocess.run([sys.executable, "-B", str(tools / "strata_tokenizer.py"), "--gguf", str(weights[0]),
                        "--out", str(pack)], cwd=source,
                       env={**os.environ, "STRATA_GGUF_PY": str(gguf_py)}, check=True)
        write_json(stamp, expected)

    context = str(profile.get("controls", {}).get("--max-context") or 32768)
    args = ["--pack", str(pack), "--native", str(weights[0]), "--ple-gguf", str(tables[0]),
            "--expert-profile", str(source / "data" / "expert-profile.bin"), "--expert-cache", "auto",
            "--prefill", "auto", "--spec", "4", "--spec-min-p", "0.5", "--max-context", context,
            "--kv", "int8", "--kv-resident", str(min(int(context), 32768)), "--resident-experts"]
    draft = root / "run-data" / "mtp" / "rt"
    drafting = (profile.get("config") or {}).get("drafting", "off")
    if drafting == "mtp":
        if not (draft / "experts.bin").is_file():
            raise ValueError("Prepare the shared Flash-Next MTP draft layer before enabling it for this model.")
        args.extend(("--mtp", str(draft)))
    build = read_json(engine.parent / "BUILD.json")
    config = {"exe": "component:strata-engine" if component else str(engine), "cwd": str(source), "tokenizer": str(tokenizer),
              "log": str(pack / "engine.log"), "drafting": drafting, "args": args,
              "lib_dirs": engine_component.package_lib_dirs(component.root) if component else build.get("lib_dirs") or build.get("cuda_dirs") or []}
    mode = profile.get("vision_mode", "off")
    if mode != "off":
        config["vision"] = prepare_vision_reader(root, mode, model or {}, weights)
    return config


def prepare_inkling_config(root, profile, model, destination, strata):
    if model.get("model") not in getattr(strata, "INKLING_FAMILIES", {}).get("inkling-2-mini", {}).get("models", {}):
        raise ValueError("Update the Strata backend to v0.2.19 before preparing Inkling.")
    from strata_control import inkling_family
    import hashlib

    folder = root / "models" / hashlib.sha256(model["id"].encode("utf-8")).hexdigest()[:20]
    files = [(folder / name).resolve() for name in model["files"]]
    if any(not path.is_relative_to(folder) or not path.is_file() for path in files):
        raise ValueError("The Inkling model files are missing from their managed download.")
    weight = next((path for path in files if path.name == inkling_family.build_file(model["model"])), None)
    if weight is None:
        raise ValueError("Download the selected Inkling .strata pack before loading it.")
    controls = profile.get("controls") or {}
    if any(value not in (None, "") for flag, value in controls.items()
           if flag in {"--memory-limit-mib", "--resident-budget-gib"}):
        raise ValueError("Inkling uses native RAM placement. Clear the Qwen RAM controls for this profile.")
    extra = [*profile.get("setup_args", []), *profile.get("launch_args", [])]
    reject_routing_args(extra, {"--family", "--model", "--gguf-dir", "--data-dir", "--models-dir", "--feed",
                                "--host", "--port", "--api-key", "--build", "--update"})
    source = terminal_directory(root)
    sys.argv = [str(source / "setup.py"), "--setup", "--yes", "--no-start", "--no-browser", "--host", "127.0.0.1",
                "--family", "inkling-2-mini", "--model", model["model"], "--gguf-dir", str(weight.parent),
                "--data-dir", str(root / "run-data"), "--vision", "none" if profile.get("vision_mode", "gpu") == "off" else "yes",
                "--context", str(controls.get("--max-context") or inkling_family.DEFAULT_CONTEXT), *extra]
    print(f"Preparing {model['title']} with its native Strata engine.", flush=True)
    if strata.main():
        raise ValueError("Strata could not prepare the Inkling profile.")
    config = read_json(strata.home() / f"strata-{inkling_family.model_key(model['model'])}.json")
    if not config.get("exe") or not config.get("args"):
        raise ValueError("Inkling setup did not write a usable server config.")
    # Native setup shares a family config: do not inherit the previous preset's tuning.
    tuning = {"--kv", "--kv-dtype", "--draft", "--parallel", "--batch", "--slots", "--chunk", "--cpu-threads",
              "--vram-experts-gb", "--ram-experts-gb", "--vram-reserve-mib", "--gpu", "--gpus", "--ep-split"}
    from strata_control import user_args

    config["args"] = user_args.flat([group for group in user_args.groups(config["args"])
                                    if group[0].split("=", 1)[0] not in tuning])
    config["args_written"] = list(config["args"])
    config["user_args"] = []
    config["parallel"] = 1
    config.pop("gpu", None)
    from strata_control import shape_engine

    found = shape_engine.find("inkling2", full=False)
    write_json(root / "inkling-engine.json", {"exe": str(found.exe), "version": found.version})
    if int(controls.get("--max-context") or 32768) > 262144 and getattr(strata, "gpus", lambda: [])():
        build = runpy.run_path(str(Path(__file__).with_name("inkling_build.py")))
        config["exe"] = str(build["prepare_engine"](root, strata))
        config["lib_dirs"] = list(dict.fromkeys([str(Path(found.exe).parent), *config.get("lib_dirs", [])]))
        floor = int((profile.get("headroom") or {}).get("vram_mib", 0))
        # WDDM's CUDA free-memory view can run above nvidia-smi's global availability.
        workspace_floor = floor + 1024 if floor and os.name == "nt" else floor
        config["env"] = {**(config.get("env") or {}), "HERMES_INKLING_VRAM_FLOOR_MIB": str(workspace_floor)}
        config["hermes_inkling_cuda"] = True
    write_json(destination, config)


def prepare_config(root, profile, model, destination):
    source = terminal_directory(root)
    sys.path.insert(0, str(source))
    import setup as strata

    if model.get("family") == "inkling-2-mini":
        prepare_inkling_config(root, profile, model, destination, strata)
        return
    weights = catalog_shards(root, model)
    if model["source"] == "local":
        write_json(destination, prepare_external_flash_next(root, profile, weights, model))
        return
    if model["source"] == "catalog":
        family, size = model["family"], model["model"]
    else:
        match = strata.gguf_choice(weights[0].name)
        if match is None:
            write_json(destination, prepare_external_flash_next(root, profile, weights, model))
            return
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


def inkling_engine_args(profile, *, optimized=False):
    args = list(profile.get("user_args", []))
    options = {}
    for index, arg in enumerate(args):
        if arg.startswith("--"):
            flag, separator, value = arg.partition("=")
            options["--kv" if flag == "--kv-dtype" else flag] = value if separator else args[index + 1] if index + 1 < len(args) else ""
    hybrid = (int(options.get("--gpus", 1)) > 1 or options.get("--kv") in {"k8v8", "k8v4", "i8", "q8"}
              or not optimized and (int(options.get("--vram-reserve-mib", -1)) >= 0 or int(options.get("--cpu-threads", 0)) > 0))
    if not optimized and float(options.get("--vram-experts-gb", -1)) >= 0 and not hybrid:
        # The primary CUDA loader ignores the expert cap; select the native hybrid loader.
        args.extend(("--vram-reserve-mib", "0"))
        hybrid = True
    if hybrid:
        # 0.1.65 hybrid scratch buffers are sized for 64 hidden rows, not 64 wide projection rows.
        if int(options.get("--chunk", 16)) > 16 or int(options.get("--parallel", 1)) > 16:
            raise ValueError("Strata 0.1.65's Inkling offload path needs a prompt chunk and request slots of 16 or less. Use chunk 16 to avoid its native buffer error.")
        if "--chunk" not in options:
            args.extend(("--chunk", "16"))
    return args


def start_profile(root, python, env, profile, model, alias, log, intent=None):
    directory = service_root(root)
    key = profile_id(profile["id"])
    record_path = instance_file(directory, key)
    previous = read_json(record_path)
    if process_for(previous):
        if intent and previous.get("intent") == intent:
            return
        raise ValueError("This Strata profile is already running.")
    source = terminal_directory(root)
    config_path = directory / "configs" / f"{key}.json"
    spec_path = directory / "configs" / f"{key}.prepare.json"
    write_json(spec_path, {"profile": profile, "model": model})
    def current():
        if not intent:
            return True
        choice, _ = desktop_choice(root)
        return bool(choice and choice["alias"] == alias and (choice["intent"] == intent or choice.get("request")))

    if not current():
        return
    record = {"profile_id": key, "name": profile["name"], "model": alias, "phase": "preparing",
              "intent": intent}
    if model.get("family") == "inkling-2-mini" and profile.get("headroom"):
        record["headroom"] = profile["headroom"]
        floor = profile["headroom"].get("ram_gib", 0)
        if psutil.virtual_memory().available < floor * 2**30:
            raise ValueError(f"This Inkling profile needs at least {floor:g} GiB of free system RAM before loading.")
    prepare = spawn([str(python), "-B", __file__, "prepare", str(root), str(spec_path), str(config_path)], source, env, log)
    write_json(record_path, {**record, **process_record(prepare)})
    result = None
    while result is None:
        if not current():
            process = process_for(read_json(record_path))
            if process:
                stop_process_tree(process, managed=True)
            return
        try:
            result = prepare.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            continue
    if not current():
        return
    if result:
        raise ValueError("Strata could not prepare this profile. See the activity log.")

    config = read_json(config_path)
    optimized = config.pop("hermes_inkling_cuda", False)
    user_args = inkling_engine_args(profile, optimized=optimized) if model.get("family") == "inkling-2-mini" else profile.get("user_args", [])
    engine = config["exe"]
    vision = config.get("vision")
    config.update(profile.get("config") or {})
    if "vision_mode" in profile:
        if vision:
            config["vision"] = vision
        else:
            config.pop("vision", None)
    port, secret = loopback_port(), secrets.token_urlsafe(24)
    config.update(exe=engine, host="127.0.0.1", port=port, api_key=secret, model_name=alias, open_browser=False,
                  user_args=user_args, env={**(config.get("env") or {}), **profile.get("env", {})})
    write_json(config_path, config)
    args = profile.get("server_args", [])
    reject_routing_args(args, {"--config", "--host", "--port", "--api-key", "--engine"})
    logfile = directory / "instances" / f"{key}.log"
    if not current():
        return
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


def log_tail(path, limit):
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - limit))
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def preparation_progress(output):
    for line in reversed(output.splitlines()):
        if line.startswith("Preparing the Strata vision reader"):
            return {"stage": "preparing-vision"}
        if line == "Preparing custom Flash-Next pack from the downloaded GGUF shards.":
            return {"stage": "packing-model"}
        if line.lstrip().startswith("> "):
            if "inkling-cuda" in line and "build.bat" in line:
                return {"stage": "building-inkling"}
            if "build-plugin-vision-" in line:
                return {"stage": "preparing-vision"}
            tool = re.search(r"\bmtp_(fetch|pack|rt)\.py\b", line)
            if tool:
                return {"stage": "downloading-mtp" if tool[1] == "fetch" else "packing-mtp"}
            return {}
        progress = re.fullmatch(r"\s*MTP draft layer:\s+([\d.,]+\s*/\s*[\d.,]+\s+GB\s*\(\d+%\))\s*", line)
        if progress:
            return {"stage": "downloading-mtp", "detail": progress[1]}
        if line.lstrip().startswith(("===", "[ok] MTP draft layer:")):
            return {}
    return {}


def clear_exited_instances(root):
    instances = service_root(root) / "instances"
    if instances.resolve() != instances:
        raise ValueError("The Strata process records moved outside the managed installation.")
    for file in instances.glob("*.json"):
        record = read_json(file)
        if record.get("pid") and not record.get("dismissed") and not process_for(record):
            # Keep the launch intent so hiding a failed launch cannot make the heartbeat retry it.
            write_json(file, {**record, "dismissed": True})


def instance_rows(root, *, include_stats=False):
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
        if not process and config.get("dismissed"):
            continue
        state = ("ready" if online(config, "/health") else config.get("phase", "starting")) if process else "exited"
        if process:
            owned_pids.add(process.pid)
            try:
                owned_pids.update(child.pid for child in process.children(recursive=True))
            except psutil.Error:
                pass
        row = {"pid": config.get("pid"), "started": config.get("started"), "name": config.get("name"),
               "profile_id": config.get("profile_id"), "managed": True, "state": state, "port": config.get("port")}
        if config.get("headroom"):
            row["headroom"] = config["headroom"]
        if config.get("stop_reason"):
            row["stop_reason"] = config["stop_reason"]
        logfile = file.with_suffix(".log")
        if state == "ready" and include_stats:
            row["stats"] = performance_stats(config)
        elif state == "preparing":
            row.update(preparation_progress(log_tail(root / "setup.log", 16384)))
        elif state in ("starting", "exited"):
            row["log"] = log_tail(logfile, 1800)
            if state == "starting":
                row["stage"] = "loading-model"
        rows.append(row)
    for process, info in external_strata():
        if process.pid not in owned_pids:
            rows.append({"pid": process.pid, "started": info["create_time"], "name": info.get("name"),
                         "path": info.get("exe"), "managed": False, "state": "running"})
    return rows


def protect_inkling_headroom(root):
    directory = service_root(root)
    for file in (directory / "instances").glob("*.json"):
        record = read_json(file)
        limits = record.get("headroom")
        if not limits or not process_for(record) or not online(record, "/health"):
            continue
        reason = None
        ram_free = psutil.virtual_memory().available / 2**30
        if ram_free < limits.get("ram_gib", 0):
            reason = f"System RAM headroom fell below {limits['ram_gib']:g} GiB ({ram_free:.1f} GiB available)."
        stats = performance_stats(record) if limits.get("vram_mib") else None
        if stats and stats.get("vram_total") is not None and stats.get("vram_used") is not None:
            vram_free = (stats["vram_total"] - stats["vram_used"]) / 2**20
            if vram_free < limits["vram_mib"]:
                reason = f"GPU headroom fell below {limits['vram_mib']:g} MiB ({vram_free:.0f} MiB available)."
        if reason:
            stop_instance(root, record)
            write_json(file, {**read_json(file), "stop_reason": f"Stopped to preserve memory: {reason}"})


def stop_instance(root, payload):
    payload = payload or {}
    row = next((item for item in instance_rows(root)
                if item["pid"] == payload.get("pid") and item["started"] == payload.get("started")
                and item["state"] != "exited"), None)
    process = process_for(row) if row else None
    if process is None:
        raise ValueError("That Strata process has already exited or changed identity.")
    if row.get("profile_id"):
        selection, _ = desktop_choice(root, include_dormant=True)
        if selection and selection["profile_id"] == row["profile_id"]:
            hold_desktop_choice(root, selection["intent"], manual_stop=True)
    stop_process_tree(process, managed=row["managed"])
    return {"ok": True}


def stop_process_tree(process, *, managed):
    try:
        children = process.children(recursive=True) if managed else []
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


def stop_unselected(root, selected):
    directory = service_root(root)
    for file in (directory / "instances").glob("*.json"):
        instance = read_json(file)
        latest, _ = desktop_choice(root)
        if latest != selected:
            return
        if latest and (instance.get("profile_id"), instance.get("model")) == (latest["profile_id"], latest["alias"]):
            continue
        process = process_for(instance)
        if process:
            stop_process_tree(process, managed=True)


def launch_shared_profile(root, selected, intent):
    from hermes_cli.plugins_auto_update import plugin_update_lock
    from hermes_cli.plugins_cmd import PluginOperationError

    try:
        with plugin_update_lock(root):
            instance = read_json(instance_file(service_root(root), selected["profile_id"]))
            if not process_for(instance):
                adapter = runpy.run_path(str(Path(__file__).with_name("plugin_api.py")))
                adapter["runtime"]()["change_strata"](root, "serve", {"id": selected["profile_id"], "intent": intent})
                error = root / "setup-error.txt"
                if error.is_file():
                    raise ValueError(error.read_text(encoding="utf-8"))
            return True
    except PluginOperationError:
        return False


def serve_bridge(directory):
    import asyncio
    import httpx
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request as ApiRequest
    from fastapi.responses import StreamingResponse

    provider = read_json(directory / "provider.json")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    requests = asyncio.Lock()

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
        root = directory.parent
        if not read_json(root.parent / "profile-access.json").get("profiles"):
            return await forward_chat(request, body, alias, selected)

        # One shared engine serves bot requests in order. Its request lease also covers unfocused bots.
        await requests.acquire()
        window, _ = desktop_window_choice(directory)
        lease = {"model": alias, "intent": secrets.token_hex(12), "seen": time(), "load": True, "request": True,
                 "desktop": window}
        lease_path = directory / "active-request.json"
        write_json(lease_path, lease)

        async def keep_alive():
            while True:
                await asyncio.sleep(10)
                if await request.is_disconnected():
                    if read_json(lease_path).get("intent") == lease["intent"]:
                        lease_path.unlink(missing_ok=True)
                    return
                write_json(lease_path, {**lease, "seen": time()})

        heartbeat = asyncio.create_task(keep_alive())

        async def release(keep_loaded=False):
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            try:
                if read_json(lease_path).get("intent") == lease["intent"]:
                    if keep_loaded:
                        retain_shared_model(root, lease)
                    lease_path.unlink(missing_ok=True)
            finally:
                requests.release()

        try:
            response = await forward_chat(request, body, alias, selected, request_intent=lease["intent"])
        except BaseException:
            await release()
            raise
        upstream = response.body_iterator

        async def response_body():
            completed = False
            try:
                async for chunk in upstream:
                    yield chunk
                completed = response.status_code < 400 and not getattr(response, "strata_state", {}).get("failed")
            finally:
                await release(keep_loaded=completed)

        response.body_iterator = response_body()
        return response

    async def forward_chat(request, body, alias, selected, request_intent=None):
        deadline = monotonic() + 480
        # Session confirmation and the desktop heartbeat can arrive after the first prompt.
        selection_grace = monotonic() + DESKTOP_LEASE_SECONDS
        requested_at = time()
        selected_intent = None
        root = directory.parent
        launched = request_intent is None
        while True:
            if not launched:
                try:
                    launched = await asyncio.to_thread(launch_shared_profile, root, selected, request_intent)
                except (ValueError, OSError) as exc:
                    raise HTTPException(503, str(exc) if isinstance(exc, ValueError) else "Could not start the shared Strata profile.") from None
            instance = read_json(instance_file(directory, selected["profile_id"]))
            tracked = (directory / "windows").is_dir()
            choice, _ = desktop_choice(root, request_alias=alias if selected_intent is None else None,
                                       requested_at=requested_at)
            matching = bool(choice and choice["alias"] == alias)
            if matching and selected_intent is None:
                selected_intent = choice["intent"]
            if selected_intent and (not matching or choice["intent"] != selected_intent):
                raise HTTPException(409, "The Strata profile was deselected or stopped in Hermes Desktop.")
            process = process_for(instance)
            same_instance = instance.get("model") == alias and (not tracked or (
                matching and (not choice.get("restarted") or instance.get("intent") == choice["intent"])))
            if process and same_instance and instance.get("port") and await asyncio.to_thread(online, instance, "/health", allow_unloaded=True):
                break
            if not matching and monotonic() >= selection_grace:
                raise HTTPException(409, "Hermes Desktop did not confirm this Strata profile. Select it in the model picker and retry.")
            if matching and same_instance and instance.get("intent") == choice["intent"] and instance.get("pid") and not process:
                raise HTTPException(503, instance.get("stop_reason") or "Strata could not start this profile. Check the activity log in Strata Backend.")
            if monotonic() >= deadline:
                raise HTTPException(503, "Strata is still loading this profile. Check the activity log and retry.")
            if await request.is_disconnected():
                raise HTTPException(499, "Hermes disconnected while Strata was loading.")
            await asyncio.sleep(0.5)
        client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=5), trust_env=False)
        try:
            upstream = await client.send(client.build_request(
                "POST", f"http://127.0.0.1:{int(instance['port'])}/v1/chat/completions", json=body,
                headers={"Authorization": f"Bearer {instance['key']}"},
            ), stream=True)
        except httpx.HTTPError:
            await client.aclose()
            raise HTTPException(503, "Strata is still starting or has stopped. Check its instance status.") from None

        state = {"failed": False}

        async def response():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            except httpx.HTTPError:
                state["failed"] = True
                if not upstream.headers.get("content-type", "").startswith("text/event-stream"):
                    raise
                stopped = read_json(instance_file(directory, selected["profile_id"]))
                message = stopped.get("stop_reason") or "Strata stopped before completing the response. Check its process status in Strata Backend, then retry."
                error = {"error": {"type": "server_error", "code": "strata_engine_stopped", "message": message}}
                yield f"data: {json.dumps(error)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
            finally:
                await upstream.aclose()
                await client.aclose()

        result = StreamingResponse(response(), status_code=upstream.status_code,
                                   media_type=upstream.headers.get("content-type", "application/json"))
        result.strata_state = state
        return result

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=int(provider["port"]), access_log=False))

    def watch_desktop():
        root = directory.parent
        started = monotonic()
        while not server.should_exit:
            try:
                selected, closed = desktop_choice(root)
                windows = directory / "windows"
                if windows.is_dir() or monotonic() - started >= DESKTOP_LEASE_SECONDS:
                    stop_unselected(root, selected)
                    protect_inkling_headroom(root)
                    shared = bool(read_json(root.parent / "profile-access.json").get("profiles"))
                    if (closed or not windows.is_dir()) and not shared and not (selected or {}).get("request"):
                        server.should_exit = True
                        return
            except (OSError, ValueError, psutil.Error) as exc:
                print(f"Could not reconcile Strata process lifetime: {exc}", flush=True)
            sleep(1)

    Thread(target=watch_desktop, daemon=True).start()
    server.run()


if __name__ == "__main__":
    operation, folder, *args = sys.argv[1:]
    root = Path(folder)
    if operation == "bridge":
        serve_bridge(root)
    elif operation == "prepare":
        input_path, output_path = map(Path, args)
        inputs = read_json(input_path)
        prepare_config(root, inputs["profile"], inputs["model"], output_path)
