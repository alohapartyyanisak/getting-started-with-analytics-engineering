from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix=f".{path.name}.", delete=False) as handle:
        tmp_path = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    tmp_path.replace(path)


def write_json_atomic(path: Path, payload: Any) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    write_bytes_atomic(path, encoded)
