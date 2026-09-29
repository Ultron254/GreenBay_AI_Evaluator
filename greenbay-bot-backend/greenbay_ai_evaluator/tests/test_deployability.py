"""Guards for things that only break at container start, not at import time.

The unit suite never imports ``app.main`` (the deploy gate has no uvicorn), so
nothing here exercised engine creation. A clean rebuild then dropped a package
that a cached Docker layer had been silently providing, and the container would
not boot. These assertions are cheap and cover that blind spot.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
REQUIREMENTS = BACKEND / "requirements.txt"


@pytest.fixture(scope="module")
def requirements() -> list[str]:
    lines = REQUIREMENTS.read_text(encoding="utf-8", errors="replace").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _declares(requirements: list[str], package: str) -> bool:
    """Whether *package* is declared, ignoring extras and version pins."""
    for line in requirements:
        name = re.split(r"[<>=!\[;\s]", line, 1)[0].strip().lower()
        if name == package.lower():
            return True
    return False


class TestDatabaseDriversAreDeclared:
    """SQLAlchemy picks its DBAPI from the URL scheme at runtime.

    ``postgresql://`` and ``postgresql+psycopg2://`` need psycopg2, while
    ``postgresql+psycopg://`` needs psycopg 3. Nothing in our code imports
    either directly, so a missing driver is invisible until create_engine()
    runs inside the container. Production once served 502s for exactly this.
    """

    def test_psycopg2_declared(self, requirements: list[str]):
        assert _declares(requirements, "psycopg2-binary") or _declares(requirements, "psycopg2")

    def test_psycopg3_declared(self, requirements: list[str]):
        assert _declares(requirements, "psycopg"), (
            "the server's DATABASE_URL uses postgresql+psycopg://, which needs psycopg 3"
        )

    def test_both_are_present_together(self, requirements: list[str]):
        # Either scheme must work, because the URL lives in the server's .env
        # and is not visible from here.
        assert _declares(requirements, "psycopg")
        assert _declares(requirements, "psycopg2-binary") or _declares(requirements, "psycopg2")


class TestRuntimeEntrypointDependencies:
    """Packages app/main.py imports at module scope must be installable."""

    @pytest.mark.parametrize("package", ["uvicorn", "fastapi", "sqlalchemy", "alembic", "loguru"])
    def test_declared(self, requirements: list[str], package: str):
        assert _declares(requirements, package), f"{package} is imported at start-up"


class TestNoDuplicateRequirements:
    """Two pins for one package make the installed version order-dependent."""

    def test_no_package_declared_twice(self, requirements: list[str]):
        seen: dict[str, int] = {}
        for line in requirements:
            name = re.split(r"[<>=!\[;\s]", line, 1)[0].strip().lower()
            if name:
                seen[name] = seen.get(name, 0) + 1
        dupes = {k: v for k, v in seen.items() if v > 1}
        assert not dupes, f"duplicate requirements: {dupes}"
