"""Configuration handling for EmailMan."""

import os
import json
import shutil
import tempfile
import urllib.parse
import ipaddress
from pathlib import Path
from typing import List, Dict, Any, Optional


class ConfigError(Exception):
    """Raised when configuration is invalid."""
    pass


def default_app_dir() -> Path:
    """Return the per-user EmailMan data directory.

    On Windows this is ``%LOCALAPPDATA%\\EmailMan``. On Linux/macOS it is
    ``$XDG_CONFIG_HOME/emailman`` or ``~/.config/emailman``.
    """
    local_appdata = os.getenv("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "EmailMan"
    xdg = os.getenv("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "emailman"
    home = os.getenv("HOME") or str(Path.home())
    return Path(home) / ".config" / "emailman"


def is_loopback_host(hostname: Optional[str]) -> bool:
    """Check if a hostname or IP string represents a loopback address.

    Args:
        hostname: Hostname or IP address string

    Returns:
        True if hostname is loopback (e.g. localhost, 127.0.0.1, ::1)
    """
    if not hostname:
        return False
    hostname = hostname.strip().lower()
    if hostname == "localhost":
        return True
    if hostname.startswith("[") and hostname.endswith("]"):
        hostname = hostname[1:-1]
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_loopback
    except ValueError:
        return False


def is_loopback_url(url: Optional[str]) -> bool:
    """Check that a URL is http(s) and points at a loopback host.

    Used to validate model-endpoint overrides at the same strictness as the
    configured endpoint, so inference cannot be redirected off-host.
    """
    if not url or not isinstance(url, str):
        return False
    if not url.startswith(("http://", "https://")):
        return False
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    return is_loopback_host(parsed.hostname)


# Retired label ids mapped to their current equivalents. Saved decisions from
# before a rename keep the old id, so normalize on read before comparing against
# the live vocabulary (e.g. when applying labels or recording a snapshot).
LEGACY_LABEL_ALIASES: Dict[str, str] = {
    "Retention/Keep": "Retention/Forever",
    "Retention/Ephemeral": "Retention/30Days",
    "Retention/Review": "Retention/1Year",
    "Type/VerificationCode": "Type/Verification",
    "Action/NeedsReply": "Type/NeedsReply",
}


def normalize_label_id(label_id: Optional[str]) -> str:
    """Map a retired label id to its current equivalent (spaces stripped)."""
    compact = (label_id or "").replace(" ", "")
    return LEGACY_LABEL_ALIASES.get(compact, compact)


HARDWARE_PROFILES: Dict[str, Dict[str, Any]] = {
    "tabby": {
        "name": "TabbyAPI (NVIDIA GPU)",
        "provider": "tabby",
        "endpoint": "http://127.0.0.1:8080/completion",
        "id": "Qwen2.5-7B-Instruct-EXL3",
    },
    "standard": {
        "name": "Standard (Apple Silicon / 8-16 GB VRAM GPU)",
        "provider": "ollama",
        "endpoint": "http://127.0.0.1:11434",
        "id": "qwen2.5:7b",
    },
    "lightweight": {
        "name": "Lightweight (CPU / older hardware / 4-8 GB RAM)",
        "provider": "ollama",
        "endpoint": "http://127.0.0.1:11434",
        "id": "qwen2.5:3b",
    },
}


class Config:
    """Configuration management."""

    HARDWARE_PROFILES = HARDWARE_PROFILES

    # Default label definitions
    DEFAULT_LABELS: List[Dict[str, Any]] = [
        {
            "id": "Type/Newsletter",
            "name": "Newsletter",
            "axis": "kind",
            "description": "Generic mass mail sent to a broad list: pricing or availability updates, product announcements, deals, and recurring digests. Not specific to you.",
            "examples": ["Weekly digest", "Product announcement", "Limited-time deal"],
            "exclusions": [
                "Mail specific to your account or an order",
                "Order receipt or invoice",
                "OTP or sign-in alert",
            ],
        },
        {
            "id": "Type/Targeted",
            "name": "Targeted",
            "axis": "kind",
            "description": "Company or service mail that is specific to you rather than a generic mass mailing: account and service notices, changes, invitations, and booking or order updates. Not a generic newsletter/pricing/availability update, and not personal 1:1 mail.",
            "examples": [
                "Account notice for your subscription",
                "Service change affecting your account",
                "Invitation to a group you belong to",
            ],
            "exclusions": [
                "Generic newsletter, pricing/availability update, or mass deal",
                "A message whose main point is that you must act (also set NeedsReply or NeedsAction)",
                "Personal 1:1 correspondence",
            ],
        },
        {
            "id": "Type/Personal",
            "name": "Personal",
            "axis": "kind",
            "description": "A 1:1 message from a person to you, not a company or automated sender.",
            "examples": ["Note from a friend", "Personal reply in a thread", "Message from a family member"],
            "exclusions": [
                "Company or automated notification",
                "Newsletter or mass mail",
                "Receipt or invoice",
            ],
        },
        {
            "id": "Type/NeedsReply",
            "name": "Needs reply",
            "axis": "kind",
            "description": "Overlay: a written reply is expected from you - answer a question, respond to a person, or send information back. Combine it with the matching kind label. Not for mail that only requires an action in a system (use NeedsAction).",
            "examples": ["Please reply by Friday", "Can you answer this?", "Awaiting your response"],
            "exclusions": [
                "You only need to act in a system, not reply (use NeedsAction)",
                "OTP or magic link",
                "Shipping update with nothing to do",
                "Receipt you only need to file",
            ],
        },
        {
            "id": "Type/NeedsAction",
            "name": "Needs action",
            "axis": "kind",
            "description": "Overlay: you must act in a system, not reply - log in, open a link, confirm, pay, or update settings. Combine it with the matching kind label. Not for mail where a written reply is expected (use NeedsReply).",
            "examples": ["Confirm your email address", "Your payment is due", "Sign in to keep your account active"],
            "exclusions": [
                "A written reply is expected (use NeedsReply)",
                "OTP or magic link",
                "Newsletter",
                "Receipt you only need to file",
            ],
        },
        {
            "id": "Type/SecurityAlert",
            "name": "Security Alert",
            "axis": "kind",
            "description": "Sign-in, passkey, OAuth app, or suspicious-activity notice about your account. Not a code.",
            "examples": [
                "New sign-in to your account",
                "A new OAuth app was added to your account",
                "New passkey added",
            ],
            "exclusions": ["Verification code or magic link", "Newsletter about security products"],
        },
        {
            "id": "Type/Verification",
            "name": "Verification",
            "axis": "kind",
            "description": "One-time code, magic link, or access PIN. Delete after use.",
            "examples": ["Your verification code is 123456", "Magic link for your login", "Access PIN"],
            "exclusions": [
                "Security alert with no code",
                "Recovery-code list or password-change notice",
            ],
        },
        {
            "id": "Type/Receipt",
            "name": "Receipt",
            "axis": "kind",
            "description": "Invoice, payment, dunning, or order confirmation. When it is a purchase, also set exactly one Purchase/* label.",
            "examples": ["Order confirmation", "Payment receipt", "Invoice"],
            "exclusions": [
                "Shipping update without payment info",
                "Newsletter or abandoned-cart promo",
            ],
        },
        {
            "id": "Type/ShippingUpdate",
            "name": "Shipping Update",
            "axis": "kind",
            "description": "A parcel moved, is out for delivery, or was delivered. Not the invoice.",
            "examples": ["Your parcel is out for delivery", "Your order was delivered"],
            "exclusions": [
                "Invoice or payment receipt",
                "Order confirmation before the parcel exists",
            ],
        },
        {
            "id": "Purchase/Tech",
            "name": "Tech",
            "axis": "purchase",
            "description": "Receipt or invoice for hardware, software, apps, APIs, or digital storefronts.",
            "examples": ["App store receipt", "Software invoice", "Cloud API invoice"],
            "exclusions": [
                "Groceries or restaurants",
                "Electricity / water / internet utility bill",
                "Clothing or apparel",
            ],
        },
        {
            "id": "Purchase/FoodDrink",
            "name": "Food & drink",
            "axis": "purchase",
            "description": "Receipt or invoice for groceries, restaurants, delivery, or beverages.",
            "examples": ["Supermarket receipt", "Restaurant bill", "Food delivery receipt"],
            "exclusions": [
                "Hardware, software, or API invoices",
                "Utility bills",
                "Clothing",
            ],
        },
        {
            "id": "Retention/30Days",
            "name": "30 days",
            "axis": "retention",
            "description": "Disposable once read: keep about a month, then it stops mattering. Use only for transient content you will not revisit; when unsure between this and 1 year, choose 1 year. A relevance judgement; not tied to the message type.",
            "examples": ["Sale ends today", "Package delivered", "One-time code", "Expiring promo"],
            "exclusions": [
                "Invoices or receipts (always keep forever)",
                "Account, booking, or event mail (keep 1 year or longer)",
                "Tax documents",
                "Contracts",
            ],
        },
        {
            "id": "Retention/1Year",
            "name": "1 year",
            "axis": "retention",
            "description": "Default for real mail: keep about a year. Worth referencing later but not proof of a purchase, e.g. account or registration notices, bookings, event details, results, and reminders about something already arranged. A relevance judgement; not tied to the message type.",
            "examples": ["Booking confirmation", "Event announcement", "Account change notice", "Registration follow-up", "Election result"],
            "exclusions": [
                "Invoices or receipts (always keep forever)",
                "One-time code or delivery ping (use 30 days)",
                "Contracts or tax (use Forever)",
            ],
        },
        {
            "id": "Retention/Forever",
            "name": "Forever",
            "axis": "retention",
            "description": "Keep indefinitely: the record copy. Invoices and receipts always go here; also tax, contracts, utility bills or dunning, and account or security proof. A relevance judgement; not tied to the message type.",
            "examples": ["Annual invoice", "Tax receipt", "Signed contract", "Utility dunning", "Warranty invoice"],
            "exclusions": [
                "One-time code",
                "Newsletter",
                "Shipping ping",
                "Event or booking details (use 1 year)",
            ],
        },
    ]

    DEFAULT_LABEL_IDS: List[str] = [label["id"] for label in DEFAULT_LABELS]

    def __init__(
        self,
        config_path: Optional[str] = None,
        profile: Optional[str] = None,
        labels: Optional[List[Dict[str, Any]]] = None,
    ):
        """Initialize configuration.

        Args:
            config_path: Path to config file (defaults to the per-user app dir)
            profile: Optional hardware profile name for default configuration
            labels: Optional custom label definitions overriding defaults
        """
        if config_path is None:
            config_path = str(default_app_dir() / "config.json")
        else:
            config_path = os.path.abspath(config_path)

        self.config_path = Path(config_path)
        self.config_dir = self.config_path.parent

        # Ensure config directory exists
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Load or create config
        self._load_config(profile=profile)
        if labels is not None:
            self._config["labels"] = labels
            self.validate()

    def _load_config(self, profile: Optional[str] = None):
        """Load configuration from file or use defaults."""
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                self._config = json.load(f)
        else:
            self._config = self._default_config(profile=profile)
            # Save the default configuration
            self.save()

    def _default_config(self, profile: Optional[str] = None) -> Dict[str, Any]:
        """Return default configuration."""
        selected_profile = self.HARDWARE_PROFILES.get(
            profile or "standard", self.HARDWARE_PROFILES["standard"]
        )
        return {
            "model": {
                "endpoint": selected_profile["endpoint"],
                "id": selected_profile["id"],
                "provider": selected_profile["provider"],
            },
            "timeout": 30.0,  # seconds
            "sample_limit": 100,  # messages to process
            "labels": self.DEFAULT_LABELS,
            "debug": False,
        }

    def _write_config(self) -> None:
        """Write the configuration atomically: temp file in the same dir + replace."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.config_dir), prefix=self.config_path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._config, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.config_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def save(self):
        """Save configuration to file."""
        self._write_config()

    def set_labels(self, labels: List[Dict[str, Any]]) -> None:
        """Replace the label definitions, validating before touching disk.

        On any failure the previous in-memory state is restored exactly and the
        config file is left unchanged. On success the current file is backed up
        to ``config.json.bak-label-edit`` before the new labels are written.
        """
        had_labels = "labels" in self._config
        previous = self._config.get("labels")

        def restore() -> None:
            if had_labels:
                self._config["labels"] = previous
            else:
                self._config.pop("labels", None)

        self._config["labels"] = labels
        try:
            self.validate()
        except ConfigError:
            restore()
            raise

        backup_path = self.config_path.parent / (self.config_path.name + ".bak-label-edit")
        try:
            if self.config_path.exists():
                shutil.copy2(self.config_path, backup_path)
            self.save()
        except BaseException:
            restore()
            raise

    def validate(self):
        """Validate configuration and raise ConfigError if invalid."""
        # Validate model configuration
        model = self._config.get("model", {})
        if not isinstance(model, dict):
            raise ConfigError("'model' section must be a dictionary")

        if "endpoint" not in model:
            raise ConfigError("Missing required 'model.endpoint' in configuration")

        endpoint = model["endpoint"]
        if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
            raise ConfigError("Model endpoint must use http:// or https://")

        try:
            parsed = urllib.parse.urlparse(endpoint)
        except Exception as e:
            raise ConfigError(f"Invalid model endpoint URL: {e}")

        if not is_loopback_host(parsed.hostname):
            raise ConfigError(
                f"Model endpoint must be a loopback address (e.g., http://127.0.0.1:8080 or http://localhost:8080), got '{endpoint}'"
            )

        if "id" not in model or not model["id"]:
            raise ConfigError("Missing required 'model.id' in configuration")

        provider = model.get("provider")
        if provider is not None and provider not in ("ollama", "tabby", "openai", "auto"):
            raise ConfigError(
                f"Invalid model provider '{provider}'. Must be 'ollama', 'tabby', 'openai', or 'auto'"
            )

        # Validate sample limit (bool is a subclass of int)
        sample_limit = self._config.get("sample_limit")
        if not isinstance(sample_limit, int) or isinstance(sample_limit, bool) or sample_limit <= 0:
            raise ConfigError("sample_limit must be a positive integer")

        # Validate timeout
        timeout = self._config.get("timeout")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or timeout <= 0
        ):
            raise ConfigError("timeout must be a positive number")

        # Validate the same label list that Config.labels returns
        label_ids = set()
        label_names = set()

        labels = self.labels
        if not isinstance(labels, list):
            raise ConfigError("labels must be a list")
        if not labels:
            raise ConfigError("labels must contain at least one label")

        for label in labels:
            if not isinstance(label, dict):
                raise ConfigError("Each label must be an object")

            # Validate label structure
            required_fields = ["id", "name", "description"]
            for field in required_fields:
                if field not in label or not label[field]:
                    raise ConfigError(f"Label missing required field: {field}")
                if not isinstance(label[field], str):
                    raise ConfigError(f"Label field '{field}' must be a string")

            # Check for duplicate IDs
            if label["id"] in label_ids:
                raise ConfigError(f"Duplicate label ID: {label['id']}")
            label_ids.add(label["id"])

            # Check for duplicate names
            if label["name"] in label_names:
                raise ConfigError(f"Duplicate label name: {label['name']}")
            label_names.add(label["name"])

            if "exclusions" not in label:
                raise ConfigError(f"Label '{label['id']}' is missing 'exclusions' field")
            if not isinstance(label["exclusions"], list):
                raise ConfigError(f"Label '{label['id']}' exclusions must be a list")
            if "examples" in label and not isinstance(label["examples"], list):
                raise ConfigError(f"Label '{label['id']}' examples must be a list")

        # Check debug mode
        debug = self._config.get("debug", False)
        if not isinstance(debug, bool):
            raise ConfigError("debug must be a boolean")

    def get(self, key: str, default=None):
        """Get a configuration value.

        Args:
            key: Configuration key (dot notation supported)
            default: Default value if key not found

        Returns:
            Configuration value
        """
        value = self._config
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    @property
    def model_endpoint(self) -> str:
        """Get model endpoint."""
        return self._config["model"]["endpoint"]

    @property
    def model_provider(self) -> str:
        """Get model provider."""
        return self._config.get("model", {}).get("provider", "auto")

    @property
    def model_id(self) -> str:
        """Get model ID."""
        return self._config["model"]["id"]

    @property
    def timeout(self) -> float:
        """Get timeout in seconds."""
        return float(self._config["timeout"])

    @property
    def sample_limit(self) -> int:
        """Get sample limit."""
        return self._config["sample_limit"]

    @property
    def labels(self) -> List[Dict[str, Any]]:
        """Get label definitions."""
        return self._config.get("labels", self.DEFAULT_LABELS)

    def get_label_ids(self) -> List[str]:
        """Get list of active label IDs."""
        return [label["id"] for label in self.labels]

    @property
    def debug(self) -> bool:
        """Get debug mode."""
        return self._config.get("debug", False)

    @property
    def database_path(self) -> str:
        """Get database path within config directory."""
        return str(self.config_dir / "EmailMan.db")


DEFAULT_LABEL_IDS: List[str] = Config.DEFAULT_LABEL_IDS
