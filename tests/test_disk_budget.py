"""Disk reserve and owned build process cleanup.

Created by Anton on 2026-10-10.
"""

import signal
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.disk_budget import GIB, run_guarded, terminate_build


class DiskBudgetTests(unittest.TestCase):
    def run_fixture(self, free, status=0):
        process = Mock(pid=12345)
        process.poll.return_value = status
        with patch("scripts.disk_budget.free_bytes", side_effect=free), \
             patch("scripts.disk_budget.subprocess.Popen", return_value=process) as start, \
             patch("scripts.disk_budget.os.killpg") as kill:
            result = run_guarded(["build"], Path("/tmp"), interval=0)
        return result, process, start, kill

    def test_success_has_its_own_process_group(self):
        result, _process, start, kill = self.run_fixture([30 * GIB, 29 * GIB])
        self.assertEqual(result, 0)
        start.assert_called_once_with(["build"], start_new_session=True)
        kill.assert_not_called()

    def test_build_failure_is_propagated(self):
        result, *_ = self.run_fixture([30 * GIB, 30 * GIB], status=7)
        self.assertEqual(result, 7)

    def test_build_is_not_started_below_reserve_and_write_buffer(self):
        result, _process, start, _kill = self.run_fixture([24 * GIB])
        self.assertEqual(result, 75)
        start.assert_not_called()

    def test_budget_exhaustion_terminates_only_owned_process_group(self):
        result, process, _start, kill = self.run_fixture([30 * GIB, 24 * GIB])
        self.assertEqual(result, 75)
        kill.assert_called_once_with(process.pid, signal.SIGTERM)
        process.wait.assert_called_once_with(timeout=5)

    def test_failed_space_measurement_leaves_no_running_build(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch("scripts.disk_budget.free_bytes", side_effect=[30 * GIB, OSError("unavailable")]), \
             patch("scripts.disk_budget.subprocess.Popen", return_value=process), \
             patch("scripts.disk_budget.os.killpg") as kill:
            with self.assertRaises(OSError):
                run_guarded(["build"], Path("/tmp"))
        kill.assert_called_once_with(process.pid, signal.SIGTERM)

    def test_stuck_build_is_killed_and_reaped(self):
        process = Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired("build", 5), 0]
        with patch("scripts.disk_budget.os.killpg") as kill:
            terminate_build(process)
        self.assertEqual(kill.call_args_list[-1].args, (process.pid, signal.SIGKILL))
        self.assertEqual(process.wait.call_count, 2)

    def test_interruption_reaps_child_and_restores_signal_handlers(self):
        handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch("scripts.disk_budget.free_bytes", return_value=30 * GIB), \
             patch("scripts.disk_budget.subprocess.Popen", return_value=process), \
             patch("scripts.disk_budget.os.killpg") as kill, \
             patch("scripts.disk_budget.time.sleep", side_effect=InterruptedError("stop")):
            with self.assertRaises(InterruptedError):
                run_guarded(["build"], Path("/tmp"))
        kill.assert_called_once_with(process.pid, signal.SIGTERM)
        self.assertEqual({signum: signal.getsignal(signum) for signum in handlers}, handlers)

    def test_floor_below_twenty_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "lower than 20"):
            run_guarded(["build"], Path("/tmp"), minimum_gib=19)
