"""Firebase Cloud Messaging transport (optional external integration).

Lazily initialises the Firebase Admin SDK from a service-account JSON and sends
a single push to many device tokens. Excluded from the coverage gate (like the
other optional external integrations) because it only runs against a live
Firebase project the test suite never provisions; ``app.push_service`` mocks
``send()`` in its tests.

Nothing here breaks startup: ``firebase_admin`` is imported lazily inside the
functions, every failure is caught and logged, and a failed init disables push
for the process rather than raising.
"""
from __future__ import annotations

import logging
import threading

from app.config import get_settings

logger = logging.getLogger("bug_hunter.push")

# Module state without the `global` keyword: the Firebase app is created once
# and cached; a hard init failure latches so we don't retry every send.
_state: dict = {"app": None, "init_failed": False}
_init_lock = threading.Lock()

# FCM error class names that mean "this token is dead — stop sending to it".
_DEAD_TOKEN_ERRORS = frozenset({"UnregisteredError", "SenderIdMismatchError"})


def _ensure_app():
    """Return the cached firebase_admin app, initialising it once. None (never
    raises) if web push isn't configured or the SDK can't start."""
    if _state["app"] is not None:
        return _state["app"]
    if _state["init_failed"]:
        return None
    with _init_lock:
        if _state["app"] is not None:
            return _state["app"]
        if _state["init_failed"]:
            return None
        settings = get_settings()
        if not settings.FCM_CREDENTIALS_FILE:
            logger.warning("Web push enabled but FCM_CREDENTIALS_FILE is empty.")
            _state["init_failed"] = True
            return None
        try:
            import firebase_admin
            from firebase_admin import credentials
            cred = credentials.Certificate(settings.FCM_CREDENTIALS_FILE)
            _state["app"] = firebase_admin.initialize_app(cred, name="bug-hunter-push")
            logger.info("Firebase Admin initialised for web push.")
            return _state["app"]
        except Exception:  # noqa: BLE001 — any init failure just disables push
            logger.exception("Firebase Admin init failed; web push disabled this run.")
            _state["init_failed"] = True
            return None


def _is_dead_token(exc) -> bool:
    if exc is None:
        return False
    if type(exc).__name__ in _DEAD_TOKEN_ERRORS:
        return True
    return "not-registered" in str(exc).lower()


def _webpush_config(messaging, url: str):
    """A WebpushConfig carrying the click-through link, but only for an
    absolute https URL. FCM rejects any other link value and an encode-time
    error would abort the whole multicast; the link is also carried in
    data["url"], so omitting it here just drops a browser convenience."""
    if url.startswith("https://"):
        return messaging.WebpushConfig(
            fcm_options=messaging.WebpushFCMOptions(link=url),
        )
    return None


def send(tokens, *, title: str, body: str, url: str = "", data: dict | None = None) -> list[str]:
    """Send one notification to many FCM tokens.

    Returns the list of tokens FCM reported as invalid/unregistered, so the
    caller can prune them. Returns ``[]`` on any failure and never raises.
    """
    tokens = list(tokens or [])
    if not tokens:
        return []
    # Normalize url up front: a None slipping through would make the
    # url.startswith("https://") check below raise AttributeError, and that line
    # sits outside the try/except — breaking this function's documented
    # never-raises contract in a background push path.
    url = url or ""
    app = _ensure_app()
    if app is None:
        return []
    try:
        from firebase_admin import messaging
    except Exception:  # noqa: BLE001
        logger.exception("firebase_admin.messaging import failed")
        return []

    payload = {"url": url or "/"}
    if data:
        payload.update({k: str(v) for k, v in data.items()})
    # The deep link is carried in data["url"] (read by the Android app and the
    # web service worker); the webpush link below is a browser click-through
    # convenience, attached only for an https URL — see _webpush_config.
    message = messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(title=title, body=body),
        data=payload,
        webpush=_webpush_config(messaging, url),
    )
    try:
        resp = messaging.send_each_for_multicast(message, app=app)
    except Exception:  # noqa: BLE001
        logger.exception("FCM multicast send failed")
        return []

    if len(resp.responses) != len(tokens):  # pragma: no cover - FCM returns 1:1
        # Defensive: a partial/misaligned multicast response would silently drop
        # the unmatched tail under zip(). Surface it rather than swallow it.
        logger.warning(
            "FCM returned %d responses for %d tokens — response/token mismatch",
            len(resp.responses), len(tokens),
        )
    dead: list[str] = []
    for tok, result in zip(tokens, resp.responses):
        if result.success:
            continue
        if _is_dead_token(result.exception):
            dead.append(tok)
        else:
            logger.warning("FCM send to a token failed: %s", result.exception)
    return dead
