"""End-to-end tests for standalone desktop application launcher, companion, and packaging."""

import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from Mailroom.config import Config, ConfigError
from Mailroom.db import DB
from Mailroom.desktop import DesktopApp, find_available_loopback_port


class TestDesktopApp(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.config_path = str(self.tmp_path / "config.json")
        self.config = Config(self.config_path)
        self.config.save()

    def tearDown(self):
        self.tmpdir.cleanup()

    # --- Initialization & Port Selection ---

    def test_desktop_app_init_auto_creates_config_and_db(self):
        """When DesktopApp is initialized without explicit config, it creates default config and DB."""
        with tempfile.TemporaryDirectory() as user_dir:
            custom_cfg_path = Path(user_dir) / "app_config.json"
            cfg = Config(str(custom_cfg_path))
            DesktopApp(config=cfg, open_browser=False)

            self.assertTrue(custom_cfg_path.exists())
            self.assertTrue(Path(cfg.database_path).exists())

            # Verify schema was initialized
            db = DB(cfg.database_path)
            try:
                row = db.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
                self.assertGreaterEqual(row[0], 5)
            finally:
                db.close()

    def test_desktop_app_rejects_non_loopback_host(self):
        """DesktopApp rejects non-loopback hosts with ConfigError."""
        with self.assertRaises(ConfigError):
            DesktopApp(config=self.config, host="0.0.0.0", open_browser=False)
        with self.assertRaises(ConfigError):
            DesktopApp(config=self.config, host="192.168.1.100", open_browser=False)

    def test_find_available_loopback_port_skips_occupied_port(self):
        """find_available_loopback_port skips occupied ports and returns an available one."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 5000))
            s.listen(1)

            port = find_available_loopback_port(start_port=5000)
            self.assertNotEqual(port, 5000)
            self.assertGreater(port, 5000)

    def test_desktop_app_explicit_occupied_port_raises_config_error(self):
        """When an explicit port is requested but occupied, DesktopApp raises ConfigError."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 5000))
            s.listen(1)

            with self.assertRaises(ConfigError):
                DesktopApp(config=self.config, port=5000, open_browser=False)

    # --- Server Lifecycle & HTTP Serving ---

    def test_desktop_app_server_lifecycle_and_http_serving(self):
        """DesktopApp starts threaded server, serves HTTP requests, and shuts down cleanly."""
        desktop = DesktopApp(config=self.config, open_browser=False)
        desktop.start_server()
        try:
            status = desktop.get_status()
            self.assertTrue(status["running"])
            self.assertEqual(status["port"], desktop.port)
            self.assertIn("http://127.0.0.1:", status["url"])

            # Test real HTTP GET / (dashboard review page)
            resp = requests.get(desktop.dashboard_url, timeout=5)
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Mailroom", resp.text)
            self.assertIn("text/html", resp.headers["Content-Type"])

            # Test real HTTP GET /api/status
            api_resp = requests.get(f"{desktop.dashboard_url}/api/status", timeout=5)
            self.assertEqual(api_resp.status_code, 200)
            data = api_resp.json()
            self.assertIn("accounts", data)
            self.assertIn("messages", data)
        finally:
            desktop.stop()

        # Verify stopped
        time.sleep(0.2)
        status_after = desktop.get_status()
        self.assertFalse(status_after["running"])

    @patch("webbrowser.open")
    def test_desktop_app_open_dashboard(self, mock_browser):
        """open_dashboard opens default browser with correct URL."""
        desktop = DesktopApp(config=self.config, open_browser=False)
        desktop.open_dashboard()
        mock_browser.assert_called_once_with(f"http://127.0.0.1:{desktop.port}/")

    # --- Headless & Display Fallback ---

    @patch("webbrowser.open")
    def test_desktop_app_run_headless_fallback(self, _mock_browser):
        """When Tkinter/display is unavailable, DesktopApp runs headless loop until stopped."""
        desktop = DesktopApp(config=self.config, open_browser=False)

        # Start desktop in background thread
        run_thread = threading.Thread(target=desktop.run, daemon=True)

        with patch.object(desktop, "_run_tkinter_companion", side_effect=Exception("No display")):
            run_thread.start()
            time.sleep(0.3)

            # Server is running and serving
            resp = requests.get(desktop.dashboard_url, timeout=5)
            self.assertEqual(resp.status_code, 200)

            # Signal stop
            desktop.stop()
            run_thread.join(timeout=3)
            self.assertFalse(run_thread.is_alive())

    # --- CLI Integration Tests ---

    @patch("Mailroom.desktop.DesktopApp.run", return_value=0)
    def test_cli_default_no_args_launches_desktop(self, mock_run):
        """Running mailroom without arguments defaults to launching the desktop app."""
        from Mailroom import cli

        exit_code = None
        with patch("sys.argv", ["mailroom"]):
            try:
                cli.main([])
            except SystemExit as e:
                exit_code = e.code

        self.assertEqual(exit_code, 0)
        mock_run.assert_called_once()

    @patch("Mailroom.desktop.DesktopApp.run", return_value=0)
    def test_cli_app_command_with_flags(self, mock_run):
        """mailroom app with flags passes configuration to DesktopApp."""
        from Mailroom import cli

        exit_code = None
        argv = ["app", "--no-browser", "--port", "5099", "--host", "127.0.0.1"]
        try:
            cli.main(argv)
        except SystemExit as e:
            exit_code = e.code

        self.assertEqual(exit_code, 0)
        mock_run.assert_called_once()

    # --- Shutdown API Endpoint Tests ---

    def test_api_system_quit_endpoint(self):
        """POST /api/system/quit triggers shutdown callback cleanly."""
        desktop = DesktopApp(config=self.config, open_browser=False)
        desktop.start_server()
        try:
            # Fetch CSRF token from page
            page = requests.get(desktop.dashboard_url, timeout=5).text
            import re
            csrf_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', page)
            csrf_token = csrf_match.group(1) if csrf_match else ""

            # 1. Host header enforcement
            bad_host_res = requests.post(
                f"{desktop.dashboard_url}/api/system/quit",
                headers={"Host": "evil.com", "X-CSRF-Token": csrf_token},
                timeout=5,
            )
            self.assertEqual(bad_host_res.status_code, 400)

            # 2. CSRF enforcement
            no_csrf_res = requests.post(
                f"{desktop.dashboard_url}/api/system/quit",
                timeout=5,
            )
            self.assertEqual(no_csrf_res.status_code, 403)

            # 3. Successful quit request
            shutdown_called = threading.Event()
            desktop.stop = lambda: shutdown_called.set()

            # Re-register stop callback on app config
            app = desktop.server.app
            app.config["SERVER_SHUTDOWN"] = desktop.stop

            res = requests.post(
                f"{desktop.dashboard_url}/api/system/quit",
                headers={"X-CSRF-Token": csrf_token},
                timeout=5,
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["status"], "shutting_down")

            # Verify shutdown was called asynchronously
            self.assertTrue(shutdown_called.wait(timeout=2.0))
        finally:
            desktop._stop_event.set()
            if desktop.server:
                desktop.server.shutdown()


if __name__ == "__main__":
    unittest.main()
