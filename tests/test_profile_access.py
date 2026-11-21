import os
import runpy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

from dashboard import profile_access, serving


class ProfileAccessTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.enterContext(patch.dict(os.environ, {"HERMES_HOME": str(self.home)}))
        token = set_hermes_home_override(self.home)
        self.addCleanup(reset_hermes_home_override, token)
        (self.home / "config.yaml").write_text("model:\n  default: cloud-model\nagent:\n  max_turns: 17\n", encoding="utf-8")
        self.bot = self.home / "profiles" / "test-bot"
        self.bot.mkdir(parents=True)
        (self.bot / "config.yaml").write_text("model:\n  default: cloud-model\nagent:\n  max_turns: 23\n", encoding="utf-8")
        (self.bot / "profile.yaml").write_text("display_name: Test Bot\n", encoding="utf-8")
        self.root = profile_access.data_directory(self.home) / "strata-runtime"
        self.root.mkdir(parents=True)
        self.provider = {"port": 12345, "key": "test-local-provider"}
        serving.write_json(self.root / "serving" / "provider.json", self.provider)
        self.roster = {"iq3-saved": {"profile_id": "a" * 32, "context_length": 262144}}
        self.enable = self.enterContext(patch("hermes_cli.plugins_cmd.dashboard_set_agent_plugin_enabled", return_value={"ok": True}))

    def change(self, enabled):
        return profile_access.change_access(self.root, {"profile": "test-bot", "enabled": enabled}, vars(serving), self.roster)

    def test_bot_uses_owner_runtime_and_shared_provider_without_copying_files(self):
        self.assertTrue(self.change(True)["ok"])
        self.enable.assert_called_once_with("strata-backend", enabled=True)
        self.assertEqual([p.name for p in profile_access.provider_profiles(self.root)], ["default", "test-bot"])
        self.assertFalse((self.bot / "plugin-data/strata-backend/strata-runtime").exists())
        with profile_access.profile_scope("test-bot"):
            from hermes_cli.config import require_readable_config_before_write

            config = require_readable_config_before_write()
            self.assertEqual(config["model"]["default"], "cloud-model")
            self.assertEqual(config["agent"]["max_turns"], 23)
            self.assertEqual(config["providers"]["strata-backend"]["models"], {"iq3-saved": {"context_length": 262144}})
            self.assertEqual(profile_access.runtime_directory(profile_access.data_directory(self.bot)), self.root)
            self.assertFalse(profile_access.access_status(self.root)["can_manage"])
        self.assertTrue(self.change(False)["ok"])
        with profile_access.profile_scope("test-bot"):
            self.assertEqual(profile_access.runtime_directory(profile_access.data_directory(self.bot)),
                             profile_access.data_directory(self.bot) / "strata-runtime")
            self.assertNotIn("strata-backend", require_readable_config_before_write().get("providers", {}))
        self.assertTrue(self.root.is_dir())

    def test_independent_installation_and_provider_name_conflicts_are_preserved(self):
        own = profile_access.data_directory(self.bot) / "strata-runtime"
        own.mkdir(parents=True)
        (own / "prepared-commit").write_text("separate", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "own Strata data"):
            self.change(True)
        self.assertEqual((own / "prepared-commit").read_text(), "separate")
        (own / "prepared-commit").unlink()
        (self.bot / "config.yaml").write_text("providers:\n  strata-backend:\n    api_key: different-provider\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "different Hermes provider"):
            self.change(True)
        self.assertFalse((profile_access.data_directory(self.bot) / "shared-runtime.json").exists())

    def test_unapproved_link_cannot_attach_to_owner_data(self):
        data = profile_access.data_directory(self.bot)
        serving.write_json(data / "shared-runtime.json", {"owner": "default"})
        with profile_access.profile_scope("test-bot"), self.assertRaisesRegex(ValueError, "no longer has access"):
            profile_access.runtime_directory(data)

    def test_shared_presets_refresh_every_exposed_profile(self):
        self.change(True)
        serving.sync_provider(self.root, {"updated-profile": {"profile_id": "b" * 32, "context_length": 131072}})
        from hermes_cli.config import require_readable_config_before_write

        for name in ("default", "test-bot"):
            with profile_access.profile_scope(name):
                config = require_readable_config_before_write()
                provider = config["providers"]["strata-backend"]
                self.assertEqual(provider["api"], "http://127.0.0.1:12345/v1")
                self.assertEqual(provider["models"], {"updated-profile": {"context_length": 131072}})
                self.assertEqual(config["model"]["default"], "cloud-model")

    def test_bot_default_must_be_changed_before_revoking_access(self):
        self.change(True)
        with profile_access.profile_scope("test-bot"):
            from hermes_cli.config import require_readable_config_before_write, save_config

            config = require_readable_config_before_write()
            config["model"]["default"] = "iq3-saved"
            save_config(config)
        self.assertEqual(profile_access.defaults_using(self.root, {"iq3-saved"}), ["test-bot"])
        with self.assertRaisesRegex(ValueError, "different default model"):
            self.change(False)
        self.assertEqual([p.name for p in profile_access.provider_profiles(self.root)], ["default", "test-bot"])

    def test_failed_native_activation_rolls_back_shared_access(self):
        self.enable.return_value = {"ok": False, "error": "activation failed"}
        with self.assertRaisesRegex(ValueError, "activation failed"):
            self.change(True)
        self.assertFalse((profile_access.data_directory(self.bot) / "shared-runtime.json").exists())
        self.assertEqual(profile_access.read_state(self.root.parent / "profile-access.json").get("profiles", []), [])

    def test_bot_api_reads_the_owner_library_and_cannot_change_access(self):
        self.change(True)
        model = self.root / "models" / "shared-model"
        model.mkdir(parents=True)
        (model / "weights.gguf").write_bytes(b"fixture")
        serving.write_json(model / "model.json", {"id": "shared", "title": "Shared weights", "files": ["weights.gguf"]})
        serving.write_json(self.root.parent / "model-profiles" / ("a" * 32 + ".json"),
                           {"id": "a" * 32, "name": "Shared preset", "model_id": "shared"})
        adapter = runpy.run_path("dashboard/plugin_api.py")
        app = FastAPI()
        app.include_router(adapter["router"])
        with profile_access.profile_scope("test-bot"), TestClient(app) as client:
            library = client.get("/runtime/library").raise_for_status().json()
            self.assertEqual(library["models"][0]["title"], "Shared weights")
            self.assertEqual(library["profiles"][0]["name"], "Shared preset")
            status = client.get("/runtime/profile-access").raise_for_status().json()
            self.assertEqual(status["owner"], "default")
            self.assertEqual(status["current"], "test-bot")
            result = client.post("/runtime/profile-access", json={"profile": "default", "enabled": False}).json()
            self.assertIn("owner profile", result["error"])
