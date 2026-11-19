"""P0 smoke test: every planned package imports and says what it is for."""
import importlib

import pytest

PACKAGES = [
    "taskforge",
    "taskforge.core",
    "taskforge.core.engine",
    "taskforge.core.sandbox",
    "taskforge.core.agent",
    "taskforge.core.gateway",
    "taskforge.core.store",
    "taskforge.core.tools",
    "taskforge.core.kb",
    "taskforge.core.review",
    "taskforge.core.obs",
    "taskforge.taskgen",
    "taskforge.taskgen.agents",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports_and_is_described(name):
    module = importlib.import_module(name)
    assert module.__doc__ and module.__doc__.strip()
