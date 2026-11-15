"""Exercise the library endpoints with Hermes on PYTHONPATH and disposable files."""

import os
import runpy
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard import model_download, serving


class LibraryDeletionTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(TemporaryDirectory())
        self.root = Path(temp).resolve() / "strata-runtime"
        self.selection = {"source": "catalog", "family": "qwen", "model": "IQ3_S"}
        self.base_id = model_download.model_id(self.selection)
        self.external_id = "hf:example/custom"
        self.base = self.model(self.base_id, "Base model")
        self.external = self.model(self.external_id, "Custom model")
        self.base_file = self.base / "shards" / "weights.gguf"
        self.base_file.parent.mkdir()
        self.base_file.write_bytes(b"shared shard")
        self.external_file = self.external / "shards" / "weights.gguf"
        self.external_file.parent.mkdir()
        os.link(self.base_file, self.external_file)
        self.key = "a" * 32
        self.profile = {"id": self.key, "name": "Base", "model_id": self.base_id}
        self.profile_path = self.root.parent / "model-profiles" / f"{self.key}.json"
        serving.write_json(self.profile_path, self.profile)
        adapter = runpy.run_path("dashboard/plugin_api.py")
        runtime = adapter["runtime"]()
        patches = self.enterContext(ExitStack())
        self.default = {}
        self.rows = []
        self.synced_rosters = []
        patches.enter_context(patch.dict(runtime["delete_library_entry"].__globals__, {
            "strata_directory": lambda: self.root,
            "sync_serving_profiles": lambda _root: self.synced_rosters.append(serving.model_roster(runtime["model_library"]())),
            "serving_tools": lambda: {**vars(serving), "instance_rows": lambda _root: self.rows},
        }))
        patches.enter_context(patch("hermes_cli.config.load_config_readonly", return_value=self.default))
        patches.enter_context(patch("hermes_cli.plugins_auto_update.plugin_update_lock", side_effect=lambda _root: nullcontext()))
        app = FastAPI()
        app.include_router(adapter["router"])
        self.client = self.enterContext(TestClient(app))

    def model(self, model_id, title):
        folder = model_download.managed_dir(self.root, model_id)
        record = {"id": model_id, "title": title, "files": ["shards/weights.gguf"]}
        if model_id == self.base_id:
            record.update(self.selection)
        serving.write_json(folder / "model.json", record)
        return folder

    def delete(self, kind, item_id):
        return self.client.post(f"/runtime/delete-{kind}", json={"id": item_id}).raise_for_status().json()

    def test_delete_model_keeps_profiles_and_the_open_shared_shard(self):
        serving.write_json(self.root / "download-state.json", {"selection": self.selection, "phase": "complete"})
        with self.external_file.open("rb") as active:
            self.assertEqual(self.delete("model", self.base_id), {"ok": True})
            self.assertEqual(active.read(), b"shared shard")
        self.assertFalse(self.base.exists())
        self.assertTrue(self.external_file.is_file())
        self.assertTrue(self.profile_path.is_file())
        self.assertFalse((self.root / "download-state.json").exists())
        self.assertEqual(self.synced_rosters, [{}])

    def test_delete_profile_keeps_both_downloads(self):
        self.assertEqual(self.delete("profile", self.key), {"ok": True})
        self.assertFalse(self.profile_path.exists())
        self.assertTrue(self.base_file.is_file())
        self.assertTrue(self.external_file.is_file())
        self.assertEqual(self.synced_rosters, [{}])

    def test_running_and_default_profiles_are_protected(self):
        self.rows[:] = [{"profile_id": self.key, "state": "preparing"}]
        self.assertIn("Stop the Strata profile", self.delete("model", self.base_id)["error"])
        self.assertIn("Stop the Strata profile", self.delete("profile", self.key)["error"])
        self.rows.clear()
        alias = next(iter(serving.model_roster({"models": [{"id": self.base_id, "title": "Base"}],
                                                "profiles": [self.profile]})))
        self.default["model"] = {"default": alias}
        self.assertIn("Hermes default", self.delete("profile", self.key)["error"])
        self.assertTrue(self.profile_path.is_file())
        self.assertTrue(self.base_file.is_file())

    def test_reassigned_profile_still_protects_the_model_used_by_its_process(self):
        serving.write_json(self.profile_path, {**self.profile, "model_id": self.external_id})
        self.rows[:] = [{"profile_id": self.key, "state": "ready"}]
        serving.write_json(self.root / "serving" / "configs" / f"{self.key}.prepare.json",
                           {"model": {"id": self.base_id}})
        self.assertIn("Stop the Strata profile", self.delete("model", self.base_id)["error"])
        self.assertTrue(self.base_file.is_file())
