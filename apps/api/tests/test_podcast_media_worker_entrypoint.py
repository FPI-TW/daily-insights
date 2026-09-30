import subprocess
import sys


def test_media_worker_entrypoint_registers_foreign_key_tables() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import daily_insights_api.scripts.run_podcast_media_worker; "
            "from daily_insights_api.core.models import Base; "
            "Base.metadata.sorted_tables",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
