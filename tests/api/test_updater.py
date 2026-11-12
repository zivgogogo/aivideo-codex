"""Real local Git updates: no GitHub access, AI calls or user project data."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from backend.updater import (UpdateError, UpdateManager, git, read_json,
                             repository_state, write_json)

SOURCE = Path(__file__).resolve().parents[2]
# A tiny real HTTP process lets the copied worker verify both successful restart
# and rollback after an invalid new application, without model dependencies.
APPLICATION = '''import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from backend.updater import UpdateManager
import json
parser = argparse.ArgumentParser()
parser.add_argument('command')
parser.add_argument('--port', type=int)
args, _ = parser.parse_known_args()
root = Path(__file__).resolve().parents[1]
manager = UpdateManager(root, args.port, root/'production/projects/default')
manager.register()
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        data=json.dumps(manager.snapshot()).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        self.wfile.write(data)
HTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
'''


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.author = self.base / 'author'
        self.author.mkdir()
        git(self.author, 'init', '-b', 'main')
        git(self.author, 'config', 'user.email', 'update-test@example.invalid')
        git(self.author, 'config', 'user.name', 'Offline update test')
        (self.author / '.gitignore').write_text('.venv\nproduction/\n.env\n__pycache__/\n*.pyc\n')
        (self.author / 'requirements.txt').write_text('# No third party packages in this fixture\n')
        (self.author / 'README.md').write_text('Version one\n')
        (self.author / 'backend').mkdir()
        (self.author / 'backend/__init__.py').touch()
        (self.author / 'backend/__main__.py').write_text(APPLICATION)
        shutil.copyfile(SOURCE / 'backend/updater.py', self.author / 'backend/updater.py')
        self.commit('Initial application')
        self.remote = self.base / 'public.git'
        git(self.base, 'clone', '--bare', str(self.author), str(self.remote))
        git(self.author, 'remote', 'add', 'origin', str(self.remote))
        self.root = self.base / 'installed'
        git(self.base, 'clone', str(self.remote), str(self.root))
        # Reuse the test runner environment. No packages are installed/modified
        # since fixture requirements never change.
        environment = Path(sys.prefix)
        (self.root / '.venv').symlink_to(environment, target_is_directory=True)
        self.python = self.root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not self.python.exists():
            self.skipTest('Run this suite with the initialized .venv Python')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.manager = UpdateManager(self.root, self.port, self.root/'production/projects/default',
                                     remote=str(self.remote), python=self.python)
        self.manager.register()
        self.before = self.manager.running_sha
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        # Only terminate detached restart processes registered inside this test's
        # fresh directory. Never signal a listener discovered outside the fixture.
        for record in (self.root / 'production/.updates/servers').glob('*.json'):
            row = read_json(record)
            if row.get('pid') and row['pid'] != os.getpid():
                try:
                    os.kill(row['pid'], 15)
                except ProcessLookupError:
                    pass
        self.manager.close()
        self.temporary.cleanup()

    def commit(self, message):
        git(self.author, 'add', '.')
        git(self.author, 'commit', '-m', message)
        return git(self.author, 'rev-parse', 'HEAD')

    def publish(self, name='README.md', content='Version two\n'):
        destination = self.author / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
        sha = self.commit('New application version')
        git(self.author, 'push', 'origin', 'main')
        return sha

    def test_latest_and_new_version_with_ignored_local_data(self):
        self.assertFalse(self.manager.check()['available'])
        for name in ('.env', 'production/projects/one/project.json', 'production/canvas-projects/one/image.png'):
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text('LOCAL DATA')
        target = self.publish()
        state = self.manager.check()
        self.assertTrue(state['can_update'])
        self.assertEqual(state['target_sha'], target)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), self.before)
        self.assertIn('New application version', state['commits'][0])

    def test_dirty_code_and_untracked_code_block_update(self):
        self.publish()
        for name in ('README.md', 'unexpected.py'):
            with self.subTest(name=name):
                destination = self.root / name
                old = destination.read_text() if destination.exists() else None
                destination.write_text('Uncommitted local edit')
                state = self.manager.check()
                self.assertFalse(state['can_update'])
                self.assertIn('未提交', state['message'])
                if old is None:
                    destination.unlink()
                else:
                    destination.write_text(old)

    def test_other_branch_remote_and_zip_are_explained(self):
        git(self.root, 'checkout', '-b', 'local-work')
        self.assertIn('main', self.manager.check()['message'])
        git(self.root, 'checkout', 'main')
        git(self.root, 'remote', 'set-url', 'origin', 'https://example.invalid/other.git')
        self.assertIn('其他仓库', self.manager.check()['message'])
        with self.assertRaisesRegex(UpdateError, 'ZIP'):
            repository_state(self.base)

    def test_divergent_local_commit_is_never_merged(self):
        git(self.root, 'config', 'user.email', 'local@example.invalid')
        git(self.root, 'config', 'user.name', 'Local test')
        (self.root/'local-code.py').write_text('local = True\n')
        git(self.root, 'add', 'local-code.py')
        git(self.root, 'commit', '-m', 'Local change')
        self.manager.running_sha = git(self.root, 'rev-parse', 'HEAD')
        self.publish()
        state = self.manager.check()
        self.assertFalse(state['can_update'])
        self.assertIn('分歧', state['message'])

    def test_protected_local_paths_are_rejected(self):
        destination = self.author / 'production/private.txt'
        destination.parent.mkdir()
        destination.write_text('Should never be distributed')
        git(self.author, 'add', '-f', 'production/private.txt')
        self.commit('Accidentally add local data')
        git(self.author, 'push', 'origin', 'main')
        state = self.manager.check()
        self.assertFalse(state['can_update'])
        self.assertIn('本地配置或制作资料', state['message'])

    def test_new_symlink_is_rejected(self):
        (self.author / 'linked.txt').symlink_to('README.md')
        self.commit('Add a symlink')
        git(self.author, 'push', 'origin', 'main')
        self.assertIn('软链接', self.manager.check()['message'])

    def test_ignored_file_collision_is_preserved(self):
        (self.root / '.git/info/exclude').write_text('new-config.txt\n')
        (self.root / 'new-config.txt').write_text('LOCAL CONFIG')
        self.publish('new-config.txt', 'Upstream contents')
        state = self.manager.check()
        self.assertFalse(state['can_update'])
        self.assertIn('重名', state['message'])
        self.assertEqual((self.root / 'new-config.txt').read_text(), 'LOCAL CONFIG')

    def test_leading_space_in_ignored_filename_is_preserved(self):
        (self.root/'.git/info/exclude').write_text(' local-config.txt\n')
        (self.root/' local-config.txt').write_text('LOCAL CONFIG')
        self.publish(' local-config.txt', 'Upstream file')
        self.assertIn('重名', self.manager.check()['message'])
        self.assertEqual((self.root/' local-config.txt').read_text(), 'LOCAL CONFIG')

    def test_rename_destination_cannot_overwrite_ignored_local_file(self):
        (self.root/'.git/info/exclude').write_text('new-config.txt\n')
        (self.root/'new-config.txt').write_text('LOCAL CONFIG')
        git(self.author, 'mv', 'README.md', 'new-config.txt')
        self.commit('Rename documentation')
        git(self.author, 'push', 'origin', 'main')
        self.assertIn('重名', self.manager.check()['message'])
        self.assertEqual((self.root/'new-config.txt').read_text(), 'LOCAL CONFIG')

    def test_ignored_parent_file_cannot_be_replaced_by_new_directory(self):
        (self.root/'.git/info/exclude').write_text('new-config\n')
        (self.root/'new-config').write_text('LOCAL CONFIG')
        self.publish('new-config/options.txt', 'Upstream file')
        self.assertIn('冲突', self.manager.check()['message'])
        self.assertEqual((self.root/'new-config').read_text(), 'LOCAL CONFIG')

    def test_apply_refuses_unchecked_target_busy_jobs_and_peer_service(self):
        target = self.publish()
        with self.assertRaisesRegex(UpdateError, '过期'):
            self.manager.apply(target, lambda: '', lambda: None)
        self.manager.check()
        with self.assertRaisesRegex(UpdateError, '正在生成'):
            self.manager.apply(target, lambda: '正在生成', lambda: None)
        peer = UpdateManager(self.root, self.port + 1, self.root/'production/projects/other', remote=str(self.remote))
        peer.register()
        try:
            with self.assertRaisesRegex(UpdateError, '其他服务'):
                self.manager.apply(target, lambda: '', lambda: None)
        finally:
            peer.close()
        self.assertFalse(self.manager.lock_path.exists())

    def test_preparation_failure_releases_lock_and_keeps_service_running(self):
        from unittest.mock import patch
        target = self.publish()
        self.manager.check()
        with patch('backend.updater.shutil.copyfile', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(UpdateError, '服务保持运行'):
                self.manager.apply(target, lambda: '', lambda: self.fail('Service must not stop'))
        self.assertFalse(self.manager.lock_path.exists())
        self.assertFalse(self.manager.stopping)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), self.before)

    def test_stale_status_recovers_and_new_service_requires_worker_token(self):
        write_json(self.manager.status_path, {'phase': 'updating'})
        self.assertEqual(self.manager.snapshot()['phase'], 'failed')
        write_json(self.manager.lock_path, {'operation': 'another-update', 'pid': os.getpid()})
        peer = UpdateManager(self.root, self.port + 1, self.root/'production/projects/other')
        with self.assertRaisesRegex(UpdateError, '正在更新'):
            peer.register()
        self.manager.lock_path.unlink()

    def perform_update(self, target):
        from unittest.mock import patch
        self.assertTrue(self.manager.check()['can_update'])
        spawned = []
        original_popen = subprocess.Popen
        def capture(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            spawned.append(process)
            return process
        with patch('backend.updater.subprocess.Popen', side_effect=capture):
            # close() is normally called only after the real HTTP server and its
            # executors stop. Here it marks our intentionally idle fixture stopped.
            result = self.manager.apply(target, lambda: '', self.manager.close)
        self.assertEqual(result['target_sha'], target)
        worker = next(process for process in spawned if 'worker.py' in str(process.args))
        try:
            worker.wait(timeout=40)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=5)
            self.fail('Update worker did not finish')
        return read_json(self.manager.status_path), worker.returncode

    def test_full_update_restarts_new_version_and_preserves_local_data(self):
        target = self.publish()
        files = {'.env': b'LOCAL_KEY=example-only\n',
                 'production/projects/own/project.json': b'{"own":true}',
                 'production/canvas-projects/own/video.mp4': b'LOCAL MEDIA'}
        for name, data in files.items():
            destination = self.root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        state, code = self.perform_update(target)
        self.assertEqual((code, state['phase']), (0, 'complete'), state)
        with self.opener.open(f'http://127.0.0.1:{self.port}/api/system/update', timeout=2) as response:
            self.assertEqual(json.load(response)['running_sha'], target)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), target)
        self.assertTrue((self.manager.folder/state['operation']/'before.bundle').is_file())
        for name, data in files.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        self.assertFalse(self.manager.lock_path.exists())

    def test_http_install_waits_for_jobs_then_drains_and_restarts_service(self):
        from unittest.mock import patch
        import backend.api.server as api_server
        target = self.publish()
        self.manager.check()
        listeners, workers = [], []
        original_server = api_server.ThreadingHTTPServer
        original_popen = subprocess.Popen
        def listener(*args, **kwargs):
            result = original_server(*args, **kwargs)
            listeners.append(result)
            return result
        def capture(*args, **kwargs):
            result = original_popen(*args, **kwargs)
            workers.append(result)
            return result
        def post(route, data):
            request = Request(f'http://127.0.0.1:{self.port}' + route,
                              data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
            return self.opener.open(request, timeout=5)
        with patch.object(api_server, 'UpdateManager', return_value=self.manager), \
             patch.object(api_server, 'ThreadingHTTPServer', side_effect=listener):
            thread = threading.Thread(target=api_server.serve,
                args=(self.root/'production/projects/default', self.port),
                kwargs={'canvas_root': self.root/'production/canvas-projects'})
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        with self.opener.open(f'http://127.0.0.1:{self.port}/api/system/update', timeout=.5):
                            break
                    except URLError:
                        if time.monotonic() > deadline:
                            self.fail('Test HTTP service did not start')
                        time.sleep(.05)
                project = self.root/'production/canvas-projects/own/project.json'
                write_json(project, {'jobs': [{'status': 'running'}]})
                with self.assertRaises(HTTPError) as error:
                    post('/api/system/update/apply', {'target_sha': target})
                self.assertEqual(error.exception.code, 409)
                self.assertIn('任务', json.load(error.exception)['error'])
                self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), self.before)
                write_json(project, {'jobs': []})
                saved_project = (self.root/'production/projects/default/project.json').read_bytes()
                with patch('backend.updater.subprocess.Popen', side_effect=capture):
                    with post('/api/system/update/apply', {'target_sha': target}) as response:
                        self.assertTrue(json.load(response)['operation'])
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive(), 'Original HTTP service did not stop')
                worker = next(process for process in workers if 'worker.py' in str(process.args))
                worker.wait(timeout=40)
                self.assertEqual(worker.returncode, 0, read_json(self.manager.status_path))
                self.assertEqual((self.root/'production/projects/default/project.json').read_bytes(), saved_project)
            finally:
                if thread.is_alive() and listeners:
                    listeners[0].shutdown()
                    thread.join(timeout=5)
                for process in workers:
                    if 'worker.py' in str(process.args) and process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)

    def test_failed_new_application_restores_code_and_old_service(self):
        target = self.publish('backend/__main__.py', "raise RuntimeError('broken new application')\n")
        state, code = self.perform_update(target)
        self.assertEqual((code, state['phase']), (1, 'failed'), state)
        self.assertIn('已恢复', state['message'])
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), self.before)
        with self.opener.open(f'http://127.0.0.1:{self.port}/api/system/update', timeout=2) as response:
            self.assertEqual(json.load(response)['running_sha'], self.before)


if __name__ == '__main__':
    unittest.main()
