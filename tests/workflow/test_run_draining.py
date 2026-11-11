"""A paused dispatcher cannot overlap work that is still saving paid results."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.workflow.pipeline import Pipeline
from core.store import Store


class RunDrainingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.store.save({"project": {"run_status": "idle"}, "characters": [], "locations": [],
                         "episodes": [], "jobs": [], "exports": []})
        self.pipeline = Pipeline(self.store)

    def tearDown(self):
        self.pipeline.executor.shutdown(wait=True)
        self.temp.cleanup()

    def test_paused_worker_must_finish_saving_before_new_dispatch_can_start(self):
        entered, release = threading.Event(), threading.Event()
        def submitted_video_result():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("offline worker was not released")
            self.store.mutate(lambda state: state.update(saved_result="original-task-result"))
            return {"saved": True}
        future = self.pipeline.executor.submit(self.pipeline._perform, "video", "EP001/S001", submitted_video_result)
        try:
            self.assertTrue(entered.wait(2))
            self.pipeline.pause()
            # Reproduce run() finally marking paused while its future remains active.
            self.store.mutate(lambda state: state["project"].update(run_status="paused", running=False))
            before = self.store.read()
            with patch("backend.workflow.pipeline.threading.Thread") as dispatcher:
                result = self.pipeline.start_run()
            self.assertTrue(result["draining"])
            self.assertEqual(result["active_job_ids"], [before["jobs"][0]["id"]])
            dispatcher.assert_not_called()
            self.assertEqual(self.store.read(), before)
            self.assertTrue(self.pipeline.pause_event.is_set())
        finally:
            release.set()
            future.result(timeout=3)
        completed = self.store.read()
        self.assertEqual(completed["saved_result"], "original-task-result")
        self.assertEqual(completed["jobs"][0]["status"], "succeeded")
        # A historical run row must not block a fresh dispatcher after work drains.
        self.store.mutate(lambda state: state["jobs"].append({"id": "old-run", "kind": "run", "status": "running"}))
        with patch("backend.workflow.pipeline.threading.Thread") as dispatcher:
            result = self.pipeline.start_run()
        self.assertNotIn("draining", result)
        self.assertIn("job_id", result)
        dispatcher.return_value.start.assert_called_once()

    def test_all_known_production_jobs_block_but_completed_or_nonproduction_rows_do_not(self):
        for kind in ("plan", "continuity", "image", "image-download", "video", "video-repair", "render"):
            for status in ("queued", "running"):
                with self.subTest(kind=kind, status=status):
                    self.store.mutate(lambda state: state.update(jobs=[{"id": "active", "kind": kind, "status": status}]))
                    before = self.store.read()
                    with patch("backend.workflow.pipeline.threading.Thread") as dispatcher:
                        result = self.pipeline.start_run()
                    self.assertTrue(result["draining"])
                    dispatcher.assert_not_called()
                    self.assertEqual(self.store.read(), before)
        self.store.mutate(lambda state: state.update(jobs=[
            {"id": "run-row", "kind": "run", "status": "running"},
            {"id": "other", "kind": "unrelated-metadata", "status": "running"},
            {"id": "finished", "kind": "render", "status": "succeeded"},
            {"id": "failed", "kind": "video", "status": "failed"},
            {"id": "unknown", "kind": "image", "status": "needs_review"}]))
        with patch("backend.workflow.pipeline.threading.Thread") as dispatcher:
            result = self.pipeline.start_run()
        self.assertIn("job_id", result)
        self.assertNotIn("draining", result)
        dispatcher.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
