"""Local file registration through the plugin API using tiny, valid GGUF fixtures."""

import os
import runpy
import shutil
import struct
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard import model_download, serving


def write_gguf(path, architecture, tensor, *, split=None, truncate=False):
    def string(value):
        encoded = value.encode()
        return struct.pack("<Q", len(encoded)) + encoded

    metadata = {"general.architecture": architecture}
    if split is not None:
        metadata.update({"split.no": split, "split.count": 2, "split.tensors.count": 2})
        if split == 0:
            metadata[architecture + ".context_length"] = 262144
    data = b"GGUF" + struct.pack("<IQQ", 3, 1, len(metadata))
    for key, value in metadata.items():
        data += string(key)
        data += struct.pack("<I", 8) + string(value) if isinstance(value, str) else struct.pack("<II", 4, value)
    data += string(tensor) + struct.pack("<I", 1) + struct.pack("<QIQ", 1, 0, 0)
    data += bytes((-len(data)) % 32)
    if not truncate:
        data += struct.pack("<f", 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class LocalModelTests(unittest.TestCase):
    def setUp(self):
        from hermes_constants import get_hermes_home

        installed = get_hermes_home() / "plugin-data/strata-backend/strata-runtime"
        reader = installed / "source/tools/gguf_reader.py"
        python = installed / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not reader.is_file() or not python.is_file():
            self.skipTest("Set up the managed Strata runtime for local GGUF integration tests.")
        self.directory = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.root = self.directory / "data/strata-runtime"
        tools = self.root / "source/tools"
        tools.mkdir(parents=True)
        shutil.copyfile(reader, tools / "gguf_reader.py")
        self.models = self.directory / "Huihui-Flash-Next-v2"
        self.first = self.models / "swift-IQ3_S/Swift-IQ3_S-00001-of-00002.gguf"
        self.second = self.models / "swift-IQ3_S/Swift-IQ3_S-00002-of-00002.gguf"
        self.projector = self.models / "mmproj-Swift-BF16.gguf"
        write_gguf(self.first, "qwen4exp", "token_embd.weight", split=0)
        write_gguf(self.second, "qwen4exp", "per_layer_token_embd.weight", split=1)
        write_gguf(self.projector, "clip", "v.patch_embd.weight")
        adapter = runpy.run_path("dashboard/plugin_api.py")
        runtime = adapter["runtime"]()
        patches = self.enterContext(ExitStack())
        patches.enter_context(patch.dict(runtime["add_local_model"].__globals__, {
            "strata_directory": lambda: self.root,
            "strata_python": lambda _root: python,
            "sync_serving_profiles": lambda _root: None,
            "serving_tools": lambda: {**vars(serving), "instance_rows": lambda _root: []},
        }))
        patches.enter_context(patch("hermes_cli.config.load_config_readonly", return_value={}))
        patches.enter_context(patch("hermes_cli.plugins_auto_update.plugin_update_lock", side_effect=lambda _root: nullcontext()))
        app = FastAPI()
        app.include_router(adapter["router"])
        self.client = self.enterContext(TestClient(app))

    def scan(self, path=None):
        return self.client.post("/runtime/local-model-files", json={"path": str(path or self.models)}).json()

    def add(self):
        scan = self.scan()
        return self.client.post("/runtime/add-local-model", json={
            "path": scan["path"], "group": scan["selected"], "projector": scan["projectors"][0], "name": "Huihui IQ3_S",
        }).json()

    def test_shards_and_projector_register_once_and_are_available_to_profiles(self):
        scan = self.scan()
        self.assertEqual(len(scan["groups"]), 1)
        self.assertEqual(len(scan["groups"][0]["files"]), 2)
        self.assertEqual(self.scan(self.second)["selected"], self.first.name)
        result = self.add()
        self.assertTrue(result["ok"], result)
        model = result["model"]
        self.assertEqual(model["files"], [str(self.first), str(self.second)])
        self.assertEqual(model["projector"], str(self.projector))
        self.assertEqual(model["context_length"], 262144)
        self.assertEqual(self.add()["model"]["id"], model["id"])
        folder = model_download.managed_dir(self.root, model["id"])
        self.assertEqual([path.name for path in folder.iterdir()], ["model.json"])
        self.assertEqual(serving.catalog_shards(self.root, model), [self.first, self.second])
        saved = self.client.post("/runtime/save-profile", json={"name": "Local profile", "model_id": model["id"]}).json()
        self.assertTrue(saved["ok"], saved)
        library = self.client.get("/runtime/library").json()
        self.assertEqual(len(library["models"]), 1)
        self.assertEqual(len(serving.model_roster(library)), 1)

    def test_removing_local_entry_preserves_original_files_and_saved_profile(self):
        model = self.add()["model"]
        original = {file: file.read_bytes() for file in (self.first, self.second, self.projector)}
        profile_path = self.root.parent / "model-profiles" / ("a" * 32 + ".json")
        serving.write_json(profile_path, {"id": "a" * 32, "name": "Keep me", "model_id": model["id"]})
        result = self.client.post("/runtime/delete-model", json={"id": model["id"]}).json()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.client.get("/runtime/library").json()["models"], [])
        self.assertTrue(profile_path.is_file())
        self.assertEqual({file: file.read_bytes() for file in original}, original)

    def test_incomplete_or_truncated_shards_are_rejected(self):
        self.second.unlink()
        self.assertIn("complete", self.scan()["error"])
        write_gguf(self.second, "qwen4exp", "per_layer_token_embd.weight", split=1, truncate=True)
        result = self.add()
        self.assertFalse(result["ok"], result)
        self.assertIn("incomplete", result["error"])
        self.assertEqual(self.client.get("/runtime/library").json()["models"], [])

    def test_missing_local_files_stay_removable_and_leave_the_provider_roster(self):
        model = self.add()["model"]
        self.second.unlink()
        library = self.client.get("/runtime/library").json()
        self.assertFalse(library["models"][0]["available"])
        library["profiles"] = [{"id": "a" * 32, "name": "Unavailable", "model_id": model["id"]}]
        self.assertEqual(serving.model_roster(library), {})
        self.assertTrue(self.client.post("/runtime/delete-model", json={"id": model["id"]}).json()["ok"])

    def test_image_input_setting_and_existing_vision_overrides_are_preserved(self):
        model = self.add()["model"]
        saved = self.client.post("/runtime/save-profile", json={
            "name": "With images", "model_id": model["id"], "vision_mode": "gpu",
        }).json()
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(saved["profile"]["vision_mode"], "gpu")
        legacy = self.client.post("/runtime/save-profile", json={
            "name": "Existing image setup", "model_id": model["id"],
            "config": {"vision": {"gpu": True, "mmproj": str(self.projector)}},
        }).json()
        self.assertTrue(legacy["ok"], legacy)
        self.assertEqual(legacy["profile"]["vision_mode"], "gpu")
