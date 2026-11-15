import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from dashboard import serving


class ServingTests(unittest.TestCase):
    def test_performance_exposes_counters_without_request_content(self):
        metrics = {"engine": {"model": "iq3", "expert_cache_mib": 7966},
                   "live": {"tok_s_mean": 88.5}, "hardware": {"ram_used": 40 << 30, "gpu_mem_used": 18 << 30},
                   "requests": [{"decode_tok_s": 89.4, "engine_generated": 1180,
                                 "prompt_read": 15984, "prompt_total": 16000, "prompt_ms": 4540.9,
                                 "prompt": "private request"}]}
        with patch.object(serving, "urlopen", return_value=io.BytesIO(json.dumps(metrics).encode())):
            stats = serving.performance_stats({"port": 12345, "key": "test", "model": "iq3"})
        self.assertEqual(stats["generation_tps"], 88.5)
        self.assertEqual(stats["ram_used"], 40 << 30)
        self.assertEqual(stats["recent"], [{"generation_tps": 89.4, "generated": 1180,
                                            "prompt_tps": 3520.0, "prompt_read": 15984, "prompt_total": 16000}])

    def test_external_flash_next_pack_uses_each_models_own_tokenizer_and_ple(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            engine = source / "engine" / ("strata.exe" if os.name == "nt" else "strata")
            engine.parent.mkdir(parents=True)
            engine.touch()
            (root / "prepared-commit").write_text("revision", encoding="ascii")
            first, second = (root / "models" / "model-id" / f"custom-{index}.gguf" for index in (1, 2))
            pack = root / "run-data" / "packs" / "external-model-id"

            def header(path):
                return SimpleNamespace(path=path, metadata={"general.architecture": "qwen4exp"},
                                       tensors=[SimpleNamespace(name="per_layer_token_embd.weight")] if path == second else [])

            with patch.dict("sys.modules", {"gguf_reader": SimpleNamespace(GGUFFile=header)}), \
                    patch.object(serving.sys, "path", serving.sys.path.copy()), patch.object(serving.subprocess, "run") as run:
                config = serving.prepare_external_flash_next(root, {}, [first, second])
            self.assertIn("--compat-bf16", run.call_args.args[0])
            self.assertEqual(config["tokenizer"], str(pack / "tokenizer"))
            self.assertEqual(config["args"][config["args"].index("--ple-gguf") + 1], str(second))

    def test_mtp_progress_tracks_latest_stage(self):
        fetch = "  > python tools/mtp_fetch.py fetch\n  MTP draft layer: 1.23 / 5.21 GB (24%)"
        pack = fetch + "\n  > python tools/mtp_pack.py --experts q2_0\npacking tensors"
        self.assertEqual(serving.preparation_progress(fetch),
                         {"stage": "downloading-mtp", "detail": "1.23 / 5.21 GB (24%)"})
        self.assertEqual(serving.preparation_progress(pack), {"stage": "packing-mtp"})
        self.assertEqual(serving.preparation_progress(pack + "\n  > python tools/mtp_rt.py"),
                         {"stage": "packing-mtp"})
        self.assertEqual(serving.preparation_progress(pack + "\n=== Step 7 ==="), {})

    def request_profile(self, choices, clock, *, ready):
        alias, profile_id, intent = "iq3-profile", "a" * 32, "selected"
        choices = iter(choices)

        def choice(_root, **_):
            confirmed = next(choices)
            return ({"alias": alias, "intent": intent} if confirmed else None), False

        with TemporaryDirectory() as temp:
            directory = Path(temp) / "serving"
            (directory / "windows").mkdir(parents=True)
            serving.write_json(directory / "provider.json", {"port": 12345, "key": "test"})
            serving.write_json(directory / "models.json", {alias: {"profile_id": profile_id}})
            serving.write_json(serving.instance_file(directory, profile_id),
                               {"model": alias, "intent": intent, "port": 12346, "key": "engine"})
            with patch("uvicorn.Server") as server, patch.object(serving, "Thread"), \
                    patch.object(serving, "desktop_choice", side_effect=choice), \
                    patch.object(serving, "monotonic", side_effect=clock), \
                    patch.object(serving, "process_for", return_value=object()), \
                    patch.object(serving, "online", return_value=ready), \
                    patch.object(httpx.AsyncClient, "send", return_value=httpx.Response(200, json={"ok": True})):
                serving.serve_bridge(directory)
                app = server.call_args.args[0].app
                return TestClient(app).post("/v1/chat/completions", json={"model": alias},
                                            headers={"Authorization": "Bearer test"})

    def test_first_prompt_waits_past_old_four_second_deadline(self):
        response = self.request_profile([False, True], [0, 0, 12, 12], ready=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    def test_switching_away_cancels_pending_turn(self):
        response = self.request_profile([True, False], [0, 0, 1], ready=False)
        self.assertEqual(response.status_code, 409)

    def test_missing_selection_fails_after_grace(self):
        response = self.request_profile([False], [0, 0, 66], ready=False)
        self.assertEqual(response.status_code, 409)

    def test_prompt_restarts_stopped_profile_with_a_fresh_intent(self):
        alias, key = "iq3-profile", "a" * 32
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            directory = serving.service_root(root)
            serving.write_json(directory / "models.json", {alias: {"profile_id": key}})
            heartbeat = {"client": "a" * 36, "model": alias, "load": False}
            serving.touch_desktop(root, heartbeat)
            selected, _ = serving.desktop_choice(root, request_alias=alias)
            original = selected["intent"]
            with patch.object(serving, "time", return_value=serving.time()):
                stopped_at = serving.time()
                serving.hold_desktop_choice(root, original, manual_stop=True)
            serving.touch_desktop(root, heartbeat)
            self.assertIsNone(serving.desktop_choice(root)[0])
            self.assertIsNone(serving.desktop_choice(root, request_alias=alias, requested_at=stopped_at)[0])
            self.assertIsNone(serving.desktop_choice(root, request_alias="different", requested_at=stopped_at + 1)[0])
            restarted, _ = serving.desktop_choice(root, request_alias=alias, requested_at=stopped_at + 1)
            self.assertNotEqual(restarted["intent"], original)
            self.assertEqual(serving.desktop_choice(root)[0], restarted)
            self.assertEqual(serving.desktop_choice(root, request_alias=alias, requested_at=stopped_at + 2)[0], restarted)

            serving.hold_desktop_choice(root, restarted["intent"])
            self.assertIsNone(serving.desktop_choice(root, request_alias=alias, requested_at=stopped_at + 3)[0])

    def test_chat_resumes_legacy_hold_and_waits_for_new_instance(self):
        alias, key = "iq3-profile", "a" * 32
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            directory = serving.service_root(root)
            serving.write_json(directory / "provider.json", {"port": 12345, "key": "test"})
            serving.write_json(directory / "models.json", {alias: {"profile_id": key}})
            serving.touch_desktop(root, {"client": "a" * 36, "model": alias})
            original = serving.desktop_choice(root)[0]["intent"]
            serving.write_json(directory / "hold.json", {"intent": original})
            instance_path = serving.instance_file(directory, key)
            instance = {"model": alias, "intent": original, "port": 12346, "key": "engine"}
            serving.write_json(instance_path, instance)

            def process_for(record):
                if record["intent"] == original:
                    intent = serving.desktop_choice(root)[0]["intent"]
                    serving.write_json(instance_path, {**instance, "intent": intent, "port": 12347})
                return object()

            with patch("uvicorn.Server") as server, patch.object(serving, "Thread"), \
                    patch.object(serving, "process_for", side_effect=process_for), \
                    patch.object(serving, "online", return_value=True), \
                    patch.object(httpx.AsyncClient, "send", return_value=httpx.Response(200, json={"ok": True})) as send:
                serving.serve_bridge(directory)
                app = server.call_args.args[0].app
                response = TestClient(app).post("/v1/chat/completions", json={"model": alias},
                                                headers={"Authorization": "Bearer test"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(send.call_args.args[0].url.port, 12347)


if __name__ == "__main__":
    unittest.main()
