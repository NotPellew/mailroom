"""Standalone desktop application launcher and companion controller for Mailroom."""

import logging
import os
import signal
import socket
import sys
import threading
import webbrowser
from typing import Optional

from werkzeug.serving import make_server

from Mailroom.config import Config, ConfigError, is_loopback_host
from Mailroom.db import DB

logger = logging.getLogger(__name__)

# PyInstaller noconsole on Windows initializes sys.stdout and sys.stderr to None.
# Prevent AttributeError: 'NoneType' object has no attribute 'write'.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def find_available_loopback_port(host: str = "127.0.0.1", start_port: int = 5000, max_tries: int = 50) -> int:
    """Find an available port on loopback starting from start_port.

    Useful on macOS where AirPlay Receiver may claim port 5000, or when
    another local development server is running.
    """
    for port in range(start_port, start_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise ConfigError(f"No available loopback port found in range {start_port}..{start_port + max_tries}")


class DesktopApp:
    """Mailroom Desktop Application manager.

    Starts the loopback Flask server in a background thread, auto-initializes
    configuration and database, opens the default browser, and runs a native
    companion window (or headless event loop) for control and clean shutdown.
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        host: str = "127.0.0.1",
        port: Optional[int] = None,
        open_browser: bool = True,
    ):
        if not is_loopback_host(host):
            raise ConfigError(f"Desktop application host must be a loopback address, got: {host}")

        self.host = host
        self.config = config or Config()
        self.open_browser = open_browser

        # Ensure database is initialized with current schema
        try:
            DB(self.config.database_path).close()
        except Exception as e:
            logger.warning("Could not auto-initialize database on desktop startup: %s", e)

        # Resolve port
        if port is not None:
            # Explicit port requested: verify availability or fail fast
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind((self.host, port))
                    self.port = port
                except OSError as e:
                    raise ConfigError(f"Requested port {port} is unavailable on {self.host}: {e}") from e
        else:
            self.port = find_available_loopback_port(self.host, start_port=5000)

        self.dashboard_url = f"http://{self.host}:{self.port}"
        self.server = None
        self._server_thread = None
        self._stop_event = threading.Event()
        self._root = None

    def start_server(self) -> None:
        """Start Werkzeug threaded HTTP server on loopback in a background daemon thread."""
        from Mailroom.app import create_app

        app = create_app(self.config)
        app.config["SERVER_SHUTDOWN"] = self.stop

        self.server = make_server(self.host, self.port, app, threaded=True)
        self._server_thread = threading.Thread(
            target=self.server.serve_forever,
            name="MailroomServerThread",
            daemon=True,
        )
        self._server_thread.start()
        logger.info("Mailroom server started at %s", self.dashboard_url)

    def open_dashboard(self, path: str = "/") -> bool:
        """Open the default browser to the review dashboard."""
        target = f"{self.dashboard_url}{path}"
        try:
            return webbrowser.open(target)
        except Exception as e:
            logger.warning("Could not open browser automatically: %s", e)
            return False

    def get_status(self) -> dict:
        """Return runtime status of the desktop application."""
        is_running = self.server is not None and not self._stop_event.is_set()
        db_messages = 0
        db_decisions = 0
        try:
            db_obj = DB(self.config.database_path)
            status_data = db_obj.get_status()
            db_messages = status_data.get("messages", 0)
            db_decisions = status_data.get("decisions", 0)
            db_obj.close()
        except Exception:
            pass

        return {
            "running": is_running,
            "url": self.dashboard_url,
            "host": self.host,
            "port": self.port,
            "database_path": str(self.config.database_path),
            "messages": db_messages,
            "decisions": db_decisions,
        }

    def stop(self) -> None:
        """Signal clean shutdown for server and companion controller."""
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        logger.info("Shutting down Mailroom desktop application...")

        if self.server:
            try:
                self.server.shutdown()
            except Exception as e:
                logger.warning("Error during server shutdown: %s", e)

        if self._root:
            try:
                self._root.after(0, self._root.destroy)
            except Exception:
                pass

    def _run_tkinter_companion(self) -> None:
        """Run compact native companion window using standard library tkinter."""
        import tkinter as tk

        root = tk.Tk()
        self._root = root
        root.title("Mailroom")
        root.geometry("380x240")
        root.resizable(False, False)

        # Intercept window close (X button) to stop cleanly
        root.protocol("WM_DELETE_WINDOW", self.stop)

        # Header Frame
        header_frame = tk.Frame(root, padx=16, pady=12)
        header_frame.pack(fill="x")

        title_label = tk.Label(
            header_frame,
            text="Mailroom",
            font=("Helvetica", 14, "bold"),
            fg="#0f3460",
        )
        title_label.pack(anchor="w")

        status_text = f"Running on {self.dashboard_url}"
        status_label = tk.Label(
            header_frame,
            text=status_text,
            font=("Helvetica", 9),
            fg="#28a745",
        )
        status_label.pack(anchor="w", pady=(2, 0))

        sub_label = tk.Label(
            header_frame,
            text="Local-first email classification & review",
            font=("Helvetica", 8),
            fg="#6c757d",
        )
        sub_label.pack(anchor="w", pady=(2, 0))

        # Divider
        divider = tk.Frame(root, height=1, bg="#e3e6ea")
        divider.pack(fill="x", padx=16, pady=4)

        # Buttons Frame
        btn_frame = tk.Frame(root, padx=16, pady=10)
        btn_frame.pack(fill="both", expand=True)

        open_btn = tk.Button(
            btn_frame,
            text="Open Mailroom Dashboard",
            command=self.open_dashboard,
            bg="#0f3460",
            fg="white",
            relief="flat",
            padx=8,
            pady=6,
            font=("Helvetica", 9, "bold"),
            cursor="hand2",
        )
        open_btn.pack(fill="x", pady=3)

        quit_btn = tk.Button(
            btn_frame,
            text="Quit Mailroom",
            command=self.stop,
            bg="#f8f9fa",
            fg="#dc3545",
            relief="groove",
            padx=8,
            pady=4,
            font=("Helvetica", 9),
            cursor="hand2",
        )
        quit_btn.pack(fill="x", pady=3)

        # Periodic check for stop event
        def check_stop():
            if self._stop_event.is_set():
                try:
                    root.destroy()
                except Exception:
                    pass
            else:
                root.after(200, check_stop)

        root.after(200, check_stop)
        root.mainloop()

    def _run_headless_loop(self) -> None:
        """Headless fallback for environments without a display or Tkinter."""
        def handle_signal(sig, frame):
            self.stop()

        try:
            signal.signal(signal.SIGINT, handle_signal)
            signal.signal(signal.SIGTERM, handle_signal)
        except (ValueError, AttributeError):
            pass

        print(f"Mailroom running at {self.dashboard_url}")
        print("Press Ctrl+C to quit.")
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=0.5)

    def run(self) -> int:
        """Start the application and block until shutdown."""
        self.start_server()

        if self.open_browser:
            self.open_dashboard()

        # Try launching companion GUI, fallback gracefully to headless loop
        has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or sys.platform in ("win32", "darwin"))
        gui_started = False

        if has_display:
            try:
                self._run_tkinter_companion()
                gui_started = True
            except (ImportError, ModuleNotFoundError, Exception) as e:
                logger.info("Tkinter companion unavailable (%s); falling back to background monitor", e)

        if not gui_started:
            self._run_headless_loop()

        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=2.0)

        return 0


def main() -> int:
    """CLI / Executable entry point for standalone desktop launcher."""
    app = DesktopApp()
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
