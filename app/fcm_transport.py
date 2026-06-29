"""Firebase Cloud Messaging transport (optional external integration).

Lazily initialises the Firebase Admin SDK from a service-account JSON and
sends a notification to many device tokens. Excluded from the coverage gate
because it requires a live Firebase project; ``app.push_service`` mocks
``send()`` in tests.

``firebase_admin`` is imported lazily inside each function, every failure is
caught and logged, and a failed init disables push for the process lifetime
rather than raising.
"""
from __future__ import annotations

import logging
import threading

from app.config import get_settings

logger = logging.getLogger("bug_hunter.push")

# Firebase app created once and cached. A hard init failure latches the flag
# so we skip retrying on every subsequent send call.
_state: dict = {"app": None, "init_failed": False}
_init_lock = threading.Lock()

# FCM error class names that indicate a permanently invalid token.
_DEAD_TOKEN_ERRORS = frozenset({"UnregisteredError", "SenderIdMismatchError"})


def _ensure_app():
    """Return the cached Firebase app, initialising it on first call.

    Returns None (never raises) if push is not configured or the SDK fails to start.
    """
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
    """Build a WebpushConfig with a click-through link, but only for https URLs.

    FCM rejects non-https link values and an encode-time error would abort the
    entire multicast. The URL is also in data["url"], so skipping the link here
    only drops a browser convenience.
    """
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
    # Normalize url before use: a None would cause url.startswith("https://") to
    # raise AttributeError outside the try/except, breaking the never-raises contract.
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
    # data["url"] carries the deep link for the Android app and the web service
    # worker. The webpush link is a browser click-through convenience only — see
    # _webpush_config for why it is conditional on https.
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
        # A misaligned response would silently drop unmatched tokens under zip().
        # Log the mismatch so it surfaces rather than being swallowed.
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
