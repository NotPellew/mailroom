"""Headless-browser inertness and link verification for the review page.

The real Flask review page is rendered in bundled headless Chromium: hostile
email content must stay inert text, the page must make no external requests,
Gmail links must be well formed, and the local security headers must be present.

Run with ``pytest -m browser`` after installing the ``browser`` extra and
``python -m playwright install chromium``. Set ``MAILROOM_REQUIRE_BROWSER=1`` to
turn a missing browser into a hard failure instead of a skip.
"""

import os
import shutil
import tempfile
import threading
from urllib.parse import urlparse

import pytest
from werkzeug.serving import make_server

from Mailroom import app as app_module
from Mailroom import config as config_module
from Mailroom import db as db_module

pytestmark = pytest.mark.browser

REQUIRE_BROWSER = os.environ.get("MAILROOM_REQUIRE_BROWSER") == "1"

if REQUIRE_BROWSER:
    import playwright.sync_api as _playwright_api
else:
    _playwright_api = pytest.importorskip(
        "playwright.sync_api",
        reason="install the 'browser' extra to run these tests",
    )

PlaywrightError = _playwright_api.Error
sync_playwright = _playwright_api.sync_playwright

PAYLOADS = [
    "<script>window.__xss=1</script>",
    '<img src=x onerror="window.__xss=2">',
    "<svg onload=window.__xss=3>",
    '<iframe src="http://evil.example/"></iframe>',
    '<a href="javascript:window.__xss=4">x</a>',
    "<body onload=window.__xss=5>",
    "<b>subject</b>",
]

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

GMAIL_ID = "m1"
QUOTED_GMAIL_ID = 'm 1"x'

# Expected Gmail URLs are independent literals so the assertion cannot pass just
# because the route helper and the test share the same builder.
EXPECTED_GMAIL_URL = "https://mail.google.com/mail/u/0/?authuser=user%40example.com#all/m1"
EXPECTED_QUOTED_GMAIL_URL = (
    "https://mail.google.com/mail/u/0/?authuser=user%40example.com#all/m%201%22x"
)


def _add_message(db, account_id, *, subject, body, gmail_id=None, message_id=None):
    decoded = {
        "subject": subject,
        "sender": "Sender",
        "sender_email": "sender@example.com",
        "received_at": "2026-01-01T00:00:00",
        "body_preview": body,
        "gmail_labels": [],
        "truncated": False,
        "unsupported_content": False,
        "has_attachments": False,
    }
    if gmail_id is not None:
        decoded["gmail_message_id"] = gmail_id
        decoded["thread_id"] = "th-" + gmail_id
    else:
        decoded["message_id"] = message_id
    return db.upsert_message(account_id, decoded)


def _add_receipt_proposal(db, account_id, message_id):
    return db.insert_proposal(
        account_id,
        message_id,
        ["Type/Receipt"],
        "looks like a receipt",
        "model",
        "m1",
        "pv",
        "lv",
    )


def _select_item(page, message_id):
    page.wait_for_selector(".review-item")
    page.evaluate(
        """(messageId) => {
            const card = Array.from(document.querySelectorAll(".review-item"))
                .find((node) => node.dataset.id === messageId);
            if (!card) { throw new Error("review item not found: " + messageId); }
            card.click();
        }""",
        message_id,
    )


def _wait_for_detail(page, subject):
    page.wait_for_function(
        """(expected) => {
            const heading = document.querySelector("#review-detail h3");
            return !!heading && heading.textContent === expected;
        }""",
        arg=subject,
    )


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as playwright:
        try:
            launched = playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            message = (
                "could not launch headless Chromium; "
                "run `python -m playwright install chromium`: " + str(exc)
            )
            if REQUIRE_BROWSER:
                pytest.fail(message)
            pytest.skip(message)
        try:
            yield launched
        finally:
            launched.close()


@pytest.fixture
def page(browser):
    page_obj = browser.new_page()
    try:
        yield page_obj
    finally:
        page_obj.close()


@pytest.fixture
def review_server():
    temp_dir = tempfile.mkdtemp()
    try:
        config = config_module.Config(os.path.join(temp_dir, "config.json"))
        db = db_module.DB(config.database_path)
        try:
            account_id = db.get_or_create_account("user@example.com")
            ids = {}
            payload_items = []
            for index, payload in enumerate(PAYLOADS):
                message_id = _add_message(
                    db, account_id, subject=payload, body=payload, message_id=f"payload-{index}"
                )
                _add_receipt_proposal(db, account_id, message_id)
                payload_items.append((payload, message_id))
            ids["payloads"] = payload_items
            for key, gmail_id, subject in (
                ("gmail", GMAIL_ID, "from gmail"),
                ("gmail_quoted", QUOTED_GMAIL_ID, "quoted id"),
            ):
                message_id = _add_message(
                    db, account_id, subject=subject, body="body", gmail_id=gmail_id
                )
                _add_receipt_proposal(db, account_id, message_id)
                ids[key] = message_id
            ids["local"] = _add_message(
                db, account_id, subject="local mail", body="body", message_id="local-1"
            )
            _add_receipt_proposal(db, account_id, ids["local"])
        finally:
            db.close()

        app = app_module.create_app(config)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            yield base_url, app.config["CSRF_TOKEN"], ids
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_malicious_payloads_render_as_inert_text(review_server, page):
    base_url, _, ids = review_server
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog), dialog.dismiss()))
    page.goto(base_url + "/")

    for payload, message_id in ids["payloads"]:
        _select_item(page, message_id)
        _wait_for_detail(page, payload)
        preview = page.locator("#review-detail pre.body-preview")
        assert preview.evaluate("(node) => node.textContent") == payload
        assert preview.evaluate("(node) => node.children.length") == 0
        assert page.evaluate("() => window.__xss") is None
        assert (
            page.evaluate(
                "() => document.querySelectorAll('img, iframe, svg, object, embed').length"
            )
            == 0
        )
        assert page.evaluate("() => document.querySelectorAll('script').length") == 1

    assert dialogs == []


def test_zero_external_requests(review_server, page):
    base_url, _, ids = review_server
    requested_urls = []
    page.on("request", lambda request: requested_urls.append(request.url))
    page.goto(base_url + "/")
    _select_item(page, ids["gmail"])
    _wait_for_detail(page, "from gmail")
    page.wait_for_selector("#review-detail a.link-out")

    for url in requested_urls:
        host = urlparse(url).hostname
        if host is not None:
            assert host in LOOPBACK_HOSTS, url

    resource_urls = page.evaluate(
        "() => performance.getEntriesByType('resource').map((entry) => entry.name)"
    )
    for url in resource_urls:
        assert urlparse(url).hostname in LOOPBACK_HOSTS, url

    hrefs = page.evaluate(
        "() => Array.from(document.querySelectorAll('a[href]')).map((a) => a.getAttribute('href'))"
    )
    assert hrefs
    for href in hrefs:
        assert href.startswith("/") or href.startswith("https://mail.google.com/"), href


def test_gmail_link_format_in_rendered_dom(review_server, page):
    base_url, _, ids = review_server
    page.goto(base_url + "/")
    _select_item(page, ids["gmail"])
    _wait_for_detail(page, "from gmail")

    link = page.locator("#review-detail a.link-out")
    link.wait_for()
    assert link.get_attribute("href") == EXPECTED_GMAIL_URL
    assert link.get_attribute("target") == "_blank"
    rel = link.get_attribute("rel") or ""
    assert "noopener" in rel
    assert "noreferrer" in rel


def test_gmail_link_quoting_and_local_message(review_server, page):
    base_url, _, ids = review_server
    page.goto(base_url + "/")

    _select_item(page, ids["gmail_quoted"])
    _wait_for_detail(page, "quoted id")
    link = page.locator("#review-detail a.link-out")
    link.wait_for()
    assert link.get_attribute("href") == EXPECTED_QUOTED_GMAIL_URL

    _select_item(page, ids["local"])
    _wait_for_detail(page, "local mail")
    assert page.locator("#review-detail a.link-out").count() == 0


def test_security_headers_on_review_page(review_server, page):
    base_url, _, _ = review_server
    response = page.goto(base_url + "/")
    assert response is not None
    headers = response.all_headers()
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]


def _label_card(page, label_id):
    return page.locator(".label-card").filter(has_text=label_id).first


def test_label_management_renders_inert_text(review_server, page):
    base_url, _, _ = review_server
    requested_urls = []
    page.on("request", lambda request: requested_urls.append(request.url))
    page.goto(base_url + "/")

    page.click("#labels-toggle")
    page.wait_for_selector("#labels-panel:not([hidden])")

    payload = "<script>window.__xss=1</script>"
    page.fill("#label-id", "Type/Xss")
    page.fill("#label-name", payload)
    page.fill("#label-description", payload)
    page.fill("#label-examples", payload)
    page.click("#label-save")

    # The payload must land in a rendered card as inert text, and the form must
    # reset (a textarea match alone would not prove the card rendered).
    page.wait_for_function(
        """(payload) => Array.from(document.querySelectorAll(".label-card .label-name"))
            .some((node) => node.textContent === payload)""",
        arg=payload,
    )
    assert _label_card(page, "Type/Xss").locator(".label-name").text_content() == payload
    assert page.input_value("#label-name") == ""
    assert page.input_value("#label-id") == ""
    assert page.evaluate("() => window.__xss") is None
    assert (
        page.evaluate(
            "() => document.querySelectorAll("
            "'#labels-panel script, #labels-panel img, #labels-panel iframe, "
            "#labels-panel svg, #labels-panel object, #labels-panel embed').length"
        )
        == 0
    )

    # Edit round trip on a slash-containing id (encoded in the request URL).
    updated = '<img src=x onerror="window.__xss=2"> upgraded'
    _label_card(page, "Type/Xss").locator("button", has_text="Edit").click()
    assert page.input_value("#label-id") == "Type/Xss"
    page.fill("#label-description", updated)
    page.click("#label-save")
    page.wait_for_function(
        """(text) => Array.from(document.querySelectorAll(".label-card .label-desc"))
            .some((node) => node.textContent === text)""",
        arg=updated,
    )
    assert _label_card(page, "Type/Xss").locator(".label-desc").text_content() == updated
    assert page.input_value("#label-description") == ""
    assert page.evaluate("() => window.__xss") is None
    assert page.evaluate("() => document.querySelectorAll('.label-card img').length") == 0

    # Delete round trip, accepting the confirmation dialog.
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
    _label_card(page, "Type/Xss").locator("button", has_text="Delete").click()
    page.wait_for_function(
        """() => !Array.from(document.querySelectorAll(".label-card"))
            .some((card) => card.textContent.includes("Type/Xss"))"""
    )
    assert any("Type/Xss" in message for message in dialogs)
    assert page.evaluate("() => window.__xss") is None

    for url in requested_urls:
        host = urlparse(url).hostname
        if host is not None:
            assert host in LOOPBACK_HOSTS, url
