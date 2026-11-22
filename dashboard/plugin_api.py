"""Stable Hermes routes for the replaceable Strata plugin implementation."""

import runpy
from pathlib import Path
from threading import Lock
from typing import Literal

from fastapi import APIRouter, HTTPException


router = APIRouter()
runtime_path = Path(__file__).resolve().with_name("runtime_api.py")
load_lock = Lock()
loaded_source = None
loaded_runtime = None
loaded_path = None
runtime_state = {
    "update_lock": Lock(),
    "strata_jobs_lock": Lock(),
    "strata_jobs": {},
}


def runtime():
    global loaded_source, loaded_runtime, loaded_path
    with load_lock:
        try:
            access = runpy.run_path(str(runtime_path.with_name("profile_access.py")))
            path = access["runtime_api_path"](runtime_path)
            source = path.read_bytes()
            if source != loaded_source or path != loaded_path:
                loaded_runtime = runpy.run_path(str(path), init_globals=runtime_state)
                loaded_source = source
                loaded_path = path
        except (OSError, ImportError, SyntaxError, ValueError) as exc:
            raise HTTPException(503, "The Strata backend is being updated or could not load.") from exc
        return loaded_runtime


@router.get("/version-status")
def version_status():
    return runtime()["version_status"]()


@router.post("/apply-update")
def apply_update():
    return runtime()["apply_update"]()


@router.get("/strata/status")
def strata_status():
    return runtime()["strata_status"]()


@router.get("/strata/latest")
def strata_latest():
    return runtime()["strata_latest"]()


@router.post("/strata/action/{operation}")
def strata_action(operation: Literal["setup", "update", "wipe"]):
    return runtime()["strata_action"](operation)


def dispatch(method: str, operation: str, *args):
    handler = runtime()[method].get(operation)
    if handler is None:
        raise HTTPException(404, "Unknown Strata operation.")
    return handler(*args)


@router.get("/runtime/{operation}")
def read_runtime(operation: str):
    return dispatch("GET_HANDLERS", operation)


@router.post("/runtime/{operation}")
def change_runtime(operation: str, payload: dict | None = None):
    return dispatch("POST_HANDLERS", operation, payload)
