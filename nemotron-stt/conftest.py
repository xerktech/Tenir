"""Put this directory on sys.path so the tests can `import server`.

The STT server is a single module run by uvicorn from its own workdir, not an
installed package, so there's nothing for pip to make importable. A conftest at the
component root is the standard way to say that to pytest, and it works however the
runner is invoked (`pytest`, `pytest tests/`, from any cwd) — unlike relying on
`python -m pytest` happening to prepend the working directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
