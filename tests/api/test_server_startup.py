"""Only ephemeral ports/projects: a duplicate server must not recover live jobs."""
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import backend.api.server as server
from backend.workflow.pipeline import Pipeline


class ServerStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "project.json"
        state = {"schema_version": 1, "project": {"id": "test", "run_status": "running"},
                 "jobs": [{"id": "running-job", "status": "running"}, {"id": "queued-job", "status": "queued"}],
                 "characters": [], "locations": [], "episodes": [], "exports": []}
        self.path.write_text(json.dumps(state, ensure_ascii=False, indent=3) + "\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_occupied_port_never_constructs_pipeline_or_changes_running_state(self):
        before = self.path.read_bytes()
        constructed = []
        def construct_pipeline(store):
            pipeline = Pipeline(store)  # If incorrectly reached, this really recovers the temporary jobs.
            constructed.append(pipeline)
            return pipeline
        try:
            with socket.socket() as occupied:
                occupied.bind(("127.0.0.1", 0))
                occupied.listen(1)
                port = occupied.getsockname()[1]
                with patch.object(server, "Pipeline", side_effect=construct_pipeline) as constructor, \
                     patch.object(server.Store, "initialize") as initialize:
                    with self.assertRaises(OSError):
                        server.serve(self.root, port)
                constructor.assert_not_called()
                initialize.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)
        finally:
            for pipeline in constructed:
                pipeline.executor.shutdown(wait=True)

    def test_startup_failure_after_bind_releases_listening_socket(self):
        real_server = server.ThreadingHTTPServer
        listeners = []
        def bind_server(*args, **kwargs):
            listener = real_server(*args, **kwargs)
            listeners.append(listener)
            return listener
        with patch.object(server, "ThreadingHTTPServer", side_effect=bind_server), \
             patch.object(server, "Pipeline", side_effect=RuntimeError("initialization failed")):
            with self.assertRaisesRegex(RuntimeError, "initialization failed"):
                server.serve(self.root, 0)
        self.assertEqual(len(listeners), 1)
        self.assertEqual(listeners[0].fileno(), -1)
        with socket.socket() as replacement:
            replacement.bind(listeners[0].server_address)


if __name__ == "__main__":
    unittest.main()
