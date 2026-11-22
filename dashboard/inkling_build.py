"""Build the Inkling-only CUDA headroom fix without changing signed components."""

import hashlib
import json
import os
import subprocess
from pathlib import Path


SOURCE_COMMIT = "c6f6427d9de9016fd92d13e29740f65625261df7"
PATCH = Path(__file__).resolve().parents[1] / "native" / "inkling-cuda-headroom.patch"


def run(command, **kwargs):
    return subprocess.run(command, check=True, stdin=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs)


def prepare_engine(root, strata):
    if (root / "prepared-commit").read_text(encoding="ascii").strip() != SOURCE_COMMIT:
        raise ValueError("This Inkling CUDA fix targets Strata v0.2.19. Update the plugin for the installed backend revision.")
    cards = strata.gpus()
    if not cards:
        raise ValueError("The Inkling CUDA offload fix needs an NVIDIA GPU.")
    architectures = sorted({str(card['arch']) for card in cards})
    patch = PATCH.read_bytes().replace(b"\r\n", b"\n")
    identity = hashlib.sha256(patch + SOURCE_COMMIT.encode() + repr(architectures).encode()).hexdigest()
    directory = root / "inkling-cuda"
    if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError("The Inkling build folder is outside the managed runtime.")
    marker = directory / "build.json"
    executable = directory / "build" / ("strata-inkling.exe" if os.name == "nt" else "strata-inkling")
    if executable.is_file() and marker.is_file() and json.loads(marker.read_text()).get("identity") == identity:
        return executable
    nvcc, cuda_version = strata.find_nvcc()
    if not nvcc:
        raise ValueError("The Inkling CUDA offload fix needs an existing NVIDIA CUDA C++ toolkit.")
    vcvars = strata.find_vcvars(cuda_version) if os.name == "nt" else None
    if os.name == "nt" and not vcvars:
        raise ValueError("The Inkling CUDA offload fix needs the existing Visual Studio C++ build tools.")
    source = directory / "source"
    if source.is_symlink() or not source.resolve().is_relative_to(directory.resolve()):
        raise ValueError("The private Inkling source moved outside its build folder.")
    directory.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        print("Preparing an isolated Inkling source checkout.", flush=True)
        run(["git", "clone", "--no-hardlinks", "--no-checkout", str(root / "source"), str(source)])
        run(["git", "-C", str(source), "sparse-checkout", "set", "engine"])
        run(["git", "-C", str(source), "checkout", "--detach", SOURCE_COMMIT])
    head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if head != SOURCE_COMMIT:
        raise ValueError("The private Inkling build checkout changed revision; inspect it before rebuilding.")
    # A prior version of our own patch is reversed only after its exact reverse passes Git's check.
    previous = directory / "applied.patch"
    if previous.is_file():
        run(["git", "-C", str(source), "apply", "--reverse", "--check", str(previous)])
        run(["git", "-C", str(source), "apply", "--reverse", str(previous)])
    run(["git", "-C", str(source), "apply", "--check", str(PATCH)])
    run(["git", "-C", str(source), "apply", str(PATCH)])
    previous.write_bytes(patch)
    print("Building the Inkling CUDA offload fix (first preparation only).", flush=True)
    definitions = ["-DSTRATA_ENABLE_CUDA=ON", f"-DCMAKE_CUDA_COMPILER={nvcc}",
                   f"-DSTRATA_INK_CUDA_ARCHS={';'.join(architectures)}",
                   f"-DCMAKE_CUDA_ARCHITECTURES={';'.join(architectures)}",
                   *strata.toolkit_root_defs(nvcc), *strata.host_compiler_defs(True)]
    strata.cmake_build(source / "engine/src/inkling2", directory / "build", "strata-inkling",
                       definitions, vcvars, str(directory / "build.bat"))
    if not executable.is_file():
        raise ValueError("The Inkling CUDA build did not produce its executable.")
    run([str(executable), "--version"])
    marker.write_text(json.dumps({"identity": identity, "source": SOURCE_COMMIT,
                                  "patch_sha256": hashlib.sha256(patch).hexdigest(), "exe": str(executable)}), encoding="utf-8")
    return executable
