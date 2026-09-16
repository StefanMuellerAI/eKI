"""Single source of truth for the eKI API version.

Every place that reports a version (FastAPI app metadata, ``/health``,
``HealthResponse`` default, root endpoint, OpenAPI info block) imports
``__version__`` from here. ``pyproject.toml`` reads it dynamically via
``[tool.setuptools.dynamic]`` so the package metadata stays in sync.

Bump policy (Pflichtenheft §9 Roadmap): one minor bump per milestone
(M08 -> 0.8.0, M09 -> 0.9.0, ...), ``1.0.0`` with the M12 UAT package.
"""

__version__ = "0.9.0"

__all__ = ["__version__"]
