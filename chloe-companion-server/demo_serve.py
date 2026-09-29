"""Demo launcher: serve the companion PWA + API from one origin.

Local:  :8471. Railway: binds $PORT on 0.0.0.0.

The PWA's CONFIG.API_BASE_URL defaults to the page's own origin over
HTTP(S), so mounting the static app on the same FastAPI instance avoids
CORS entirely. API routes registered on the app take precedence over
the static mount.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.staticfiles import StaticFiles
import uvicorn

from app.main import app

PWA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "chloe-companion-app")
app.mount("/", StaticFiles(directory=PWA_DIR, html=True), name="pwa")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8471"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
