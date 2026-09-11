import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("deployer", Path(__file__).parents[1] / "deploy/deploy.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root / "app"
        self.data = root / "state"
        (self.root / "releases").mkdir(parents=True)
        self.data.mkdir()
        for name, value in (("ROOT", self.root), ("DATA", self.data), ("REPO", self.data / "repo.git")):
            patcher = patch.object(d, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.old = "a" * 40
        self.new = "b" * 40
        for sha in (self.old, self.new):
            p = self.root / "releases" / sha
            p.mkdir()
            (p / ".complete").write_text(sha)
        d.point("current", self.old)
        self.state = {"current": self.old, "successful": [self.old]}

    def test_success_records_previous_and_clears_transaction(self):
        with patch.object(d, "control"), patch.object(d, "wait_healthy", return_value=True):
            d.activate(self.state, {}, self.new)
        self.assertEqual(d.linked("current"), self.new)
        self.assertEqual(d.linked("previous"), self.old)
        self.assertNotIn("transaction", d.read_state())

    def test_failed_health_restores_old_release(self):
        with patch.object(d, "control") as control, patch.object(d, "wait_healthy", side_effect=[False, True]):
            with self.assertRaisesRegex(RuntimeError, "Candidate"):
                d.activate(self.state, {}, self.new)
        self.assertEqual(d.linked("current"), self.old)
        self.assertEqual(control.call_count, 4)
        self.assertNotIn("transaction", d.read_state())

    def test_failed_rollback_pauses_and_preserves_recovery_record(self):
        with patch.object(d, "control"), patch.object(d, "wait_healthy", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "Rollback"):
                d.activate(self.state, {}, self.new)
        state = d.read_state()
        self.assertTrue(state["paused"])
        self.assertEqual(state["transaction"]["old"], self.old)

    def test_interrupted_transaction_recovers_before_next_update(self):
        self.state["transaction"] = {"old": self.old, "target": self.new}
        d.point("current", self.new)
        d.save(self.state)
        with patch.object(d, "control"), patch.object(d, "wait_healthy", return_value=True):
            d.restore(self.state, {})
        self.assertEqual(d.linked("current"), self.old)
        self.assertNotIn("transaction", d.read_state())

    def test_unchanged_sha_does_not_build_or_restart(self):
        with patch.object(d, "run", return_value=self.old), patch.object(d, "prepare") as prepare, patch.object(d, "control") as control:
            d.update(self.state, {}, False)
        prepare.assert_not_called()
        control.assert_not_called()

    def test_build_failure_keeps_current_and_sets_cooldown(self):
        with patch.object(d, "run", return_value=self.new), patch.object(d, "prepare", side_effect=RuntimeError("pip failure")), patch.object(d, "control") as control:
            with self.assertRaisesRegex(RuntimeError, "pip failure"):
                d.update(self.state, {}, False)
        control.assert_not_called()
        self.assertEqual(d.linked("current"), self.old)
        self.assertEqual(d.read_state()["failed"]["stage"], "prepare")

    def test_opend_offline_does_not_switch(self):
        with patch.object(d, "run", return_value=self.new), patch.object(d, "prepare"), patch.object(d, "opend_available", return_value=False), patch.object(d, "control") as control:
            d.update(self.state, {}, False)
        control.assert_not_called()
        self.assertEqual(d.linked("current"), self.old)

    def test_incomplete_release_cannot_be_activated(self):
        (self.root / "releases" / self.new / ".complete").unlink()
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            d.point("current", self.new)
        self.assertEqual(d.linked("current"), self.old)


if __name__ == "__main__":
    unittest.main()
