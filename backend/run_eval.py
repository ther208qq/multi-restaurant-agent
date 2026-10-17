"""Uvicorn server runner for the isolated evaluation database.

The public-dataset import (``scripts/import_public_sales_dataset.py``) creates
``restaurant_ai_eval`` and its ``eval_admin`` / ``eval_staff`` accounts. The
development server (``backend/run.py``) keeps using ``restaurant_ai``, so the
evaluation accounts only work while the API runs against the evaluation
database. That is what this runner does: it derives the evaluation URL from
``DATABASE_URL`` and starts the app on the usual port.

Usage:
    .venv/Scripts/python.exe backend/run_eval.py
"""

import os
import sys

import uvicorn
from sqlalchemy.engine import make_url

# Running ``python backend/run_eval.py`` puts ``backend/`` on sys.path rather
# than the project root; add the root so ``backend.app`` imports work.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.environ["PYTHONPATH"] = os.pathsep.join(
    [PROJECT_ROOT] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
)

from backend.app.config import EVAL_DATABASE_NAME, settings  # noqa: E402

# uvicorn's reload subprocess re-imports ``backend.app.config`` from the
# environment, so the override must be set before the server starts.
os.environ["DATABASE_URL"] = (
    make_url(settings.DATABASE_URL)
    .set(database=EVAL_DATABASE_NAME)
    .render_as_string(hide_password=False)
)

if __name__ == "__main__":
    uvicorn.run(
        "backend.app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=True,
    )
