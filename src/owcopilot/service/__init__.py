"""HTTP service layer — the deployable surface of the copilot (audit Theme A).

`create_app()` builds the FastAPI app; `app` is the module-level instance uvicorn/gunicorn target
(`owcopilot.service.api:app`). The pipeline itself is reused from the engine-agnostic kernel; this
package only parses requests, calls the kernel, and serialises results.
"""

from .api import app, create_app

__all__ = ["app", "create_app"]
