from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.schemas.publish import PublishJobResponse
from app.services.publish_service import PublishService


class _Storage:
    def __init__(self) -> None:
        self.succeeded_job_ids: list[int] = []
        self.failed_job_ids: list[int] = []

    def mark_publish_job_succeeded(
        self, *, job_id: int, release_id: str, finished_at: datetime
    ) -> None:
        self.succeeded_job_ids.append(job_id)

    def mark_publish_job_failed(self, *, job_id: int, error: str, finished_at: datetime) -> None:
        self.failed_job_ids.append(job_id)


class PublishServiceTests(unittest.TestCase):
    @staticmethod
    def _job(job_id: int) -> PublishJobResponse:
        now = datetime(2026, 7, 27, 9)
        return PublishJobResponse(
            job_id=job_id,
            status="running",
            trigger="automatic",
            change_count=1,
            scheduled_at=now,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _create_release(releases: Path, name: str, modified_at: float) -> Path:
        release = releases / name
        release.mkdir(parents=True)
        marker = release / "marker"
        marker.write_text(name, encoding="utf-8")
        os.utime(release, (modified_at, modified_at))
        return release

    @staticmethod
    def _service(root: Path, storage: _Storage) -> PublishService:
        quartz_root = root / "quartz"
        (quartz_root / "quartz").mkdir(parents=True)
        (quartz_root / "quartz" / "bootstrap-cli.mjs").write_text("", encoding="utf-8")
        (root / "wiki").mkdir()
        return PublishService(
            storage=storage,
            wiki_repo_path=root,
            quartz_repo_path=quartz_root,
            node_executable="node",
            build_timeout_seconds=900,
            debounce_seconds=120,
            max_delay_seconds=600,
            wiki_lock=threading.RLock(),
            start_worker=False,
        )

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
            self.assertEqual(run.call_args.kwargs["env"]["WIKI_SOURCE_ROOT"], str(root.resolve()))

    def test_successful_publish_cleans_temporary_snapshot_and_keeps_three_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            storage = _Storage()
            service = self._service(root, storage)
            releases = root / "quartz" / ".publish" / "releases"
            snapshot_root = root / "temporary-snapshot-success"
            for index, name in enumerate(("old-1", "old-2", "old-3"), start=1):
                self._create_release(releases, name, float(index))

            def create_release(*, snapshot_dir: Path, release_dir: Path) -> None:
                release_dir.mkdir()

            with (
                patch(
                    "app.services.publish_service.tempfile.mkdtemp",
                    return_value=str(snapshot_root),
                ),
                patch.object(service, "_build", side_effect=create_release),
                patch.object(service, "_validate_release"),
            ):
                service._run_job(self._job(1))

            self.assertEqual(storage.succeeded_job_ids, [1])
            self.assertFalse(snapshot_root.exists())
            self.assertFalse((root / "quartz" / ".publish" / "work").exists())
            self.assertTrue((root / "quartz" / "public").is_symlink())
            self.assertEqual(
                sorted(entry.name for entry in releases.iterdir()),
                ["1", "old-2", "old-3"],
            )

    def test_windows_activation_replaces_existing_public_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            service = self._service(root, _Storage())
            releases = root / "quartz" / ".publish" / "releases"
            previous_release = self._create_release(releases, "previous", 1.0)
            next_release = self._create_release(releases, "next", 2.0)
            public = root / "quartz" / "public"
            temporary = root / "quartz" / ".publish" / "public.next"
            public.symlink_to(previous_release, target_is_directory=True)
            temporary.symlink_to(next_release, target_is_directory=True)

            with patch("app.services.publish_service.os.name", "nt"):
                service._replace_public_link(temporary=temporary, public=public)

            self.assertFalse(temporary.exists())
            self.assertTrue(public.is_symlink())
            self.assertEqual(public.resolve(), next_release)

    def test_failed_publish_cleans_temporary_snapshot_and_caps_failed_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            storage = _Storage()
            service = self._service(root, storage)
            releases = root / "quartz" / ".publish" / "releases"
            snapshot_root = root / "temporary-snapshot-failure"
            for index, name in enumerate(("old-1", "old-2", "old-3"), start=1):
                self._create_release(releases, name, float(index))

            def fail_after_creating_release(*, snapshot_dir: Path, release_dir: Path) -> None:
                release_dir.mkdir()
                raise RuntimeError("build failed")

            with (
                patch(
                    "app.services.publish_service.tempfile.mkdtemp",
                    return_value=str(snapshot_root),
                ),
                patch.object(service, "_build", side_effect=fail_after_creating_release),
            ):
                service._run_job(self._job(2))

            self.assertEqual(storage.failed_job_ids, [2])
            self.assertFalse(snapshot_root.exists())
            self.assertFalse((root / "quartz" / ".publish" / "work").exists())
            self.assertEqual(
                sorted(entry.name for entry in releases.iterdir()),
                ["2", "old-2", "old-3"],
            )


if __name__ == "__main__":
    unittest.main()
