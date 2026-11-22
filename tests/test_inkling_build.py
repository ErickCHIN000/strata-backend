import os
import io
import json
import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from dashboard import inkling_build, serving


class InklingBuildTests(unittest.TestCase):
    def test_live_prompt_progress_reads_the_native_slot_counter(self):
        metrics = {"engine": {"model": "inkling"}, "hardware": {}, "requests": [],
                   "live": {"prompt_total": None, "slots": [{"state": "reading", "prompt_read": 4096, "prompt_tokens": 16000}]}}
        with patch.object(serving, "urlopen", return_value=io.BytesIO(json.dumps(metrics).encode())):
            stats = serving.performance_stats({"port": 12345, "key": "test", "model": "inkling"})
        self.assertEqual((stats["prompt_read"], stats["prompt_total"]), (4096, 16000))

    def test_large_native_preset_uses_private_engine_without_replacing_component(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source/terminal"
            source.mkdir(parents=True)
            (source / "setup.py").touch()
            identity = "catalog:inkling-2-mini:SQ-20"
            directory = root / "models" / hashlib.sha256(identity.encode()).hexdigest()[:20]
            directory.mkdir(parents=True)
            weight = directory / "model.strata"
            weight.touch()
            signed = root / "components/shapes/strata-inkling.exe"
            private = root / "inkling-cuda/build/strata-inkling.exe"
            cfg = {"exe": "component:strata-inkling", "args": ["--native", str(weight)]}
            def setup():
                serving.write_json(source / "strata-test.json", cfg)
                return 0
            strata = SimpleNamespace(INKLING_FAMILIES={"inkling-2-mini": {"models": {"SQ-20": {}}}},
                                     main=setup, home=lambda: source, gpus=lambda: [{"arch": "120"}])
            family = SimpleNamespace(build_file=lambda _: weight.name, model_key=lambda _: "test", DEFAULT_CONTEXT=32768)
            shape = SimpleNamespace(find=lambda *_args, **_kw: SimpleNamespace(exe=signed, version="0.1.65"))
            args = SimpleNamespace(groups=lambda a: [a], flat=lambda g: [a for group in g for a in group])
            with (
                patch.dict("sys.modules", {"strata_control": SimpleNamespace(inkling_family=family, shape_engine=shape, user_args=args)}),
                patch.object(serving.runpy, "run_path", return_value={"prepare_engine": lambda *_: private}),
                patch.object(serving.sys, "argv", []),
            ):
                destination = root / "config.json"
                serving.prepare_inkling_config(root, {"controls": {"--max-context": "524288"}, "headroom": {"vram_mib": 8192}},
                                               {"id": identity, "title": "Inkling", "model": "SQ-20", "files": [weight.name]}, destination, strata)
            result = serving.read_json(destination)
            self.assertEqual(result["exe"], str(private))
            self.assertEqual(result["env"]["HERMES_INKLING_VRAM_FLOOR_MIB"], "9216" if os.name == "nt" else "8192")
            self.assertEqual(serving.read_json(root / "inkling-engine.json")["exe"], str(signed))

    def test_private_build_preserves_signed_engine_and_reuses_cached_binary(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "prepared-commit").write_text(inkling_build.SOURCE_COMMIT)
            signed = root / "components/engine/strata.exe"
            signed.parent.mkdir(parents=True)
            signed.write_bytes(b"Flash Next signed engine")
            source = root / "inkling-cuda/source"
            source.mkdir(parents=True)

            def compile_engine(src, directory, target, definitions, vcvars, bat):
                self.assertTrue(src.is_relative_to(source))
                self.assertEqual(target, "strata-inkling")
                self.assertTrue(Path(bat).is_relative_to(root / "inkling-cuda"))
                directory.mkdir()
                (directory / ("strata-inkling.exe" if os.name == "nt" else "strata-inkling")).write_bytes(b"Inkling fix")

            strata = SimpleNamespace(gpus=lambda: [{"arch": "120"}], find_nvcc=Mock(return_value=("nvcc", (13, 3))),
                                     find_vcvars=lambda _: "vcvars.bat", toolkit_root_defs=lambda _: [],
                                     host_compiler_defs=lambda _: [], cmake_build=compile_engine)
            with patch.object(inkling_build, "run") as run, patch.object(inkling_build.subprocess, "check_output", return_value=inkling_build.SOURCE_COMMIT):
                executable = inkling_build.prepare_engine(root, strata)
                self.assertTrue(executable.is_relative_to(root / "inkling-cuda"))
                self.assertEqual(signed.read_bytes(), b"Flash Next signed engine")
                commands = [call.args[0] for call in run.call_args_list if call.args[0][0] == "git"]
                self.assertTrue(all(command[2] == str(source) for command in commands))
                strata.find_nvcc.side_effect = AssertionError("Cached builds must not need a compiler")
                self.assertEqual(inkling_build.prepare_engine(root, strata), executable)

    def test_fix_refuses_a_different_backend_release(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "prepared-commit").write_text("a different release")
            with self.assertRaisesRegex(ValueError, "v0.2.19"):
                inkling_build.prepare_engine(root, SimpleNamespace())

    def test_optimized_reserve_keeps_large_gpu_prefill_without_changing_legacy(self):
        args = ["--max-context", "786432", "--kv", "fp8", "--vram-reserve-mib", "12288",
                "--cpu-threads", "8", "--chunk", "512"]
        self.assertEqual(serving.inkling_engine_args({"user_args": args}, optimized=True), args)
        with self.assertRaisesRegex(ValueError, "chunk"):
            serving.inkling_engine_args({"user_args": args})
        with self.assertRaisesRegex(ValueError, "chunk"):
            serving.inkling_engine_args({"user_args": ["--kv", "k8v4", "--chunk", "512"]}, optimized=True)
