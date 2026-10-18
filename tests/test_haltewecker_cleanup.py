import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CLEANUP_SCRIPT = REPOSITORY_ROOT / "ops" / "haltewecker-cleanup.sh"


def write_gtfs(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name in ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
            archive.writestr(name, "marker,fixture\n")


class HalteWeckerCleanupTests(unittest.TestCase):
    def run_cleaner(
        self, root: Path, data_root: Path, dry_run: bool = True,
        open_paths: tuple[Path, ...] = (),
        retention_count: int = 1,
        pipeline_repo: Path | None = None,
        docker_fixture: dict[str, object] | None = None,
        missing_locks: tuple[str, ...] = (),
        read_only_locks: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess[str]:
        systemctl = root / "systemctl"
        systemctl.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        systemctl.chmod(0o755)
        flock = root / "flock"
        flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        flock.chmod(0o755)
        lsof = root / "lsof"
        lsof.write_text(
            "#!/usr/bin/env python3\nimport sys\n"
            f"paths = {tuple(str(path.resolve()) for path in open_paths)!r}\n"
            "if '-Fn' in sys.argv:\n"
            "    for path in paths: print('n' + path)\n"
            "    raise SystemExit(0 if paths else 1)\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        lsof.chmod(0o755)
        # Emulate GNU readlink consistently on macOS and Linux.
        readlink = root / "readlink"
        readlink.write_text(
            "#!/usr/bin/env python3\nimport os, sys\n"
            "path = sys.argv[-1]\n"
            "if '-m' in sys.argv or '-f' in sys.argv:\n"
            "    resolved = os.path.realpath(path)\n"
            "    if '-f' in sys.argv and not os.path.isdir(os.path.dirname(resolved)):\n"
            "        raise SystemExit(1)\n"
            "    print(resolved)\n"
            "else:\n"
            "    print(os.readlink(path))\n",
            encoding="utf-8",
        )
        readlink.chmod(0o755)
        stat = root / "stat"
        stat.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "if len(sys.argv) == 5 and sys.argv[1:4] == ['-c', '%Y', '--']:\n"
            "    path = sys.argv[4]\n"
            "    print(int(os.stat(path).st_mtime))\n"
            "else:\n"
            "    raise SystemExit(2)\n",
            encoding="utf-8",
        )
        stat.chmod(0o755)
        du = root / "du"
        du.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "path = sys.argv[-1]\n"
            "if os.path.isfile(path):\n"
            "    size = os.path.getsize(path)\n"
            "else:\n"
            "    size = sum(os.path.getsize(os.path.join(base, name)) for base, _, files in os.walk(path) for name in files)\n"
            "print(f'{size}\\t{path}')\n",
            encoding="utf-8",
        )
        du.chmod(0o755)
        docker_fixture_path = root / "docker-fixture.json"
        docker_removed_path = root / "docker-removed.json"
        if docker_fixture is None:
            docker_fixture = {
                "health": {},
                "containers": [],
                "releaseID": "",
                "staticReady": False,
                "routeReadonly": False,
            }
        docker_fixture_path.write_text(json.dumps(docker_fixture), encoding="utf-8")
        if not docker_removed_path.exists():
            docker_removed_path.write_text("[]\n", encoding="utf-8")
        docker_removed_images_path = root / "docker-removed-images.json"
        if not docker_removed_images_path.exists():
            docker_removed_images_path.write_text("[]\n", encoding="utf-8")
        docker = root / "docker"
        docker.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, subprocess, sys\n"
            "from pathlib import Path\n"
            "fixture = json.loads(Path(os.environ['DOCKER_FIXTURE']).read_text())\n"
            "args = sys.argv[1:]\n"
            "removed_path = Path(os.environ['DOCKER_REMOVED'])\n"
            "removed = json.loads(removed_path.read_text())\n"
            "removed_images_path = Path(os.environ['DOCKER_REMOVED_IMAGES'])\n"
            "removed_images = json.loads(removed_images_path.read_text())\n"
            "if args and args[0] == 'inspect':\n"
            "    template = args[args.index('--format') + 1] if '--format' in args else ''\n"
            "    target = args[-1]\n"
            "    if template == '{{.State.Health.Status}}':\n"
            "        health = fixture.get('health', {})\n"
            "        if target not in health: raise SystemExit(1)\n"
            "        print(health[target]); raise SystemExit(0)\n"
            "    if template == '{{.Image}}':\n"
            "        image = fixture.get('activeImages', {}).get(target)\n"
            "        if image is None: raise SystemExit(1)\n"
            "        print(image); raise SystemExit(0)\n"
            "    row = next((item for item in fixture.get('containers', []) if item['id'] == target), None)\n"
            "    if row is None: raise SystemExit(1)\n"
            "    if '.SizeRw' in template:\n"
            "        print('/' + row['name'] + '|' + row['status'] + '|' + row['image'] + '|' + str(row.get('writableBytes', 0)))\n"
            "    else: raise SystemExit(2)\n"
            "elif args and args[0] == 'exec':\n"
            "    name = args[1]\n"
            "    if name == 'static-departures-api' and fixture.get('staticReady'):\n"
            "        database = {'releaseID': fixture['releaseID']}\n"
            "        for key in ('runtimeMode', 'fallbackReleaseID'):\n"
            "            if key in fixture: database[key] = fixture[key]\n"
            "        payload = json.dumps({'database': database})\n"
            "        environment = os.environ.copy()\n"
            "        environment['DEPARTURES_DATABASE'] = fixture.get('departuresDatabase', '/data/current-release/departures.sqlite')\n"
            "        environment['DOCKER_HEALTH_PAYLOAD'] = payload\n"
            "        health_check = args[args.index('-c') + 1]\n"
            "        harness = ('import io, os, sys, urllib.request\\n'\n"
            "                   'class Response(io.BytesIO):\\n    status = 200\\n'\n"
            "                   'urllib.request.urlopen = lambda *a, **k: Response(os.environ[\"DOCKER_HEALTH_PAYLOAD\"].encode())\\n'\n"
            "                   'exec(compile(sys.argv[1], \"<docker-health-check>\", \"exec\"), {})\\n')\n"
            "        checked = subprocess.run([sys.executable, '-c', harness, health_check], env=environment, capture_output=True, text=True)\n"
            "        if checked.stdout: print(checked.stdout, end='')\n"
            "        if checked.stderr: print(checked.stderr, end='', file=sys.stderr)\n"
            "        raise SystemExit(checked.returncode)\n"
            "    if name == 'routerecall-api' and fixture.get('routeReadonly'):\n"
            "        raise SystemExit(0)\n"
            "    raise SystemExit(1)\n"
            "elif args and args[0] == 'ps':\n"
            "    ancestor = next((arg.split('=', 1)[1] for arg in args if arg.startswith('ancestor=')), None)\n"
            "    for row in fixture.get('containers', []):\n"
            "        if row['id'] in removed: continue\n"
            "        if ancestor is not None and row['image'] != ancestor: continue\n"
            "        print(row['id'] + ' ' + row['name'])\n"
            "elif args and args[0] == 'rm' and len(args) == 2:\n"
            "    removed.append(args[1]); removed_path.write_text(json.dumps(removed))\n"
            "elif args and args[0] == 'image' and args[1] == 'ls':\n"
            "    for image_id, image in fixture.get('images', {}).items():\n"
            "        for tag in image['tags']:\n"
            "            if tag not in removed_images: print(image_id[:12] + ' ' + tag)\n"
            "elif args and args[0] == 'image' and args[1] == 'inspect':\n"
            "    template = args[args.index('--format') + 1]\n"
            "    target = args[-1]\n"
            "    image_id = next((key for key, value in fixture.get('images', {}).items() if key == target or key.startswith(target) or target in value['tags']), None)\n"
            "    image = fixture.get('images', {}).get(image_id)\n"
            "    if image is None: raise SystemExit(1)\n"
            "    tags = [tag for tag in image['tags'] if tag not in removed_images]\n"
            "    if template == '{{json .RepoTags}}': print(json.dumps(tags)); raise SystemExit(0)\n"
            "    if template == '{{.Id}}': print(image_id); raise SystemExit(0)\n"
            "    raise SystemExit(2)\n"
            "elif args and args[0] == 'image' and args[1] == 'rm' and len(args) >= 3:\n"
            "    removed_images.extend(args[2:]); removed_images_path.write_text(json.dumps(removed_images))\n"
            "else: raise SystemExit(2)\n",
            encoding="utf-8",
        )
        docker.chmod(0o755)
        environment = os.environ.copy()
        environment.update(
            {
                "DATA_ROOT": str(data_root),
                "GTFS_CACHE_ROOT": str(root / "cache" / "gtfs"),
                "HALTEWECKER_PIPELINE_REPO": str(pipeline_repo or REPOSITORY_ROOT),
                "HALTEWECKER_CLEANUP_LOCKS": f"{root / 'stop.lock'}:{root / 'static.lock'}:{root / 'vbb.lock'}",
                "SYSTEMCTL_BIN": str(systemctl),
                "FLOCK_BIN": str(flock),
                "LSOF_BIN": str(lsof),
                "DOCKER_BIN": str(docker),
                "DOCKER_FIXTURE": str(docker_fixture_path),
                "DOCKER_REMOVED": str(docker_removed_path),
                "DOCKER_REMOVED_IMAGES": str(docker_removed_images_path),
                "PATH": f"{root}:{environment.get('PATH', '')}",
                "HALTEWECKER_CLEANUP_DRY_RUN": "1" if dry_run else "0",
                "HALTEWECKER_RELEASE_RETENTION_COUNT": str(retention_count),
            }
        )
        for lock in (root / "stop.lock", root / "static.lock", root / "vbb.lock"):
            if lock.name not in missing_locks:
                lock.touch()
                if lock.name in read_only_locks:
                    lock.chmod(0o444)
        return subprocess.run(
            [str(CLEANUP_SCRIPT)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    @staticmethod
    def healthy_docker_fixture() -> dict[str, object]:
        return {
            "health": {
                "static-departures-api": "healthy",
                "routerecall-api": "healthy",
            },
            "releaseID": "active-release",
            "staticReady": True,
            "routeReadonly": True,
            "activeImages": {
                "static-departures-api": "sha256:" + "a" * 64,
                "routerecall-api": "sha256:" + "b" * 64,
            },
            "images": {},
            "containers": [
                {
                    "id": "active-static",
                    "name": "static-departures-api",
                    "status": "running",
                    "image": "sha256:" + "a" * 64,
                },
                {
                    "id": "active-route",
                    "name": "routerecall-api",
                    "status": "running",
                    "image": "sha256:" + "b" * 64,
                },
                {
                    "id": "old-static",
                    "name": "static-departures-api-rollback-old-release",
                    "status": "exited",
                    "image": "sha256:" + "c" * 64,
                    "writableBytes": 8192,
                },
                {
                    "id": "old-route",
                    "name": "routerecall-api-rollback-old-release",
                    "status": "exited",
                    "image": "sha256:" + "d" * 64,
                    "writableBytes": 16384,
                },
                {
                    "id": "running-rollback",
                    "name": "static-departures-api-rollback-in-progress",
                    "status": "running",
                    "image": "sha256:" + "e" * 64,
                },
                {
                    "id": "unrelated",
                    "name": "caddy",
                    "status": "exited",
                    "image": "sha256:" + "f" * 64,
                },
            ],
        }

    @staticmethod
    def make_old(path: Path, age_days: int = 2) -> None:
        old_time = time.time() - age_days * 24 * 60 * 60
        os.utime(path, (old_time, old_time))

    @staticmethod
    def make_age_hours(path: Path, age_hours: float) -> None:
        old_time = time.time() - age_hours * 60 * 60
        os.utime(path, (old_time, old_time))

    @staticmethod
    def make_incremental_release(path: Path, release_id: str) -> None:
        path.mkdir(parents=True)
        (path / "stop-data").mkdir()
        (path / "release.json").write_text(json.dumps({"releaseID": release_id}), encoding="utf-8")
        (path / "validation-receipt.json").write_text(
            json.dumps({"releaseID": release_id, "result": "PASS"}), encoding="utf-8"
        )

    def test_cleanup_and_pipeline_share_the_canonical_default_root(self) -> None:
        cleanup_source = CLEANUP_SCRIPT.read_text(encoding="utf-8")
        pipeline_source = (REPOSITORY_ROOT / "scripts" / "run_stop_data_pipeline.sh").read_text(encoding="utf-8")

        self.assertIn('GTFS_CACHE_ROOT="${GTFS_CACHE_ROOT:-$DATA/cache/gtfs}"', cleanup_source)
        self.assertIn('CACHE_ROOT="${GTFS_CACHE_ROOT:-$DATA_ROOT/cache/gtfs}"', pipeline_source)

    def test_published_artifact_retention_is_wired_to_locked_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / 'data'
            active = data / 'releases/active'
            active.mkdir(parents=True)
            (data / 'current-release').symlink_to(active)
            result = self.run_cleaner(root, data)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn('[ArtifactRetention] summary=', result.stdout)
            self.assertIn('"dry_run":true', result.stdout)

    def test_missing_vbb_lock_is_created_without_failing_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            active = data / "releases" / "active"
            active.mkdir(parents=True)
            (data / "current-release").symlink_to(active)

            result = self.run_cleaner(
                root,
                data,
                missing_locks=("vbb.lock",),
                read_only_locks=("stop.lock", "static.lock"),
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue((root / "vbb.lock").is_file())
            self.assertNotIn("pipeline-lock-held", result.stdout)

    def test_dry_run_lists_only_stopped_pipeline_rollback_containers_after_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            active = data_root / "releases" / "active-release"
            active.mkdir(parents=True)
            (active / "release-metadata.json").write_text(
                '{"releaseID":"active-release"}\n', encoding="utf-8"
            )
            (data_root / "current-release").symlink_to(active)

            result = self.run_cleaner(
                root,
                data_root,
                docker_fixture=self.healthy_docker_fixture(),
            )
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn(
                "WOULD_DELETE_CONTAINER static-departures-api-rollback-old-release",
                output,
            )
            self.assertIn(
                "WOULD_DELETE_CONTAINER routerecall-api-rollback-old-release",
                output,
            )
            self.assertNotIn("WOULD_DELETE_CONTAINER static-departures-api-rollback-in-progress", output)
            self.assertNotIn("WOULD_DELETE_CONTAINER caddy", output)
            self.assertEqual(json.loads((root / "docker-removed.json").read_text()), [])
            self.assertTrue(active.exists())

    def test_apply_removes_only_stopped_pipeline_rollback_containers_without_volumes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            active = data_root / "releases" / "active-release"
            active.mkdir(parents=True)
            (active / "release-metadata.json").write_text(
                '{"releaseID":"active-release"}\n', encoding="utf-8"
            )
            (data_root / "current-release").symlink_to(active)
            fixture = self.healthy_docker_fixture()

            first = self.run_cleaner(root, data_root, dry_run=False, docker_fixture=fixture)
            output = first.stdout + first.stderr
            self.assertEqual(first.returncode, 0, output)
            self.assertIn(
                "DELETE_CONTAINER static-departures-api-rollback-old-release",
                output,
            )
            self.assertIn(
                "DELETE_CONTAINER routerecall-api-rollback-old-release",
                output,
            )
            self.assertEqual(
                json.loads((root / "docker-removed.json").read_text()),
                ["old-static", "old-route"],
            )

            second = self.run_cleaner(root, data_root, dry_run=False, docker_fixture=fixture)
            second_output = second.stdout + second.stderr
            self.assertEqual(second.returncode, 0, second_output)
            self.assertIn("Docker rollback-container cleanup status=PASS removed=0", second_output)
            self.assertEqual(
                json.loads((root / "docker-removed.json").read_text()),
                ["old-static", "old-route"],
            )

    def test_rollback_containers_are_kept_when_route_recall_is_not_read_only_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            active = data_root / "releases" / "active-release"
            active.mkdir(parents=True)
            (active / "release-metadata.json").write_text(
                '{"releaseID":"active-release"}\n', encoding="utf-8"
            )
            (data_root / "current-release").symlink_to(active)
            fixture = self.healthy_docker_fixture()
            fixture["routeReadonly"] = False

            result = self.run_cleaner(root, data_root, docker_fixture=fixture)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn(
                "Docker rollback-container cleanup skipped reason=production-readiness-check-failed",
                output,
            )
            self.assertNotIn("WOULD_DELETE_CONTAINER", output)
            self.assertEqual(json.loads((root / "docker-removed.json").read_text()), [])

    def test_explicit_non_legacy_runtime_mode_blocks_rollback_container_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            active = data_root / "releases" / "active-release"
            active.mkdir(parents=True)
            (active / "release-metadata.json").write_text(
                '{"releaseID":"active-release"}\n', encoding="utf-8"
            )
            (data_root / "current-release").symlink_to(active)
            fixture = self.healthy_docker_fixture()
            fixture["runtimeMode"] = "provider-only"

            result = self.run_cleaner(root, data_root, docker_fixture=fixture)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn(
                "Docker rollback-container cleanup skipped reason=production-readiness-check-failed",
                output,
            )
            self.assertNotIn("WOULD_DELETE_CONTAINER", output)

    def test_fallback_release_blocks_rollback_container_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            active = data_root / "releases" / "active-release"
            active.mkdir(parents=True)
            (active / "release-metadata.json").write_text(
                '{"releaseID":"active-release"}\n', encoding="utf-8"
            )
            (data_root / "current-release").symlink_to(active)
            fixture = self.healthy_docker_fixture()
            fixture["fallbackReleaseID"] = "fallback-release"

            result = self.run_cleaner(root, data_root, docker_fixture=fixture)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn(
                "Docker rollback-container cleanup skipped reason=production-readiness-check-failed",
                output,
            )
            self.assertNotIn("WOULD_DELETE_CONTAINER", output)

    def test_verified_rollback_container_and_temporary_image_are_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            active = data_root / "releases" / "active-release"
            active.mkdir(parents=True)
            (active / "release-metadata.json").write_text(
                '{"releaseID":"active-release"}\n', encoding="utf-8"
            )
            (data_root / "current-release").symlink_to(active)
            fixture = self.healthy_docker_fixture()
            image = "sha256:" + "7" * 64
            fixture["containers"].append(
                {
                    "id": "verified-rollback",
                    "name": "static-departures-api-verified-rollback-21ad15d",
                    "status": "exited",
                    "image": image,
                    "writableBytes": 12288,
                }
            )
            fixture["images"] = {
                image: {
                    "tags": [
                        "haltewecker-static-departures:board-21ad15d",
                        "haltewecker-static-departures:verified-rollback-21ad15d",
                    ]
                }
            }

            dry_run = self.run_cleaner(root, data_root, docker_fixture=fixture)
            output = dry_run.stdout + dry_run.stderr
            self.assertEqual(dry_run.returncode, 0, output)
            self.assertIn(
                "WOULD_DELETE_CONTAINER static-departures-api-verified-rollback-21ad15d",
                output,
            )
            self.assertIn("WOULD_DELETE_IMAGE " + image, output)
            self.assertEqual(json.loads((root / "docker-removed.json").read_text()), [])
            self.assertEqual(json.loads((root / "docker-removed-images.json").read_text()), [])

            applied = self.run_cleaner(root, data_root, dry_run=False, docker_fixture=fixture)
            output = applied.stdout + applied.stderr
            self.assertEqual(applied.returncode, 0, output)
            self.assertIn(
                "DELETE_CONTAINER static-departures-api-verified-rollback-21ad15d",
                output,
            )
            self.assertIn("DELETE_IMAGE " + image, output)
            self.assertIn(
                "verified-rollback",
                json.loads((root / "docker-removed.json").read_text()),
            )
            self.assertEqual(
                json.loads((root / "docker-removed-images.json").read_text()),
                [
                    "haltewecker-static-departures:board-21ad15d",
                    "haltewecker-static-departures:verified-rollback-21ad15d",
                ],
            )

    def test_verified_rollback_image_with_unrelated_tag_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            active = data_root / "releases" / "active-release"
            active.mkdir(parents=True)
            (active / "release-metadata.json").write_text(
                '{"releaseID":"active-release"}\n', encoding="utf-8"
            )
            (data_root / "current-release").symlink_to(active)
            fixture = self.healthy_docker_fixture()
            image = "sha256:" + "7" * 64
            fixture["containers"].append(
                {
                    "id": "verified-rollback",
                    "name": "static-departures-api-verified-rollback-21ad15d",
                    "status": "exited",
                    "image": image,
                    "writableBytes": 12288,
                }
            )
            fixture["images"] = {
                image: {
                    "tags": [
                        "haltewecker-static-departures:verified-rollback-21ad15d",
                        "custom:preserve-me",
                    ]
                }
            }

            result = self.run_cleaner(root, data_root, dry_run=False, docker_fixture=fixture)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn(
                "DELETE_CONTAINER static-departures-api-verified-rollback-21ad15d",
                output,
            )
            self.assertIn(
                "KEEP_IMAGE " + image + " reason=image-tags-not-temporary-verified-rollback",
                output,
            )
            self.assertEqual(json.loads((root / "docker-removed-images.json").read_text()), [])

    def test_orphan_verified_rollback_image_is_cleaned_without_container(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            active = data_root / "releases" / "active-release"
            active.mkdir(parents=True)
            (active / "release-metadata.json").write_text(
                '{"releaseID":"active-release"}\n', encoding="utf-8"
            )
            (data_root / "current-release").symlink_to(active)
            fixture = self.healthy_docker_fixture()
            image = "sha256:" + "7" * 64
            fixture["images"] = {
                image: {
                    "tags": [
                        "haltewecker-static-departures:board-21ad15d",
                        "haltewecker-static-departures:verified-rollback-21ad15d",
                    ]
                }
            }

            result = self.run_cleaner(root, data_root, dry_run=False, docker_fixture=fixture)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("DELETE_IMAGE " + image, output)
            self.assertEqual(
                json.loads((root / "docker-removed-images.json").read_text()),
                [
                    "haltewecker-static-departures:board-21ad15d",
                    "haltewecker-static-departures:verified-rollback-21ad15d",
                ],
            )

    def test_repo_cleanup_uses_data_root_cache_when_gtfs_override_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            current_release = releases / "current-fixture"
            current_release.mkdir(parents=True)
            (data_root / "current-release").symlink_to("releases/current-fixture")
            published_release = releases / "vbb-refresh-20260928T000000Z-100-fixture"
            (published_release / "stop-data").mkdir(parents=True)
            (published_release / "departures.sqlite").write_bytes(b"fixture")
            (published_release / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            cache_root = data_root / "cache" / "gtfs"
            source_directory = cache_root / "sweden"
            source_directory.mkdir(parents=True)
            (source_directory / ".lock").touch()
            systemctl = root / "systemctl"
            systemctl.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            systemctl.chmod(0o755)
            flock = root / "flock"
            flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            flock.chmod(0o755)

            environment = os.environ.copy()
            environment.pop("GTFS_CACHE_ROOT", None)
            environment.update(
                {
                    "DATA_ROOT": str(data_root),
                    "HALTEWECKER_PIPELINE_REPO": str(REPOSITORY_ROOT),
                    "HALTEWECKER_CLEANUP_LOCKS": f"{root / 'stop.lock'}:{root / 'static.lock'}:{root / 'vbb.lock'}",
                    "SYSTEMCTL_BIN": str(systemctl),
                    "FLOCK_BIN": str(flock),
                    "HALTEWECKER_CLEANUP_DRY_RUN": "1",
                }
            )
            for lock_name in ("stop.lock", "static.lock", "vbb.lock"):
                (root / lock_name).touch()

            result = subprocess.run(
                [str(CLEANUP_SCRIPT)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn(
                f"GTFS cache cleanup root={cache_root.resolve()}",
                result.stdout,
                f"returncode={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}",
            )
            self.assertIn("source=sweden status=skipped reason=current-missing-or-nonregular", result.stdout)

    def test_nonstandard_abandoned_build_classification_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            releases.mkdir(parents=True)

            current = releases / "20260923T000000Z-current"
            current.joinpath("stop-data").mkdir(parents=True)
            (current / "departures.sqlite").write_bytes(b"fixture")
            (current / "release-metadata.json").write_text('{"status":"published"}\n', encoding="utf-8")
            (data_root / "current-release").symlink_to("releases/20260923T000000Z-current")

            failed = releases / "build-failed-1"
            failed.mkdir()
            (failed / "build-state.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            self.make_old(failed)

            incomplete = releases / "staging-old-1"
            incomplete.mkdir()
            self.make_old(incomplete)

            recent = releases / "candidate-recent"
            recent.mkdir()
            (recent / "state.json").write_text('{"status":"incomplete"}\n', encoding="utf-8")

            unknown = releases / "mystery-directory"
            unknown.mkdir()
            (unknown / "payload.bin").write_bytes(b"unknown")
            self.make_old(unknown)

            published_nonstandard = releases / "candidate-published"
            published_nonstandard.joinpath("stop-data").mkdir(parents=True)
            (published_nonstandard / "departures.sqlite").write_bytes(b"fixture")
            (published_nonstandard / "release-metadata.json").write_text(
                '{"status":"published"}\n', encoding="utf-8"
            )
            self.make_old(published_nonstandard)

            current_target = releases / "build-current"
            current_target.mkdir()
            (current_target / "state.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            self.make_old(current_target)
            (data_root / "active-release").symlink_to("releases/build-current")

            pilot_target = releases / "incremental-packaged" / "20260923T000000Z-proof"
            pilot_target.mkdir(parents=True)
            (releases / "pilot-current").symlink_to("incremental-packaged/20260923T000000Z-proof")

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("DELETE-CANDIDATE " + str(failed.resolve()) + " reason=abandoned-build", output)
            self.assertIn("DELETE-CANDIDATE " + str(incomplete.resolve()) + " reason=abandoned-build", output)
            self.assertIn("KEEP   " + str(recent.resolve()) + " reason=recent-abandoned-build", output)
            self.assertIn("KEEP   " + str(unknown.resolve()) + " reason=unknown-state", output)
            self.assertIn("KEEP   " + str(published_nonstandard.resolve()) + " reason=published-state", output)
            self.assertIn("KEEP   " + str(current_target.resolve()) + " reason=active-symlink:", output)
            self.assertIn(
                "KEEP   " + str((releases / "pilot-current").parent.resolve() / "pilot-current")
                + " reason=protected-pointer/symlink",
                output,
            )
            self.assertIn("KEEP   " + str((releases / "incremental-packaged").resolve()) + " reason=protected-by-pilot-current", output)
            self.assertTrue(failed.exists())
            self.assertTrue(pilot_target.exists())

    def test_repo_cleanup_removes_gtfs_history_preserves_services_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            cache_root = root / "cache" / "gtfs"
            source_directory = cache_root / "sweden"
            release = data_root / "releases" / "20260907T000000Z-fixture"
            release.joinpath("stop-data").mkdir(parents=True)
            (release / "departures.sqlite").write_bytes(b"fixture")
            (release / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            (data_root / "current-release").symlink_to("releases/20260907T000000Z-fixture")
            vbb_releases = (
                "vbb-refresh-20260921T230000Z-100-aaaa1111",
                "vbb-refresh-20260922T000000Z-101-bbbb2222",
                "vbb-refresh-20260922T010000Z-102-cccc3333",
            )
            for vbb_name in vbb_releases:
                vbb_release = data_root / "releases" / vbb_name
                vbb_release.joinpath("stop-data").mkdir(parents=True)
                (vbb_release / "departures.sqlite").write_bytes(b"vbb-fixture")
                (vbb_release / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            source_directory.mkdir(parents=True)
            (root / "stop.lock").touch()
            (root / "static.lock").touch()
            (root / "vbb.lock").touch()

            current = source_directory / "current.zip"
            write_gtfs(current)
            digest = hashlib.sha256(current.read_bytes()).hexdigest()
            current_hash = source_directory / f"{digest}.zip"
            os.link(current, current_hash)
            stale_hash = source_directory / ("a" * 64 + ".zip")
            stale_hash.write_bytes(b"stale-history")
            orphan_temp = source_directory / ".download-orphan.zip"
            orphan_temp.write_bytes(b"")
            old_time = time.time() - 2 * 24 * 60 * 60
            os.utime(orphan_temp, (old_time, old_time))
            (source_directory / ".lock").touch()
            (source_directory / "state.json").write_text(
                (
                    '{"schemaVersion":1,"sourceID":"sweden",'
                    '"artifact":"current.zip","sha256":"'
                    + digest
                    + '","size":'
                    + str(current.stat().st_size)
                    + ',"validated":true}\n'
                ),
                encoding="utf-8",
            )

            systemctl = root / "systemctl"
            systemctl.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            systemctl.chmod(0o755)
            flock = root / "flock"
            flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            flock.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "DATA_ROOT": str(data_root),
                    "GTFS_CACHE_ROOT": str(cache_root),
                    "HALTEWECKER_PIPELINE_REPO": str(REPOSITORY_ROOT),
                    "HALTEWECKER_CLEANUP_LOCKS": f"{root / 'stop.lock'}:{root / 'static.lock'}:{root / 'vbb.lock'}",
                    "SYSTEMCTL_BIN": str(systemctl),
                    "FLOCK_BIN": str(flock),
                    "HALTEWECKER_GTFS_ORPHAN_TEMP_MAX_AGE_HOURS": "1",
                    "HALTEWECKER_CLEANUP_DRY_RUN": "0",
                    "HALTEWECKER_RELEASE_RETENTION_COUNT": "2",
                }
            )

            first = subprocess.run(
                [str(CLEANUP_SCRIPT)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
            self.assertIn(f"GTFS cache cleanup root={cache_root}", first.stdout)
            self.assertIn("source=sweden", first.stdout, first.stderr + first.stdout)
            self.assertIn("orphan_temp_removed=1", first.stdout)
            self.assertTrue(current.exists())
            self.assertTrue(current_hash.samefile(current))
            self.assertTrue((source_directory / ".lock").exists())
            self.assertTrue((source_directory / "state.json").exists())
            self.assertTrue((data_root / "releases" / vbb_releases[-1]).exists())
            self.assertFalse((data_root / "releases" / vbb_releases[0]).exists())
            self.assertFalse((data_root / "releases" / vbb_releases[1]).exists())
            self.assertFalse(stale_hash.exists())
            self.assertFalse(orphan_temp.exists())

            second = subprocess.run(
                [str(CLEANUP_SCRIPT)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
            self.assertIn("removed=0", second.stdout)
            self.assertIn("orphan_temp_removed=0", second.stdout)

    def test_transitive_current_release_dependency_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            releases.mkdir(parents=True)

            current_root = releases / "incremental" / "20260927T000000Z-current"
            current_root.mkdir(parents=True)
            (current_root / "release.json").write_text(json.dumps({"releaseID": current_root.name}), encoding="utf-8")
            (current_root / "validation-receipt.json").write_text(
                json.dumps({"releaseID": current_root.name, "result": "PASS"}), encoding="utf-8"
            )
            target = releases / "20260926T000000Z-stale"
            target.joinpath("stop-data").mkdir(parents=True)
            (target / "departures.sqlite").write_bytes(b"fixture")
            (target / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            (current_root / "stop-data").symlink_to("../../20260926T000000Z-stale/stop-data")
            (data_root / "current-release").symlink_to("releases/incremental/20260927T000000Z-current")

            newer = releases / "20260928T000000Z-newer"
            newer.joinpath("stop-data").mkdir(parents=True)
            (newer / "departures.sqlite").write_bytes(b"fixture")
            (newer / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            self.make_old(newer)
            self.make_old(target)

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("KEEP   " + str(target.resolve()), output)
            self.assertNotIn("WOULD_DELETE " + str(target), output)
            self.assertIn("WOULD_DELETE " + str(newer.resolve()), output)

    def test_incremental_release_retention_one_deletes_older_unreferenced_generations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            self.make_incremental_release(releases / "incremental" / "20260925T000000Z-a", "20260925T000000Z-a")
            self.make_incremental_release(releases / "incremental" / "20260926T000000Z-b", "20260926T000000Z-b")
            current = releases / "incremental" / "20260927T000000Z-c"
            self.make_incremental_release(current, current.name)
            (data_root / "current-release").symlink_to("releases/incremental/" + current.name)

            result = self.run_cleaner(root, data_root, dry_run=False)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertFalse((releases / "incremental" / "20260925T000000Z-a").exists())
            self.assertFalse((releases / "incremental" / "20260926T000000Z-b").exists())
            self.assertTrue(current.exists())

    @staticmethod
    def make_historical_incremental_release(path: Path) -> None:
        HalteWeckerCleanupTests.make_incremental_release(path, path.name)
        (path / 'stop-data').rmdir()
        (path / 'stop-data').symlink_to('../../' + path.name + '/stop-data')
        (path / 'release.json').write_text(json.dumps({
            'releaseID': path.name, 'stopData': {'path': 'stop-data'},
        }))
        HalteWeckerCleanupTests.make_old(path)

    @staticmethod
    def make_summary_helper(root: Path, *, errors: tuple[str, ...] = ()) -> Path:
        pipeline = root / 'fixture-pipeline'
        scripts = pipeline / 'scripts'
        scripts.mkdir(parents=True)
        helper = scripts / 'published_artifact_retention.py'
        helper.write_text(
            "import json\nimport sys\nfrom pathlib import Path\n"
            "calls = Path(__file__).with_suffix('.calls')\n"
            "with calls.open('a') as handle:\n"
            "    handle.write(('apply' if '--apply' in sys.argv else 'dry-run') + '\\n')\n"
            "summary = {'reclaimable_bytes': 11, "
            "'physical_accounting': {'union_reclaimable_bytes': 13}, "
            f"'errors': {list(errors)!r}, 'padding': 'x' * (2 * 1024 * 1024)}}\n"
            "print('[ArtifactRetention] summary=' + json.dumps(summary))\n",
            encoding='utf-8',
        )
        return pipeline

    def test_pass_incremental_with_dangling_stop_data_obeys_retention_and_inode_accounting(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / 'data'
            releases = data / 'releases/incremental'
            old = releases / '20260925T000000Z-old'
            current = releases / '20260927T000000Z-current'
            self.make_historical_incremental_release(old)
            self.make_incremental_release(current, current.name)
            (data / 'current-release').symlink_to(current)
            result = self.run_cleaner(root, data)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn('WOULD_DELETE ' + str(old.resolve()) + ' reason=outside-retention-and-unreferenced', output)
            self.assertNotIn('reason=abandoned-unpublished', output)
            summary_line = next(line for line in result.stdout.splitlines()
                                if line.startswith('[ArtifactRetention] summary='))
            summary = json.loads(summary_line.split('summary=', 1)[1])
            physical = summary['physical_accounting']
            self.assertIn(str(old.resolve()), [row['path'] for row in physical['candidate_roots']])
            self.assertGreater(physical['union_reclaimable_bytes'], 0)
            self.assertTrue(old.exists())
            self.assertTrue(current.exists())

    def test_referenced_historical_pass_with_dangling_stop_data_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / 'data'
            releases = data / 'releases/incremental'
            old = releases / '20260925T000000Z-old'
            current = releases / '20260927T000000Z-current'
            self.make_historical_incremental_release(old)
            self.make_incremental_release(current, current.name)
            (current / 'release.json').write_text(json.dumps({
                'releaseID': current.name,
                'runtimeDependencies': [{'path': str(old)}],
            }))
            (data / 'current-release').symlink_to(current)
            result = self.run_cleaner(root, data, dry_run=False)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn('CLEANUP_SKIPPED reason=dependency-graph-unresolved', output)
            self.assertNotIn('DELETE ' + str(old.resolve()), output)
            self.assertTrue(old.exists())
            self.assertTrue(current.exists())

    def test_historical_pass_incremental_is_not_deleted_before_failed_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / 'data'
            releases = data / 'releases/incremental'
            old = releases / '20260925T000000Z-old'
            current = releases / '20260927T000000Z-current'
            self.make_historical_incremental_release(old)
            self.make_incremental_release(current, current.name)
            (data / 'current-release').symlink_to(current)
            pipeline = self.make_summary_helper(root, errors=('unsafe-runtime-dependency',))
            result = self.run_cleaner(root, data, dry_run=False, pipeline_repo=pipeline)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('CLEANUP_SKIPPED reason=physical-accounting-or-retention-errors', output)
            self.assertIn('KEEP   ' + str(old.resolve()) + ' reason=retention-preflight-not-complete', output)
            self.assertTrue(old.exists())
            self.assertTrue(current.exists())
            self.assertEqual((pipeline / 'scripts/published_artifact_retention.calls').read_text(), 'dry-run\n')

    def test_summary_larger_than_argument_limit_is_streamed_in_dry_run_and_apply(self) -> None:
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                data = root / 'data'
                old = data / 'releases/incremental/20260925T000000Z-old'
                current = data / 'releases/incremental/20260927T000000Z-current'
                self.make_historical_incremental_release(old)
                self.make_incremental_release(current, current.name)
                (data / 'current-release').symlink_to(current)
                pipeline = self.make_summary_helper(root)
                result = self.run_cleaner(root, data, dry_run=dry_run, pipeline_repo=pipeline)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn('Argument list too long', result.stderr)
                self.assertIn('Combined physical reclaimable estimate (inode union): 13 bytes', result.stdout)
                expected_calls = 'dry-run\n' if dry_run else 'dry-run\napply\n'
                self.assertEqual((pipeline / 'scripts/published_artifact_retention.calls').read_text(), expected_calls)
                self.assertTrue(current.exists())
                self.assertEqual(old.exists(), dry_run)

    def test_unvalidated_nested_incremental_is_kept_instead_of_abandoned_cleanup(self) -> None:
        invalid_payloads = ({'releaseID': 'wrong', 'result': 'PASS'},
                            {'releaseID': '20260925T000000Z-old', 'result': 'FAIL'}, [])
        for receipt in invalid_payloads:
            with self.subTest(receipt=receipt), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                data = root / 'data'
                releases = data / 'releases/incremental'
                old = releases / '20260925T000000Z-old'
                current = releases / '20260927T000000Z-current'
                self.make_historical_incremental_release(old)
                (old / 'validation-receipt.json').write_text(json.dumps(receipt))
                self.make_incremental_release(current, current.name)
                (data / 'current-release').symlink_to(current)
                result = self.run_cleaner(root, data, dry_run=False)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                self.assertIn('KEEP   ' + str(old.resolve()) + ' reason=unvalidated-incremental-generation', output)
                self.assertTrue(old.exists())
                self.assertTrue(current.exists())

    def test_retention_slot_prioritizes_current_when_newer_unreferenced_generation_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            current = releases / "incremental" / "20260927T000000Z-current"
            newer = releases / "20260928T000000Z-newer"
            self.make_incremental_release(current, current.name)
            newer.joinpath("stop-data").mkdir(parents=True)
            (newer / "departures.sqlite").write_bytes(b"fixture")
            (newer / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            (data_root / "current-release").symlink_to("releases/incremental/" + current.name)

            result = self.run_cleaner(root, data_root, dry_run=True)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("KEEP   " + str(current.resolve()), output)
            self.assertNotIn("WOULD_DELETE " + str(current), output)
            self.assertIn("WOULD_DELETE " + str(newer.resolve()) + " reason=outside-retention-and-unreferenced", output)

    def test_same_id_direct_build_does_not_displace_current_incremental_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            release_id = "20260927T000000Z-sameid"
            direct = releases / release_id
            direct.joinpath("stop-data").mkdir(parents=True)
            (direct / "departures.sqlite").write_bytes(b"direct")
            (direct / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            nested = releases / "incremental" / release_id
            self.make_incremental_release(nested, release_id)
            (data_root / "current-release").symlink_to("releases/incremental/" + release_id)

            result = self.run_cleaner(root, data_root, dry_run=False)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertFalse(direct.exists(), output)
            self.assertTrue(nested.exists())

    def test_nested_runtime_manifest_path_is_resolved_relative_to_its_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            current = releases / "incremental" / "20260927T000000Z-current"
            self.make_incremental_release(current, current.name)
            stale = releases / "20260926T000000Z-stale"
            stale.mkdir(parents=True)
            (stale / "stop-data").mkdir()
            (stale / "departures.sqlite").write_bytes(b"fixture")
            (stale / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            runtime_manifest = current / "providers" / "runtime" / "manifest.json"
            runtime_manifest.parent.mkdir(parents=True)
            (runtime_manifest).write_text(json.dumps({
                "runtimeDependencies": [{"path": "../../../../20260926T000000Z-stale/stop-data"}]
            }), encoding="utf-8")
            (data_root / "current-release").symlink_to("releases/incremental/" + current.name)

            result = self.run_cleaner(root, data_root, dry_run=True)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("KEEP   " + str(stale.resolve()), output)

    def test_nested_previous_rollback_and_pilot_references_are_kept(self) -> None:
        for pointer_name in ('previous/stop-data', 'rollback', 'pilot-current'):
            with self.subTest(pointer=pointer_name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                data_root = root / 'data'
                releases = data_root / 'releases/incremental'
                previous = releases / '20260926T000000Z-previous'
                current = releases / '20260927T000000Z-current'
                self.make_incremental_release(previous, previous.name)
                self.make_incremental_release(current, current.name)
                (data_root / 'current-release').symlink_to(current)
                pointer = data_root / pointer_name
                pointer.parent.mkdir(parents=True, exist_ok=True)
                pointer.symlink_to(previous / 'stop-data' if pointer_name.startswith('previous/') else previous)
                result = self.run_cleaner(root, data_root)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                self.assertIn('KEEP   ' + str(previous.resolve()), output)
                self.assertNotIn('WOULD_DELETE ' + str(previous.resolve()), output)

    def test_unfinished_rollback_marker_protects_nested_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / 'data'
            releases = data_root / 'releases/incremental'
            previous = releases / '20260926T000000Z-previous'
            current = releases / '20260927T000000Z-current'
            self.make_incremental_release(previous, previous.name)
            self.make_incremental_release(current, current.name)
            (data_root / 'current-release').symlink_to(current)
            marker = data_root / 'temp/current-rollback' / previous.name
            marker.mkdir(parents=True)
            (marker / 'state.json').write_text('{"state":"pending"}')
            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn('KEEP   ' + str(previous.resolve()) + ' reason=rollback-release:', output)
            self.assertNotIn('WOULD_DELETE ' + str(previous.resolve()), output)

    def test_broken_previous_pointer_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            current = data_root / "releases" / "20260927T000000Z-current"
            current.mkdir(parents=True)
            (current / "stop-data").mkdir()
            (current / "departures.sqlite").write_bytes(b"fixture")
            (current / "release-metadata.json").write_text("{}\n", encoding="utf-8")
            (data_root / "current-release").symlink_to("releases/" + current.name)
            (data_root / "previous").mkdir()
            (data_root / "previous" / "stop-data").symlink_to("../releases/missing/stop-data")

            result = self.run_cleaner(root, data_root, dry_run=True)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("CLEANUP_SKIPPED reason=dependency-graph-unresolved", output)

    def test_unresolved_protected_dependency_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            releases.mkdir(parents=True)

            current_root = releases / "20260927T000000Z-current"
            current_root.mkdir()
            (current_root / "stop-data").symlink_to("../20260926T000000Z-missing/stop-data")
            (data_root / "current-release").symlink_to("releases/20260927T000000Z-current")

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("unresolved-dependency", output)
            self.assertIn("CLEANUP_SKIPPED reason=dependency-graph-unresolved", output)
            self.assertNotIn("DELETE ", output)

    def test_active_pass_validation_is_cleanup_eligible_but_active_release_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            (data_root / "releases").mkdir(parents=True)
            active_release_id = "20260930T102204Z-germany-candidate-r4"
            active_release = data_root / "experiments" / "candidate" / "releases" / active_release_id
            active_release.mkdir(parents=True)
            (active_release / "release.json").write_text(
                '{"releaseID":"20260930T102204Z-germany-candidate-r4"}\n', encoding="utf-8"
            )
            (data_root / "current-release").symlink_to(active_release)
            validation = data_root / "validation" / "active-check"
            validation.mkdir(parents=True)
            (validation / "validation-receipt.json").write_text(
                '{"result":"PASS","releaseID":"20260930T102204Z-germany-candidate-r4"}\n',
                encoding="utf-8",
            )
            self.make_age_hours(validation, 0.1)

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("WOULD_DELETE " + str(validation.resolve()) + " reason=expired-validation", output)
            self.assertTrue(active_release.exists())

    def test_missing_provenance_allows_obsolete_release_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            active = data_root / "releases" / "active-r4"
            active.mkdir(parents=True)
            (data_root / "current-release").symlink_to(active)
            obsolete = data_root / "releases" / "build-obsolete"
            obsolete.mkdir()
            (obsolete / "build-state.json").write_text('{"status":"failed"}')
            self.make_age_hours(obsolete, 13)
            missing = obsolete / "external-artifacts" / "ireland"
            record = {"path": str(missing), "sourceID": "ireland"}
            (active / "manifest.json").write_text(json.dumps({"inputProvenance": {"ireland": record}}))
            (active / "provenance").mkdir()
            (active / "provenance" / "input-artifacts.json").write_text(json.dumps({"sources": {"ireland": record}}))

            result = self.run_cleaner(root, data_root, dry_run=False)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertEqual(output.count("reason=missing-provenance-source"), 2, output)
            self.assertNotIn("dependency-graph-unresolved", output)
            self.assertFalse(obsolete.exists(), output)
            self.assertTrue(active.exists())
            self.assertTrue((active / "manifest.json").exists())

    def test_missing_gtfs_source_provenance_does_not_block_unretained_release_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            active_id = "20261014T223508Z-1273074"
            active = releases / active_id
            active_stop_data = active / "stop-data"
            active_stop_data.mkdir(parents=True)
            (active / "departures.sqlite").write_bytes(b"active-database")
            missing_sources = {
                "israel-mot": {"path": str(data_root / "cache/gtfs/israel-mot/missing.zip")},
                "poland-gdansk": {"path": str(data_root / "cache/gtfs/poland-gdansk/missing.zip")},
            }
            (active / "release-metadata.json").write_text(
                json.dumps({"releaseID": active_id, "inputProvenance": missing_sources}),
                encoding="utf-8",
            )
            (data_root / "current-release").symlink_to(active)

            failed_id = "20261015T223505Z-56956"
            failed = releases / failed_id
            (failed / "stop-data").mkdir(parents=True)
            (failed / "departures.sqlite").write_bytes(b"failed-candidate-database")
            (failed / "release-metadata.json").write_text(
                json.dumps({"releaseID": failed_id}), encoding="utf-8"
            )
            (failed / "release-state.json").write_text(
                '{"releaseID":"20261015T223505Z-56956","completedStage":"handoff-readiness"}\n',
                encoding="utf-8",
            )
            self.make_age_hours(failed, 13)

            (active / "gtfs-artifacts.json").write_text(
                json.dumps({"sources": missing_sources}), encoding="utf-8"
            )
            (active / "custom-gtfs-artifacts.json").write_text(
                json.dumps({"sources": {"vbb": {"path": str(data_root / "cache/gtfs/vbb/missing.zip")}}}),
                encoding="utf-8",
            )
            (active / "austrian-artifacts.json").write_text(
                json.dumps({"sources": [{"path": str(data_root / "cache/gtfs/austria/missing.zip")}]}),
                encoding="utf-8",
            )
            (active_stop_data / "manifest.json").write_text(
                json.dumps({"inputProvenance": missing_sources}), encoding="utf-8"
            )
            provenance = active_stop_data / "provenance"
            provenance.mkdir()
            (provenance / "input-artifacts.json").write_text(
                json.dumps({"sources": missing_sources}), encoding="utf-8"
            )

            pipeline = self.make_summary_helper(root)
            result = self.run_cleaner(root, data_root, dry_run=False, pipeline_repo=pipeline)
            output = result.stdout + result.stderr

            self.assertEqual(result.returncode, 0, output)
            self.assertIn("reason=missing-provenance-source", output)
            self.assertNotIn("CLEANUP_SKIPPED reason=dependency-graph-unresolved", output)
            self.assertIn(f"DELETE {failed.resolve()} reason=outside-retention-and-unreferenced", output)
            self.assertFalse(failed.exists(), output)
            self.assertTrue(active.exists())
            self.assertTrue((active / "departures.sqlite").exists())

    def test_gtfs_artifact_runtime_dependencies_remain_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            active_id = "20261014T223508Z-1273074"
            active = releases / active_id
            (active / "stop-data").mkdir(parents=True)
            (active / "departures.sqlite").write_bytes(b"active-database")
            (active / "release-metadata.json").write_text(
                json.dumps({"releaseID": active_id}), encoding="utf-8"
            )
            (data_root / "current-release").symlink_to(active)

            dependency_id = "20261015T223505Z-56956"
            dependency = releases / dependency_id
            (dependency / "stop-data").mkdir(parents=True)
            (dependency / "departures.sqlite").write_bytes(b"runtime-database")
            (dependency / "release-metadata.json").write_text(
                json.dumps({"releaseID": dependency_id}), encoding="utf-8"
            )
            self.make_age_hours(dependency, 13)
            (active / "gtfs-artifacts.json").write_text(
                json.dumps({"runtimeDependencies": [{"path": str(dependency)}]}),
                encoding="utf-8",
            )

            pipeline = self.make_summary_helper(root)
            result = self.run_cleaner(root, data_root, dry_run=False, pipeline_repo=pipeline)
            output = result.stdout + result.stderr

            self.assertEqual(result.returncode, 0, output)
            self.assertIn("manifest-dependency:", output)
            self.assertIn("KEEP   " + str(dependency.resolve()), output)
            self.assertTrue(dependency.exists())
            self.assertTrue(active.exists())

    def test_missing_relative_runtime_database_blocks_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            active = data_root / "releases" / "active-r4"
            active.mkdir(parents=True)
            (data_root / "current-release").symlink_to(active)
            (active / "release.json").write_text(json.dumps({"common": {"path": "common.sqlite"}}))

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("CLEANUP_SKIPPED reason=dependency-graph-unresolved", output)
            self.assertIn("common.sqlite", output)
            self.assertNotIn("DELETE ", output)

    def test_runtime_database_and_open_provenance_artifact_are_protected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            active = data_root / "releases" / "active-r4"
            active.mkdir(parents=True)
            (data_root / "current-release").symlink_to(active)
            runtime = data_root / "releases" / "build-runtime"
            opened = data_root / "releases" / "build-opened"
            for directory in (runtime, opened):
                directory.mkdir()
                (directory / "provider.sqlite").write_bytes(b"fixture")
                (directory / "build-state.json").write_text('{"status":"failed"}')
                self.make_age_hours(directory, 13)
            (active / "release.json").write_text(json.dumps({"common": {"path": str(runtime / "provider.sqlite")}}))
            (active / "manifest.json").write_text(json.dumps({"inputProvenance": {"path": str(opened / "provider.sqlite")}}))

            result = self.run_cleaner(root, data_root, dry_run=False, open_paths=(opened / "provider.sqlite",))
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertNotIn("dependency-graph-unresolved", output)
            self.assertTrue(active.exists())
            self.assertTrue(runtime.exists(), output)
            self.assertTrue(opened.exists(), output)
            self.assertIn("active-runtime-open-file", output)

    def test_successful_orphan_staging_is_immediate_and_failed_staging_has_six_hour_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            (data_root / "releases").mkdir(parents=True)
            successful = data_root / "staging" / "completed-build"
            successful.mkdir(parents=True)
            (successful / "build-state.json").write_text('{"status":"published"}\n', encoding="utf-8")
            self.make_age_hours(successful, 0.1)

            failed = data_root / "staging" / "failed-build"
            failed.mkdir(parents=True)
            (failed / "build-state.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            self.make_age_hours(failed, 5.9)

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("WOULD_DELETE " + str(successful.resolve()) + " reason=expired-staging", output)
            self.assertIn("KEEP   " + str(failed.resolve()) + " reason=recent-staging", output)

    def test_release_scoped_staging_uses_success_state_or_six_hour_failure_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            published = releases / "20260930T000000Z-published"
            published.mkdir(parents=True)
            (published / "state.json").write_text('{"status":"published"}\n', encoding="utf-8")
            successful_staging = published / "departures-next.sqlite"
            successful_staging.write_bytes(b"staged")
            self.make_age_hours(successful_staging, 0.1)

            failed = releases / "20260930T000001Z-failed"
            failed.mkdir(parents=True)
            (failed / "state.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            failed_staging = failed / "departures-next.sqlite"
            failed_staging.write_bytes(b"staged")
            self.make_age_hours(failed_staging, 5.9)

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn(
                "WOULD_DELETE " + str(successful_staging.resolve())
                + " reason=successful-release-publication",
                output,
            )
            self.assertIn(
                "ACTIVE STAGING " + str(failed_staging.resolve())
                + " status=KEEP reason=inside-failed-staging-window",
                output,
            )

    def test_failed_validation_is_retained_until_twelve_hour_diagnostic_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            (data_root / "releases").mkdir(parents=True)
            recent = data_root / "validation" / "failed-recent"
            recent.mkdir(parents=True)
            (recent / "result.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            self.make_age_hours(recent, 11.9)
            expired = data_root / "validation" / "failed-expired"
            expired.mkdir(parents=True)
            (expired / "result.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            self.make_age_hours(expired, 12.1)

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("KEEP   " + str(recent.resolve()) + " reason=recent-validation", output)
            self.assertIn("WOULD_DELETE " + str(expired.resolve()) + " reason=expired-validation", output)

    def test_abandoned_build_uses_twelve_hour_ttl_and_protects_symlink_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            releases = data_root / "releases"
            releases.mkdir(parents=True)

            active = data_root / "experiments" / "germany-candidate" / "releases" / "active-r4"
            active.mkdir(parents=True)
            target = releases / "incremental" / "20260930T000000Z-active-reference"
            target.mkdir(parents=True)
            (active / "release.json").write_text(
                '{"artifactPath":"' + str(target) + '"}\n', encoding="utf-8"
            )
            (data_root / "current-release").symlink_to(active)
            referenced = releases / "20260929T000000Z-symlink-referenced"
            referenced.mkdir(parents=True)
            (data_root / "active-artifact").symlink_to(referenced)

            recent = releases / "candidate-recent"
            recent.mkdir()
            (recent / "state.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            self.make_age_hours(recent, 11.9)

            expired = releases / "build-expired"
            expired.mkdir()
            (expired / "state.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            self.make_age_hours(expired, 12.1)

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("KEEP   " + str(recent.resolve()) + " reason=recent-abandoned-build", output)
            self.assertIn("DELETE-CANDIDATE " + str(expired.resolve()) + " reason=abandoned-build", output)
            self.assertTrue(target.exists())
            self.assertIn(
                "KEEP   " + str(referenced.resolve()) + " reason=active-symlink:",
                output,
            )

    def test_legacy_standalone_departure_uses_twelve_hour_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_root = root / "data"
            release = data_root / "releases" / "20260930T000000Z-current"
            release.mkdir(parents=True)
            (release / "release-metadata.json").write_text('{"status":"published"}\n', encoding="utf-8")
            (data_root / "current-release").symlink_to(release)
            legacy = data_root / "departures" / "releases" / "legacy.sqlite"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"obsolete")
            self.make_age_hours(legacy, 12.1)

            result = self.run_cleaner(root, data_root)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn(
                "WOULD_DELETE " + str(legacy.resolve()) + " reason=obsolete-unbound-departures-backup",
                output,
            )


if __name__ == "__main__":
    unittest.main()
