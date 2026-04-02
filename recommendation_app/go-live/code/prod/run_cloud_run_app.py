from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from prod.langsmith_app_tracing import emit_app_boot_trace
from prod.sync_storage_from_gcs import main as sync_storage_main


def main() -> int:
    sync_rc = sync_storage_main()
    if sync_rc != 0:
        return int(sync_rc)

    emit_app_boot_trace()

    port = str(os.getenv("PORT", "8080") or "8080").strip() or "8080"
    app_entrypoint = (
        str(
            os.getenv(
                "GO_LIVE_APP_ENTRYPOINT",
                "recommendation_app/go-live/code/application_phase2.py",
            )
        ).strip()
        or "recommendation_app/go-live/code/application_phase2.py"
    )
    cmd = [
        "streamlit",
        "run",
        app_entrypoint,
        "--server.address=0.0.0.0",
        f"--server.port={port}",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
        "--server.fileWatcherType=none",
        "--theme.base=dark",
        "--theme.primaryColor=#d4af37",
        "--theme.backgroundColor=#070707",
        "--theme.secondaryBackgroundColor=#111111",
        "--theme.textColor=#f5f5f5",
    ]
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
