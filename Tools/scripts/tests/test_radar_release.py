"""Offline tests for release planning, feature checks and completeness gates."""

import argparse
import ast
from contextlib import redirect_stdout
import fnmatch
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import radar_release_boards as boards  # noqa: E402
import radar_release_build as build  # noqa: E402


class ReleaseTests(unittest.TestCase):
    def test_periph_radar_define_does_not_leak_into_copter(self):
        path = boards.ROOT / "Tools/AP_Periph/wscript"
        function = next(node for node in ast.parse(path.read_text()).body
                        if isinstance(node, ast.FunctionDef) and node.name == "build")
        namespace = {"fnmatch": fnmatch}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
        # ARKV6X is included by the coarse AP_PERIPH string search, even though
        # its normal Copter configuration is not an AP_Periph build.
        for board, periph, heavy, listed in (
                ("ARKV6X-bdshot", False, False, True),
                ("Pixhawk6X", False, False, False),
                ("CubeNode-ETH", True, False, True),
                ("periph-heavy", True, True, True)):
            with self.subTest(board=board):
                context = Mock()
                context.env = argparse.Namespace(BOARD=board, AP_PERIPH=periph,
                                                 AP_PERIPH_HEAVY=heavy, DEFINES=[])
                context.ap_periph_boards.return_value = [board] if listed else []
                context.ap_get_all_libraries.return_value = []
                namespace["build"](context)
                self.assertEqual("HAL_RADARODOM_ENABLED=0" in context.env.DEFINES, periph)

    def test_version(self):
        self.assertEqual(boards.validate_version("v4.7.0-dev-1.0.0"), "v4.7.0-dev-1.0.0")
        for value in ("v1.0.0", "v4.7.0-dev-1.0", "v4.7.0-dev-01.0.0", "../../file"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                boards.validate_version(value)

    def test_inventory(self):
        # The upstream board-list reader leaves input files unclosed.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            plan = boards.make_plan("v4.7.0-dev-1.0.0", "test-commit", 16)
        self.assertIn("Pixhawk6X", plan["candidates"])
        assigned = [name for batch in plan["matrix"]["include"] for name in batch["boards"]]
        self.assertCountEqual(assigned, plan["candidates"])
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertLessEqual(len(plan["matrix"]["include"]), 16)
        self.assertNotIn("SITL_x86_64_linux_gnu", assigned)
        self.assertNotIn("Pixhawk6X-bdshot", assigned)
        self.assertFalse(any(name.lower().endswith("-bdshot") for name in assigned))
        excluded = {item["board"]: item["reason"] for item in plan["excluded"]}
        self.assertEqual(excluded["Pixhawk6X-bdshot"], "bdshot variants are not published")
        self.assertEqual(excluded["ARKV6X-bdshot"], "bdshot variants are not published")
        with self.assertRaises(ValueError):
            boards.validate_board("../Pixhawk6X")

    def test_radar_preprocessor(self):
        cache = {"CXX": ["compiler"], "CPU_FLAGS": [], "CXXFLAGS": [], "DEFINES": []}
        for marker, expected in (("RADAR_RELEASE_ENABLED\n", True), ("\n", False)):
            with patch.object(build, "read_config_cache", return_value=cache), \
                    patch.object(build.subprocess, "run", return_value=argparse.Namespace(stdout=marker)):
                with tempfile.TemporaryFile(mode="w+") as log:
                    self.assertEqual(build.radar_enabled("Pixhawk6X", log), expected)

    def test_elf_requires_radar(self):
        for symbols, valid in (("AP_RadarOdom::var_info\nAP_RadarOdom::handle_odometry(args)", True),
                               ("AP_RadarOdom::var_info", False), ("", False)):
            with patch.object(build.subprocess, "run", return_value=argparse.Namespace(stdout=symbols)):
                if valid:
                    build.check_radar_elf(Path("test.elf"), None)
                else:
                    with self.assertRaises(ValueError):
                        build.check_radar_elf(Path("test.elf"), None)

    def test_disabled_board_is_skipped_before_build(self):
        with tempfile.TemporaryDirectory() as temp:
            args = argparse.Namespace(boards_json='["Pixhawk6X"]', output=Path(temp) / "out",
                                      version="v4.7.0-dev-1.0.0", commit="test", jobs=2, clean=False)
            with patch.object(build, "run") as runner, patch.object(build, "radar_enabled", return_value=False):
                self.assertEqual(build.build_batch(args), 0)
                self.assertEqual(runner.call_count, 1)
            result = json.loads((args.output / "results.json").read_text())
            self.assertEqual(result["boards"][0]["status"], "skipped")

    def test_build_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            args = argparse.Namespace(boards_json='["Pixhawk6X"]', output=Path(temp) / "out",
                                      version="v4.7.0-dev-1.0.0", commit="test", jobs=2, clean=False)
            output = io.StringIO()
            with redirect_stdout(output), \
                    patch.object(build, "run", side_effect=subprocess.CalledProcessError(1, "waf")):
                self.assertEqual(build.build_batch(args), 1)
            result = json.loads((args.output / "results.json").read_text())
            self.assertEqual(result["boards"][0]["status"], "failed")
            self.assertIn(result["boards"][0]["reason"], output.getvalue())
            log = args.output / "logs/Pixhawk6X.log"
            self.assertIn(result["boards"][0]["reason"], log.read_text())

    def test_enabled_board_with_and_without_bootloader(self):
        for has_bootloader in (False, True):
            with self.subTest(bootloader=has_bootloader), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                products = root / "build/Pixhawk6X/bin"
                products.mkdir(parents=True)
                (products / "arducopter.apj").write_bytes(b"application")
                if has_bootloader:
                    hwdef = root / "libraries/AP_HAL_ChibiOS/hwdef/Pixhawk6X"
                    hwdef.mkdir(parents=True)
                    (hwdef / "hwdef-bl.dat").write_text("# test\n")
                args = argparse.Namespace(boards_json='["Pixhawk6X"]', output=root / "out",
                                          version="v4.7.0-dev-1.0.0", commit="test", jobs=2, clean=False)
                with patch.object(build, "ROOT", root), patch.object(build, "run") as runner, \
                        patch.object(build, "radar_enabled", return_value=True), \
                        patch.object(build, "check_radar_elf"):
                    def mock_run(command, log):
                        if command[1] == "bootloader":
                            (products / "AP_Bootloader.bin").write_bytes(b"bootloader")
                    runner.side_effect = mock_run
                    self.assertEqual(build.build_batch(args), 0)
                    self.assertEqual(runner.call_count, 5 if has_bootloader else 2)
                result = json.loads((args.output / "results.json").read_text())["boards"][0]
                self.assertEqual(result["status"], "built")
                self.assertEqual(result["firmware"], "Pixhawk6X_ArduCopter.apj")
                self.assertEqual((args.output / result["firmware"]).read_bytes(), b"application")
                self.assertEqual("bootloader" in result, has_bootloader)
                if has_bootloader:
                    self.assertEqual(result["bootloader"], "Pixhawk6X_bootloader.bin")
                    self.assertEqual((args.output / result["bootloader"]).read_bytes(), b"bootloader")

    def test_bootloader_failure_blocks_board(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            products = root / "build/Pixhawk6X/bin"
            products.mkdir(parents=True)
            (products / "arducopter.apj").write_bytes(b"application")
            hwdef = root / "libraries/AP_HAL_ChibiOS/hwdef/Pixhawk6X"
            hwdef.mkdir(parents=True)
            (hwdef / "hwdef-bl.dat").write_text("# test\n")
            args = argparse.Namespace(boards_json='["Pixhawk6X"]', output=root / "out",
                                      version="v4.7.0-dev-1.0.0", commit="test", jobs=2, clean=False)
            calls = [None, None, None, None, subprocess.CalledProcessError(1, "bootloader")]
            with patch.object(build, "ROOT", root), patch.object(build, "run", side_effect=calls), \
                    patch.object(build, "radar_enabled", return_value=True), \
                    patch.object(build, "check_radar_elf"):
                self.assertEqual(build.build_batch(args), 1)
            result = json.loads((args.output / "results.json").read_text())["boards"][0]
            self.assertEqual(result["status"], "failed")

    def fixture(self, root):
        plan = {"version": "v4.7.0-dev-1.0.0", "commit": "test-commit",
                "candidates": ["Pixhawk6X", "small-board"], "excluded": []}
        build.write_json(root / "plan.json", plan)
        batch = root / "batches/radar-batch-0"
        batch.mkdir(parents=True)
        (batch / "Pixhawk6X_ArduCopter.apj").write_bytes(b"application")
        (batch / "Pixhawk6X_bootloader.bin").write_bytes(b"bootloader")
        result = {"version": plan["version"], "commit": plan["commit"], "boards": [
            {"board": "Pixhawk6X", "status": "built", "firmware": "Pixhawk6X_ArduCopter.apj",
             "bootloader": "Pixhawk6X_bootloader.bin"},
            {"board": "small-board", "status": "skipped", "reason": "HAL_RADARODOM_ENABLED=0"},
        ]}
        build.write_json(batch / "results.json", result)
        args = argparse.Namespace(plan=root / "plan.json", results=root / "batches", output=root / "assets")
        return args, batch / "results.json", result

    def test_collect_assets_and_checksums(self):
        with tempfile.TemporaryDirectory() as temp:
            args, _, _ = self.fixture(Path(temp))
            self.assertEqual(build.collect(args), 0)
            self.assertEqual((args.output / "supported-boards.txt").read_text(), "Pixhawk6X\n")
            self.assertTrue((args.output / "Pixhawk6X_ArduCopter.apj").is_file())
            self.assertTrue((args.output / "Pixhawk6X_bootloader.bin").is_file())
            self.assertFalse(any("v4.7.0" in item.name for item in args.output.iterdir()))
            for line in (args.output / "SHA256SUMS").read_text().splitlines():
                digest, name = line.split("  ", 1)
                self.assertEqual(digest, hashlib.sha256((args.output / name).read_bytes()).hexdigest())

    def test_collect_rejects_incomplete_failed_or_wrong_commit(self):
        for failure in ("missing", "failed", "commit", "duplicate", "asset", "no-firmware"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                args, path, result = self.fixture(Path(temp))
                if failure == "missing":
                    result["boards"].pop()
                elif failure == "failed":
                    result["boards"][1]["status"] = "failed"
                elif failure == "commit":
                    result["commit"] = "wrong"
                elif failure == "duplicate":
                    result["boards"].append(result["boards"][0])
                elif failure == "asset":
                    result["boards"][0]["firmware"] = "../wrong.apj"
                else:
                    (path.parent / "Pixhawk6X_ArduCopter.apj").unlink()
                build.write_json(path, result)
                with self.assertRaises(ValueError):
                    build.collect(args)


if __name__ == "__main__":
    unittest.main()
