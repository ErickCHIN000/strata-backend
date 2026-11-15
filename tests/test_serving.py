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

        def choice(_root, *, include_dormant=False):
            confirmed = next(choices) if not include_dormant else False
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


if __name__ == "__main__":
    unittest.main()
