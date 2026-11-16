"""The image carries every module the server imports (XERK-3017 QA).

CI never builds the multi-GB image, so a new module the server imports but the Dockerfile
does not COPY would only show as a crash-looping pod. Compare the two here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _local_imports(path: Path, seen: set[str]) -> set[str]:
    for node in ast.walk(ast.parse(path.read_text())):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module.split(".")[0]]
        for n in names:
            if (ROOT / f"{n}.py").exists() and n not in seen:
                seen.add(n)
                _local_imports(ROOT / f"{n}.py", seen)
    return seen


def test_the_dockerfile_copies_every_module_the_server_imports() -> None:
    copy_lines = [
        line for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("COPY ")
    ]
    copied = set(re.findall(r"diarizer/(\w+)\.py", "\n".join(copy_lines)))
    needed = _local_imports(ROOT / "server.py", {"server"})
    assert needed <= copied, f"Dockerfile lacks {sorted(needed - copied)}"
