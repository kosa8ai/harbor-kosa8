"""Against kosa8 Cloud itself. Skipped unless KOSA8_API_KEY is set.

Creates one sandbox on the `python` template's own image and deletes it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from harbor_kosa8 import Kosa8Environment

pytestmark = pytest.mark.skipif(not os.environ.get("KOSA8_API_KEY"), reason="KOSA8_API_KEY is not set")


async def test_live_roundtrip(tmp_path: Path) -> None:
    (tmp_path / "environment").mkdir()
    env = Kosa8Environment(
        environment_dir=tmp_path / "environment",
        environment_name="live",
        session_id="live__k8__env",
        trial_paths=TrialPaths(trial_dir=tmp_path / "trial"),
        task_env_config=EnvironmentConfig(workdir="/app"),
        use_task_image=False,
        ttl_s=1800,
    )
    await env.start(force_build=False)
    try:
        r = await env.exec("echo hello > note.txt && pwd && cat note.txt")
        assert r.return_code == 0
        assert r.stdout.splitlines() == ["/app", "hello"]
        r = await env.exec("cat /app/note.txt")  # a new container, same disk
        assert r.stdout.strip() == "hello"
        src = tmp_path / "in.bin"
        src.write_bytes(os.urandom(100_000))
        await env.upload_file(src, "/logs/agent/in.bin")
        await env.download_file("/logs/agent/in.bin", tmp_path / "out.bin")
        assert (tmp_path / "out.bin").read_bytes() == src.read_bytes()
    finally:
        await env.stop(delete=True)
