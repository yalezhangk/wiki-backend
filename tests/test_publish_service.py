from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.publish_service import PublishService


class _Storage:
    pass


class PublishServiceTests(unittest.TestCase):
    def test_build_decodes_quartz_output_as_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            quartz_root = root / "quartz"
            (quartz_root / "quartz").mkdir(parents=True)
            (quartz_root / "quartz" / "bootstrap-cli.mjs").write_text("", encoding="utf-8")
            snapshot_dir = root / "snapshot"
            snapshot_dir.mkdir()
            release_dir = root / "release"
            service = PublishService(
                storage=_Storage(),  # type: ignore[arg-type]
                wiki_repo_path=root,
                quartz_repo_path=quartz_root,
                node_executable="node",
                build_timeout_seconds=900,
                debounce_seconds=120,
                max_delay_seconds=600,
                wiki_lock=threading.RLock(),
                start_worker=False,
            )

            with patch(
                "app.services.publish_service.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="完成", stderr=""),
            ) as run:
                service._build(snapshot_dir=snapshot_dir, release_dir=release_dir)

            self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
            self.assertEqual(run.call_args.kwargs["errors"], "replace")


if __name__ == "__main__":
    unittest.main()
