"""Chloe — AI Companion backend (Phase 2 scaffold).

Scaffold only: working memory/bank/scheduling logic, stubbed LLM/voice/payments.
Nothing here touches production (keyhole-site or the live backend).
"""

# Proactive messaging wiring (2026-10-16, feat/proactive-live).
# The scheduler starts immediately (it only needs the DB). The six API
# routes are registered once main.app exists — main.py imports from this
# package, so a short deferred wait covers it without touching main.py.
try:
    from . import proactive_scheduler
    proactive_scheduler.start_scheduler()
except Exception:
    pass


def _register_proactive_routes_when_ready() -> None:
    import time
    for _ in range(100):  # ~10s
        try:
            from . import main as _main
            _app = getattr(_main, "app", None)
            _uid = getattr(_main, "_current_user_id", None)
            _conn = getattr(_main, "_conn", None)
            if _app is not None and _uid is not None and _conn is not None:
                from .proactive_api import register_proactive_routes
                register_proactive_routes(_app, _uid, _conn)
                return
        except (ImportError, AttributeError):
            pass
        time.sleep(0.1)


try:
    import threading
    threading.Thread(target=_register_proactive_routes_when_ready,
                     name="proactive-wire", daemon=True).start()
except Exception:
    pass
