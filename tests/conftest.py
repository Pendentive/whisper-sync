"""Pytest hook point: apply the isolation seam before collection.

The actual logic lives in tests/__init__.py so the unittest-run venv
suite gets it too; importing the package here guarantees it runs at
pytest startup regardless of import mode.
"""

import tests  # noqa: F401  (import applies WS_LOG_DIR / WS_DATA_DIR)
