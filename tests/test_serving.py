import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from dashboard import serving


class ServingTests(unittest.TestCase):
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
