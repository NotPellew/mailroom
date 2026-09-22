"""Flask application for Mailroom review page."""

import logging

from flask import Flask
from Mailroom.config import is_loopback_host, ConfigError
from Mailroom.routes import bp as main_bp
from Mailroom import security

logger = logging.getLogger(__name__)


def create_app(config=None) -> Flask:
    """Create and configure Flask application.

    Args:
        config: Optional configuration object

    Returns:
        Configured Flask application
    """
    app = Flask(__name__)
    # Per-instance CSRF token for same-origin state-changing requests.
    app.config["CSRF_TOKEN"] = security.generate_csrf_token()
    if config is not None:
        app.config["MAILROOM_CONFIG"] = config
        app.config["DEBUG"] = config.debug

    # Register blueprint with review routes
    app.register_blueprint(main_bp)

    @app.after_request
    def _set_local_security_headers(response):
        # The review page must never be framed by another site (clickjacking),
        # and the browser must not MIME-sniff responses.
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    return app


def run_review_server(config, host: str = "127.0.0.1", port: int = 5000):
    """Run the review server bound to loopback.

    Args:
        config: Configuration object
        host: Host to bind to (must be loopback)
        port: Port to bind to
    """
    if not is_loopback_host(host):
        raise ConfigError(
            f"Review server host must be a loopback address (e.g. 127.0.0.1 or localhost), got: {host}"
        )

    try:
        from Mailroom.db import DB
        DB(config.database_path).close()
    except Exception as e:
        logger.warning("Could not initialize database or enforce body-cache expiry on review start: %s", e)

    app = create_app(config)
    # Never expose the Werkzeug debugger/reloader on the review server, even if
    # the config asks for debug mode.
    app.run(host=host, port=port, debug=False, use_reloader=False)
