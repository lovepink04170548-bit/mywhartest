import asyncio
import importlib
import os
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import installer
import browser_installer
import build_macos


class InstallerTests(unittest.TestCase):
    def test_copy_payload_skips_only_top_level_runtime_state(self):
        with TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "payload"
            install_dir = Path(tmpdir) / "install"
            (source_dir / "data").mkdir(parents=True)
            (source_dir / "data" / "runtime.txt").write_text("skip", encoding="utf-8")
            (source_dir / "config.toml").write_text("api_url = 'http://example.test'\n", encoding="utf-8")

            nested_data = source_dir / "_internal" / "cv2" / "data"
            nested_data.mkdir(parents=True)
            (nested_data / "__init__.py").write_text("# required package data\n", encoding="utf-8")
            (source_dir / "_internal" / "dependency.dll").write_bytes(b"dll")
            (source_dir / "WHartTest_Actuator.exe").write_bytes(b"MZ")

            installer.copy_payload(source_dir, install_dir)

            self.assertFalse((install_dir / "data" / "runtime.txt").exists())
            self.assertTrue((install_dir / "config.toml").exists())
            self.assertTrue((install_dir / "_internal" / "cv2" / "data" / "__init__.py").exists())
            self.assertTrue((install_dir / "_internal" / "dependency.dll").exists())

    def test_macos_app_uses_package_directory_for_runtime_assets(self):
        executable = Path('/tmp/WHartTest_Actuator_MacOS/WHartTest_Actuator.app/Contents/MacOS/WHartTest_Actuator')
        with patch.object(sys, 'frozen', True, create=True), \
                patch.object(sys, 'platform', 'darwin'), \
                patch.object(sys, 'executable', str(executable)):
            self.assertEqual(browser_installer.get_exe_dir(), executable.parents[3])

    def test_browser_executable_path_is_platform_specific(self):
        with TemporaryDirectory() as tmpdir:
            browser_root = Path(tmpdir) / 'browsers'
            executable = (
                browser_root
                / 'chromium-1200'
                / 'chrome-mac'
                / 'Chromium.app'
                / 'Contents'
                / 'MacOS'
                / 'Chromium'
            )
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b'MACH-O')

            with patch.object(browser_installer, 'get_browser_path', return_value=browser_root), \
                    patch.object(sys, 'platform', 'darwin'):
                self.assertEqual(
                    browser_installer.get_browser_executable_path(),
                    executable,
                )

    def test_macos_config_is_initialized_in_user_runtime_directory(self):
        with TemporaryDirectory() as tmpdir:
            install_dir = Path(tmpdir) / "WHartTest_Actuator"
            runtime_dir = Path(tmpdir) / "Application Support" / "WHartTest" / "Actuator"
            install_dir.mkdir(parents=True)
            (install_dir / "config.toml").write_text(
                '[server]\napi_url = "http://example.test"\n',
                encoding="utf-8",
            )
            with patch.object(sys, 'frozen', True, create=True), \
                    patch.object(sys, 'platform', 'darwin'), \
                    patch.object(browser_installer, 'get_exe_dir', return_value=install_dir), \
                    patch.object(browser_installer, 'get_runtime_dir', return_value=runtime_dir):
                config_path = browser_installer.get_config_path()

            self.assertEqual(config_path, runtime_dir / "config.toml")
            self.assertEqual(config_path.read_text(encoding="utf-8"), (install_dir / "config.toml").read_text(encoding="utf-8"))

    def test_macos_frozen_browser_path_uses_writable_user_runtime_directory(self):
        with TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir) / "Application Support" / "WHartTest" / "Actuator"
            with patch.object(sys, 'frozen', True, create=True), \
                    patch.object(sys, 'platform', 'darwin'), \
                    patch.object(browser_installer, 'get_runtime_dir', return_value=runtime_dir):
                self.assertEqual(browser_installer.get_browser_path(), runtime_dir / "browsers")


class MacOSBuildTests(unittest.TestCase):
    def test_pyinstaller_spec_invocation_does_not_include_makespec_options(self):
        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            pyinstaller_dist_dir = tmp_path / "dist" / ".macos-pyinstaller"
            app_path = pyinstaller_dist_dir / build_macos.APP_NAME
            app_path.mkdir(parents=True)
            captured: dict[str, list[str]] = {}

            def fake_run(command, cwd):
                captured["command"] = command
                return types.SimpleNamespace(returncode=0)

            with patch.object(build_macos, "PROJECT_DIR", tmp_path), \
                    patch.object(build_macos, "PYINSTALLER_DIST_DIR", pyinstaller_dist_dir), \
                    patch.object(build_macos, "BUILD_DIR", tmp_path / "build" / "macos"), \
                    patch.object(build_macos.subprocess, "run", side_effect=fake_run), \
                    patch.dict(os.environ, {"MACOS_TARGET_ARCH": ""}):
                self.assertEqual(build_macos.run_pyinstaller(), app_path)

            self.assertIn("actuator.spec", captured["command"])
            self.assertNotIn("--specpath", captured["command"])

    def test_macos_zip_fallback_can_be_disabled_for_release_upload_size_limit(self):
        with patch.dict(os.environ, {"MACOS_BUILD_ZIP": "0"}):
            self.assertFalse(build_macos.should_build_zip())

        with patch.dict(os.environ, {"MACOS_BUILD_ZIP": "1"}):
            self.assertTrue(build_macos.should_build_zip())


class MainStartupTests(unittest.TestCase):
    def test_gui_login_opens_before_browser_check(self):
        events: list[str] = []

        browser_installer = types.ModuleType("browser_installer")
        browser_installer.get_exe_dir = lambda: Path(__file__).parent
        browser_installer.setup_playwright_env = lambda: events.append("setup_playwright_env")
        browser_installer.ensure_browser = lambda browser_type: events.append("ensure_browser") or True

        websocket_client = types.ModuleType("websocket_client")

        class WebSocketClient:
            def __init__(self, *args, **kwargs):
                events.append("websocket_client")

            async def disconnect(self):
                events.append("disconnect")

        websocket_client.WebSocketClient = WebSocketClient

        consumer = types.ModuleType("consumer")

        class TaskConsumer:
            def __init__(self, *args, **kwargs):
                events.append("consumer")

            async def run(self):
                events.append("consumer_run")

        consumer.TaskConsumer = TaskConsumer

        gui = types.ModuleType("gui")
        gui.show_login_dialog = lambda config_path: events.append("gui") or {
            "api_url": "http://10.0.0.1:8000",
            "username": "admin",
            "password": "admin123456",
        }

        fake_modules = {
            "browser_installer": browser_installer,
            "websocket_client": websocket_client,
            "consumer": consumer,
            "gui": gui,
        }

        with TemporaryDirectory() as tmpdir:
            config_path = str(Path(tmpdir) / "config.toml")
            with patch.dict(sys.modules, fake_modules):
                sys.modules.pop("main", None)
                main_module = importlib.import_module("main")
                with patch.object(sys, "argv", ["main.py", "--gui", "--config", config_path]):
                    asyncio.run(main_module.main())

        self.assertLess(events.index("gui"), events.index("ensure_browser"))
        self.assertEqual(events[-1], "disconnect")


if __name__ == "__main__":
    unittest.main()
