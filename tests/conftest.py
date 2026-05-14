"""Shared test fixtures and import mocking for modules that need Docker deps."""
import sys
from unittest.mock import MagicMock

# Mock dependencies not available outside Docker
for mod in ["jobspy", "fast_langdetect", "config"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()
