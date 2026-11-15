"""Read Hugging Face metadata and download selected weight files without launching a model."""

import hashlib
import json
import ntpath
import os
import re
import shutil
import sys
from pathlib import Path, PureWindowsPath
from time import monotonic, sleep
from urllib.parse import unquote, urlsplit


HUB = "https://huggingface.co"
REPO_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
SHA = re.compile(r"[0-9a-f]{40}")
SHARD = re.compile(r"(.+)-(\d{5})-of-(\d{5})\.gguf", re.IGNORECASE)
CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
CHUNK = 1024 * 1024


class DownloadPaused(Exception):
    pass


def write_transfer(root, state):
    staged = root / ".download-state.tmp"
    staged.write_text(json.dumps(state), encoding="utf-8")
    for attempt in range(4):
        try:
            os.replace(staged, root / "download-state.json")
            return
        except PermissionError:
            if attempt == 3:
                raise
            sleep(0.025)


def validate_repo(repo):
    if not isinstance(repo, str) or not REPO_ID.fullmatch(repo) or ".." in repo:
        raise ValueError("Enter a Hugging Face repository as owner/model.")
    return repo


def validate_file(name):
    if not isinstance(name, str) or not name or len(name) > 750:
        raise ValueError("Invalid model filename.")
    for part in name.split("/"):
        reserved = ntpath.isreserved(part) if hasattr(ntpath, "isreserved") else PureWindowsPath(part).is_reserved()
        if not part or part.endswith((".", " ")) or re.search(r'[\x00-\x1f\\<>:"|?*]', part) or reserved:
            raise ValueError("Invalid model filename.")
    return name


def model_selection(payload):
    payload = payload or {}
    if payload.get("source") == "catalog":
        family, model = payload.get("family"), payload.get("model")
        if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", v) for v in (family, model)):
            raise ValueError("Choose a model from the Strata catalog.")
        return {"source": "catalog", "family": family, "model": model}
    if payload.get("source") == "hf":
        repo = validate_repo(payload.get("repo"))
        revision = payload.get("revision")
        if not isinstance(revision, str) or not SHA.fullmatch(revision):
            raise ValueError("Load the Hugging Face file list before downloading.")
        return {"source": "hf", "repo": repo, "revision": revision, "group": validate_file(payload.get("group"))}
    raise ValueError("Choose a catalog model or a Hugging Face file.")


def model_id(selection):
    if selection["source"] == "catalog":
        return f"catalog:{selection['family']}:{selection['model']}"
    return f"hf:{selection['repo']}@{selection['revision']}:{selection['group']}"


def hub_info(repo, revision="main"):
    from huggingface_hub import HfApi

    try:
        return HfApi(endpoint=HUB).model_info(repo, revision=revision, files_metadata=True, timeout=20)
    except Exception as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status in (401, 403):
            raise ValueError("Hugging Face denied access. Log in with a token that can read this model and accept its terms if gated.") from None
        if status == 404:
            raise ValueError("Hugging Face could not find this repository or revision.") from None
        raise ValueError("Could not read the Hugging Face repository. Try again shortly.") from None


def gguf_groups(siblings):
    singles = []
    split = {}
    for item in siblings or []:
        name = item.rfilename
        if not name.lower().endswith(".gguf"):
            continue
        try:
            validate_file(name)
        except ValueError:
            continue
        file = {"name": name, "bytes": item.size, "sha256": getattr(item.lfs, "sha256", None)}
        match = SHARD.fullmatch(name)
        if match:
            prefix, index, total = match.groups()
            split.setdefault((prefix, int(total)), {})[int(index)] = file
        else:
            singles.append({"id": name, "label": name, "files": [file]})
    for (prefix, total), files in split.items():
        if total and set(files) == set(range(1, total + 1)):
            ordered = [files[index] for index in range(1, total + 1)]
            singles.append({"id": ordered[0]["name"], "label": f"{prefix} ({total} shards)", "files": ordered})
    return sorted(singles, key=lambda group: group["id"].casefold())


def select_group(groups, filename):
    if not filename:
        return None
    exact = [g for g in groups if any(filename in (f["name"], Path(f["name"]).name) for f in g["files"])]
    if not exact:
        pattern = re.compile(rf"(?:^|[-_/]){re.escape(filename)}(?:[-_./]|$)", re.IGNORECASE)
        exact = [g for g in groups if pattern.search(g["id"])]
    return exact[0]["id"] if len(exact) == 1 else None


def hf_catalog(repo, filename):
    from huggingface_hub import get_token, try_to_load_from_cache

    info = hub_info(validate_repo(repo))
    groups = gguf_groups(info.siblings)
    for group in groups:
        files = group["files"]
        group["bytes"] = sum(f["bytes"] for f in files) if all(f["bytes"] is not None for f in files) else None
        group["cached"] = all(isinstance(try_to_load_from_cache(repo, f["name"], revision=info.sha), str) for f in files)
    return {"repo": repo, "revision": info.sha, "groups": groups,
            "selected": select_group(groups, filename), "authenticated": bool(get_token())}


def hub_file(url, pin=None):
    parsed = urlsplit(url)
    owner, repo, resolve, revision, name = unquote(parsed.path).lstrip("/").split("/", 4)
    if parsed.scheme != "https" or parsed.netloc != "huggingface.co" or resolve != "resolve":
        raise ValueError("Strata did not provide a Hugging Face file source for this model.")
    size, sha = pin or (None, None)
    return {"repo": validate_repo(f"{owner}/{repo}"), "revision": revision,
            "name": validate_file(name), "bytes": size, "sha256": sha}


def catalog_files(strata, family, model):
    if family in strata.MULTI_FILE_FAMILIES:
        fam = strata.MULTI_FILE_FAMILIES[family]
        spec = fam["models"].get(model)
        if spec is None:
            raise ValueError("This model is unavailable in Strata’s catalog.")
        base = spec.get("hf") or fam["hf"]
        files = [hub_file(base + name, fam["sha256"].get(name)) for name in spec["shards"]]
        files.extend(hub_file(strata.DSV41_SHARED[name] + name, fam["sha256"].get(name)) for name in spec.get("shared", ()))
    elif family in strata.OWN_PATH_FAMILIES:
        fam = strata.OWN_PATH_FAMILIES[family]
        if model not in fam["models"]:
            raise ValueError("This model is unavailable in Strata’s catalog.")
        name = fam["file"].format(q=model)
        files = [hub_file(fam["hf"] + name, fam["sha256"].get(name))]
    else:
        fam = strata.FAMILIES.get(family)
        spec = strata.MODELS.get(model)
        if not fam or spec is None or family not in spec.get("families", ("qwen", "swift")):
            raise ValueError("This model is unavailable in Strata’s catalog.")
        if fam.get("ready"):
            files = [{"repo": validate_repo(repo), "revision": revision, "name": validate_file(name),
                      "bytes": size, "sha256": sha} for _, repo, revision, name, size, sha in strata.ready_files(model)]
        else:
            base = strata.model_hf(fam, model)
            names = [strata.model_file(fam, model, i) for i in range(1, strata.model_shards(fam, model) + 1)]
            files = [hub_file(base + name, strata.file_pin(base + name)) for name in names]
    if not files:
        raise ValueError("Strata has no downloadable files for this catalog entry.")
    return f"{fam['title']} {model}", files


def managed_dir(root, model):
    dirname = hashlib.sha256(model.encode("utf-8")).hexdigest()[:20]
    path = root / "models" / dirname
    if path.resolve() != path:
        raise ValueError("The managed model folder moved outside the Strata installation.")
    return path


def verify_file(path, file, check_pause):
    size, sha = file.get("bytes"), file.get("sha256")
    if size is not None and path.stat().st_size != size:
        return False
    if not sha:
        return True
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * CHUNK):
            check_pause()
            digest.update(chunk)
    return digest.hexdigest() == sha


def receive_file(file, part, report, check_pause):
    import httpx
    from huggingface_hub import hf_hub_url
    from huggingface_hub.utils import build_hf_headers

    name = file["name"]
    size = file.get("bytes")
    offset = part.stat().st_size if part.is_file() else 0
    report("downloading", offset, force=True)
    check_pause()
    if offset and offset == size:
        return
    url = hf_hub_url(file["repo"], name, revision=file["revision"], endpoint=HUB)
    headers = {**build_hf_headers(), "Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    try:
        with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=35) as response:
            if response.status_code in (401, 403):
                raise ValueError(f"Hugging Face denied access to {name}. Check your login and model access.")
            if response.status_code == 206:
                bounds = CONTENT_RANGE.fullmatch(response.headers.get("content-range", ""))
                if not bounds or int(bounds[1]) != offset or (size is not None and int(bounds[3]) != size):
                    raise ValueError(f"The Hub returned an unexpected byte range for {name}. The partial download was kept.")
                size = int(bounds[3])
            elif response.status_code != 200 or offset:
                raise ValueError(f"The Hub did not accept this download request for {name}. The partial download was kept.")
            elif size is None and response.headers.get("content-length", "").isdigit():
                size = int(response.headers["content-length"])
            if size is not None and shutil.disk_usage(part.parent).free < size - offset:
                raise ValueError(f"Not enough free disk space for the rest of {name}.")
            with part.open("ab" if offset else "wb") as handle:
                for chunk in response.iter_raw(CHUNK):
                    check_pause()
                    if size is not None and offset + len(chunk) > size:
                        raise ValueError(f"The download exceeded the expected file size: {name}.")
                    handle.write(chunk)
                    offset += len(chunk)
                    report("downloading", offset)
            if size is not None and offset != size:
                raise ValueError(f"The download ended early for {name}. Resume to continue.")
    except DownloadPaused:
        report("downloading", offset, force=True)
        raise
    except httpx.HTTPError:
        raise ValueError(f"The transfer stopped for {name}. Resume to continue from the saved bytes.") from None


def download_file(file, folder, report, check_pause):
    from huggingface_hub import try_to_load_from_cache

    name, repo, revision = file["name"], file["repo"], file["revision"]
    dest = folder / name
    if dest.resolve() != dest:
        raise ValueError("The model path must stay inside its managed folder.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file():
        report("verifying", dest.stat().st_size, force=True)
        if verify_file(dest, file, check_pause):
            return dest
    cached = try_to_load_from_cache(repo, name, revision=revision)
    if isinstance(cached, str) and Path(cached).is_file():
        source = Path(cached).resolve()
        report("verifying", source.stat().st_size, force=True)
        if verify_file(source, file, check_pause):
            staging = dest.with_name(dest.name + ".reuse")
            staging.unlink(missing_ok=True)
            try:
                os.link(source, staging)
            except OSError:
                shutil.copyfile(source, staging)
            os.replace(staging, dest)
            print(f"Reused cached file: {name}", flush=True)
            return dest
    pin = hashlib.sha256(f"{repo}/{revision}/{name}/{file.get('sha256')}".encode()).hexdigest()[:12]
    part = dest.with_name(f"{dest.name}.{pin}.part")
    if part.resolve() != part:
        raise ValueError("The partial file must stay inside its managed folder.")
    receive_file(file, part, report, check_pause)
    report("verifying", part.stat().st_size, force=True)
    if not verify_file(part, file, check_pause):
        part.unlink()
        raise ValueError(f"The file failed its size or checksum verification: {name}.")
    os.replace(part, dest)
    return dest


def download_model(root, selection):
    selection = model_selection(selection)
    identity = model_id(selection)
    folder = managed_dir(root, identity)
    if selection["source"] == "catalog":
        sys.path.insert(0, str(root / "source"))
        import setup as strata

        if hasattr(strata, "feed_models_setup"):
            strata.feed_models_setup(fetch=False)
        title, files = catalog_files(strata, selection["family"], selection["model"])
    else:
        info = hub_info(selection["repo"], selection["revision"])
        group = next((g for g in gguf_groups(info.siblings) if g["id"] == selection["group"]), None)
        if group is None:
            raise ValueError("That GGUF file or complete shard group is unavailable at the selected revision.")
        title = group["label"]
        files = [{**f, "repo": selection["repo"], "revision": selection["revision"]} for f in group["files"]]
    print(f"Downloading {title} ({len(files)} files)", flush=True)
    state = {"selection": selection, "title": title, "phase": "downloading", "bytes": 0,
             "total_bytes": sum(f["bytes"] for f in files) if all(f["bytes"] is not None for f in files) else None,
             "files": len(files), "completed_files": 0}
    write_transfer(root, state)
    last_report = 0.0
    complete_bytes = 0

    def check_pause():
        if (root / "download.pause").exists():
            raise DownloadPaused

    def report(phase, bytes_written, *, force=False):
        nonlocal last_report
        if force or monotonic() - last_report >= 1.0:
            state.update(phase=phase, bytes=complete_bytes + bytes_written)
            write_transfer(root, state)
            last_report = monotonic()

    paths = []
    try:
        for file in files:
            check_pause()
            state["file"] = file["name"]
            subfolder = folder / file["repo"].replace("/", "--") if selection["source"] == "catalog" else folder
            path = download_file(file, subfolder, report, check_pause)
            paths.append(path.relative_to(folder).as_posix())
            complete_bytes += path.stat().st_size
            state["completed_files"] += 1
            report("downloading", 0, force=True)
    except DownloadPaused:
        state["phase"] = "paused"
        write_transfer(root, state)
        print(f"Paused {title}. Completed and partial files were kept.", flush=True)
        return
    record = {**selection, "id": identity, "title": title, "files": paths,
              "bytes": sum((folder / path).stat().st_size for path in paths),
              "artifacts": [{"path": path, "repo": file["repo"], "revision": file["revision"], "sha256": file["sha256"]}
                            for path, file in zip(paths, files, strict=True)]}
    marker = folder / "model.json.tmp"
    marker.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(marker, folder / "model.json")
    state.update(phase="complete", bytes=complete_bytes, total_bytes=complete_bytes)
    write_transfer(root, state)
    print(f"Downloaded {title}", flush=True)


if __name__ == "__main__":
    action, root = sys.argv[1:]
    data = json.load(sys.stdin)
    try:
        if action == "files":
            print(json.dumps(hf_catalog(data["repo"], data.get("filename"))))
        elif action == "download":
            download_model(Path(root).resolve(), data)
        else:
            raise ValueError("Unknown model operation.")
    except ValueError as exc:
        sys.exit(str(exc))
