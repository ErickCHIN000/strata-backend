import hashlib
import runpy
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from dashboard import model_download, serving


class InklingTests(unittest.TestCase):
    def test_terminal_and_engine_source_layouts(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.assertEqual(serving.terminal_directory(root), root / "source")
            terminal = root / "source/terminal"
            terminal.mkdir(parents=True)
            (terminal / "setup.py").touch()
            engine = root / "source/engine"
            engine.mkdir()
            (engine / "CMakeLists.txt").touch()
            self.assertEqual(serving.terminal_directory(root), terminal)
            self.assertEqual(serving.engine_sources(terminal), engine)
            self.assertEqual(model_download.terminal_directory(root), terminal)

    def test_inkling_dispatch_does_not_try_to_read_a_gguf(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            model = {"family": "inkling-2-mini", "model": "SQ-20", "format": "strata"}
            destination = root / "server.json"
            with (
                patch.dict(sys.modules, {"setup": SimpleNamespace()}),
                patch.object(sys, "path", sys.path.copy()),
                patch.object(serving, "prepare_inkling_config") as prepare,
                patch.object(serving, "catalog_shards", side_effect=AssertionError("Read as GGUF")),
            ):
                serving.prepare_config(root, {"vision_mode": "gpu"}, model, destination)
            self.assertEqual(prepare.call_args.args[:4], (root, {"vision_mode": "gpu"}, model, destination))

    def test_native_setup_preserves_the_selected_pack_context_and_vision(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source/terminal"
            source.mkdir(parents=True)
            (source / "setup.py").touch()
            identity = "catalog:inkling-2-mini:SQ-20"
            folder = root / "models" / hashlib.sha256(identity.encode()).hexdigest()[:20]
            folder.mkdir(parents=True)
            names = ["Inkling-2-Mini-SQ-20.strata", "Inkling-2-Mini-Vision.strata", "tokenizer.json", "chat_template.jinja"]
            for name in names:
                (folder / name).touch()
            family = SimpleNamespace(DEFAULT_CONTEXT=32768, build_file=lambda _: names[0],
                                     model_key=lambda _: "inkling-2-mini-sq-20")
            result = {"exe": "component:strata-inkling", "arch": "inkling2_loop", "drafting": "dspark",
                      "args": ["--native", str(folder / names[0]), "--vision", str(folder / names[1])]}

            def setup():
                self.assertEqual(sys.argv[sys.argv.index("--context") + 1], "65536")
                self.assertEqual(sys.argv[sys.argv.index("--model") + 1], "SQ-20")
                self.assertEqual(sys.argv[sys.argv.index("--vision") + 1], "yes")
                serving.write_json(source / "strata-inkling-2-mini-sq-20.json",
                                   {**result, "args": [*result['args'], '--vram-reserve-mib', '9999'],
                                    "user_args": ['--kv', 'fp16']})
                return 0

            strata = SimpleNamespace(INKLING_FAMILIES={"inkling-2-mini": {"models": {"SQ-20": {}}}},
                                     main=setup, home=lambda: source)
            shape = SimpleNamespace(find=lambda *_args, **_kwargs: SimpleNamespace(exe=folder / "engine", version="0.1.65"))
            args_helper = SimpleNamespace(groups=lambda args: [args[i:i+2] for i in range(0, len(args), 2)],
                                          flat=lambda groups: [arg for group in groups for arg in group])
            with patch.dict(sys.modules, {"strata_control": SimpleNamespace(inkling_family=family, shape_engine=shape, user_args=args_helper)}), patch.object(sys, "argv", []):
                destination = root / "server.json"
                serving.prepare_inkling_config(root, {"controls": {"--max-context": "65536"}, "vision_mode": "gpu"},
                                               {"id": identity, "title": "Inkling SQ-20", "model": "SQ-20", "files": names},
                                               destination, strata)
            self.assertEqual(serving.read_json(destination), {**result, "args_written": result["args"], "user_args": [], "parallel": 1})
            self.assertEqual(serving.read_json(root / "inkling-engine.json")["version"], "0.1.65")

    def test_profile_route_saves_native_controls_and_rejects_invalid_values(self):
        with TemporaryDirectory() as temporary:
            api = runpy.run_path('dashboard/plugin_api.py')
            runtime = api['runtime']()
            save = runtime['save_model_profile']
            with patch.dict(save.__globals__, {'strata_directory': lambda: Path(temporary) / 'strata-runtime',
                                              'sync_serving_profiles': lambda _: None}):
                payload = {'name': 'Inkling 256K Gaming', 'model_id': 'catalog:inkling-2-mini:SQ-20',
                           'controls': {'--max-context': '262144', '--kv': 'fp8', '--draft': 'dspark',
                                        '--vram-reserve-mib': '12288', '--cpu-threads': '8', '--parallel': '1'},
                           'headroom': {'ram_gib': 32, 'vram_mib': 8192}}
                result = api['change_runtime']('save-profile', payload)
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['profile']['headroom'], payload['headroom'])
                self.assertIn('--kv', result['profile']['user_args'])
                for flag, value in [('--max-context', 1048577), ('--kv', 'invalid'), ('--parallel', 65),
                                    ('--cpu-threads', -1), ('--resident-budget-gib', 70), ('--kv', [])]:
                    response = api['change_runtime']('save-profile', {**payload, 'controls': {flag: value}})
                    self.assertFalse(response['ok'], (flag, response))
                bad = api['change_runtime']('save-profile', {**payload, 'headroom': {'ram_gib': -1}})
                self.assertFalse(bad['ok'])
                qwen = api['change_runtime']('save-profile', {**payload, 'model_id': 'local:qwen',
                                                           'controls': {'--resident-budget-gib': 70}})
                self.assertTrue(qwen['ok'], qwen)
                self.assertNotIn('headroom', qwen['profile'])

    def test_memory_floor_stops_only_the_opted_in_inkling_instance(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = serving.service_root(root)
            inkling = {'pid': 101, 'started': 1, 'model': 'inkling', 'headroom': {'ram_gib': 32, 'vram_mib': 8192}}
            serving.write_json(directory / 'instances/inkling.json', inkling)
            serving.write_json(directory / 'instances/qwen.json', {'pid': 102, 'model': 'qwen'})
            with (
                patch.object(serving, 'process_for', return_value=object()),
                patch.object(serving, 'online', return_value=True),
                patch.object(serving.psutil, 'virtual_memory', return_value=SimpleNamespace(available=64 * 2**30)),
                patch.object(serving, 'performance_stats', return_value={'vram_total': 32 * 2**30, 'vram_used': 25 * 2**30}),
                patch.object(serving, 'stop_instance') as stop,
            ):
                serving.protect_inkling_headroom(root)
                stop.assert_called_once_with(root, inkling)
            self.assertIn('GPU headroom', serving.read_json(directory / 'instances/inkling.json')['stop_reason'])
            previous = serving.read_json(directory / 'instances/inkling.json')
            with (
                patch.object(serving, 'process_for', return_value=object()),
                patch.object(serving, 'online', return_value=True),
                patch.object(serving.psutil, 'virtual_memory', return_value=SimpleNamespace(available=16 * 2**30)),
                patch.object(serving, 'performance_stats', return_value=None),
                patch.object(serving, 'stop_instance') as stop,
            ):
                serving.protect_inkling_headroom(root)
                stop.assert_called_once_with(root, previous)
            self.assertIn('System RAM headroom', serving.read_json(directory / 'instances/inkling.json')['stop_reason'])

    def test_hybrid_tuning_uses_a_safe_native_chunk_and_preserves_the_original(self):
        original = ['--max-context', '262144', '--kv', 'fp8']
        self.assertEqual(serving.inkling_engine_args({'user_args': original}), original)
        cap = [*original, '--vram-experts-gb', '8']
        self.assertEqual(serving.inkling_engine_args({'user_args': cap}),
                         [*cap, '--vram-reserve-mib', '0', '--chunk', '16'])
        self.assertEqual(cap, [*original, '--vram-experts-gb', '8'])
        for flag, value in [('--vram-reserve-mib', '12288'), ('--cpu-threads', '8'), ('--kv', 'k8v4')]:
            with self.assertRaisesRegex(ValueError, 'chunk'):
                serving.inkling_engine_args({'user_args': [flag, value, '--chunk', '512']})

    def test_catalog_download_contains_vision_and_tokenizer_files(self):
        names = ["Inkling-2-Mini-SQ-20.strata", "Inkling-2-Mini-Vision.strata", "tokenizer.json", "chat_template.jinja"]
        family = SimpleNamespace(TITLE="Inkling-2-Mini", TOKENIZER_FILES={name: None for name in names[2:]},
                                 files_of=lambda *_: names[:2], size_of=lambda _: 128, sha256_of=lambda _: "a" * 64)
        strata = SimpleNamespace(INKLING_FAMILIES={"inkling-2-mini": {"models": {"SQ-20": {}}}})
        with (
            patch.dict(sys.modules, {"strata_control": SimpleNamespace(inkling_family=family)}),
            patch.object(model_download, "hub_info", return_value=SimpleNamespace(sha="b" * 40)),
        ):
            title, files = model_download.catalog_files(strata, "inkling-2-mini", "SQ-20")
        self.assertIn("+ vision", title)
        self.assertEqual([file["name"] for file in files], names)
        self.assertTrue(all(file["revision"] == "b" * 40 for file in files))
