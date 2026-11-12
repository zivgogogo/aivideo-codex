"""Exercise project files with Windows GBK defaults, without paid AI requests."""
from contextlib import contextmanager
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from backend.canvas import director
from backend.canvas.studio import StudioCanvas
from backend.canvas.workspace import CanvasWorkspace, node
from core.store import Store, write_json


@contextmanager
def gbk_file_defaults():
    """Keep explicit encodings; emulate GBK only when callers omit one.

    pathlib may pass the literal 'locale' instead of None to io.open.
    """
    original = io.open

    def open_file(file, mode='r', buffering=-1, encoding=None, errors=None,
                  newline=None, closefd=True, opener=None):
        if 'b' not in mode and encoding in {None, 'locale'}:
            encoding = 'gbk'
        return original(file, mode, buffering, encoding, errors, newline, closefd, opener)

    def default_encoding(encoding, stacklevel=2):
        return 'gbk' if encoding in {None, 'locale'} else encoding

    with patch('io.text_encoding', side_effect=default_encoding), \
         patch('io.open', side_effect=open_file):
        yield


class WindowsEncodingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / '中文项目'
        self.workspaces = []

    def tearDown(self):
        for workspace in self.workspaces:
            workspace.pool.shutdown(wait=True)
        self.temporary.cleanup()

    def workspace(self, kind=StudioCanvas, legacy=None):
        workspace = kind(self.root, legacy)
        self.workspaces.append(workspace)
        return workspace

    def test_blank_project_create_list_edit_and_reopen_under_gbk(self):
        with gbk_file_defaults():
            # Prove implicit locale reads really differ from UTF-8 in this test.
            self.root.mkdir(parents=True)
            probe = self.root / 'encoding-probe.txt'
            probe.write_bytes('一'.encode('utf-8'))
            with self.assertRaises(UnicodeDecodeError):
                probe.read_text()
            workspace = self.workspace()
            pid = workspace.create({'mode': 'blank', 'title': '一键新项目🎬',
                                    'style': '电影质感，窗边自然光'})['project_id']
            self.assertEqual(workspace.projects()[0]['title'], '一键新项目🎬')
            self.assertEqual(workspace.detail(pid)['episodes'][0]['title'], '自由画布')
            graph = workspace.graph(pid, 'CANVAS001')
            graph['nodes'].append(node('image', 40, 80, '一张图片', '一位人物站在窗边🎬'))
            workspace.save_graph(pid, 'CANVAS001', graph)
            reopened = self.workspace()
            self.assertEqual(reopened.graph(pid, 'CANVAS001')['nodes'][0]['text'], '一位人物站在窗边🎬')
            self.assertEqual(reopened.read(pid)['style'], '电影质感，窗边自然光')
            persisted = json.loads((workspace.path(pid) / 'project.json').read_bytes().decode('utf-8'))
            self.assertEqual(persisted['title'], '一键新项目🎬')

    def test_imported_project_preserves_chinese_script_under_gbk(self):
        source = '第一集：一封来信\n林夏：你来了。🎬\n第二集：再见\n林夏：一路平安。\n'
        with gbk_file_defaults():
            workspace = self.workspace(CanvasWorkspace)
            with patch('backend.canvas.workspace.llm_client') as client:
                result = workspace.create({'title': '一封来信', 'script': source})
                deadline = time.monotonic() + 5
                while True:
                    data = workspace.read(result['project_id'])
                    if data['jobs'][-1]['status'] not in {'queued', 'running'}:
                        break
                    self.assertLess(time.monotonic(), deadline, 'Import did not finish')
                    time.sleep(.01)
                self.assertEqual(data['jobs'][-1]['status'], 'succeeded', data['jobs'][-1])
                self.assertEqual(''.join(ep['source_text'] for ep in data['episodes']), source)
                client.assert_not_called()

    def test_character_and_episode_caches_reuse_chinese_results_under_gbk(self):
        with gbk_file_defaults():
            self.root.mkdir(parents=True)
            character_client = Mock()
            character_client.generate.return_value = json.dumps({
                'visual_bible': {'palette': '一抹暖色🎬'},
                'characters': [{'name': '林夏', 'description': '一位演员',
                                'prompt': '一位年轻女性，黑色长发，米色针织衫，正面自然站姿，窗边柔和自然光，无字无水印。',
                                'episode_ids': ['EP001']}]}, ensure_ascii=False)
            episodes = [{'id': 'EP001', 'source_text': '林夏：你来了。'}]
            first = director.plan_characters(episodes, self.root/'characters', character_client)
            self.assertEqual(director.plan_characters(episodes, self.root/'characters', character_client), first)
            self.assertEqual(character_client.generate.call_count, 1)
            episode_client = Mock()
            shot = {key: '一位人物，窗边自然光🎬' for key in
                    ['subject', 'action', 'scene', 'framing', 'camera', 'lighting', 'emotion', 'continuity_in', 'continuity_out']}
            shot.update(duration=6, character_ids=['C001'])
            episode_client.generate.return_value = json.dumps({'clips': [{
                'title': '一封来信', 'duration': 6, 'beat_ids': ['B0001'], 'shots': [shot]}]}, ensure_ascii=False)
            arguments = ('林夏：你来了。', first[0], first[1], self.root/'episode', episode_client)
            planned = director.plan_episode(*arguments)
            self.assertEqual(director.plan_episode(*arguments), planned)
            self.assertEqual(episode_client.generate.call_count, 1)

    def test_history_restore_and_legacy_response_read_under_gbk(self):
        with gbk_file_defaults():
            legacy = Store(self.root/'legacy')
            workspace = self.workspace(legacy=legacy)
            pid = workspace.create({'mode': 'blank', 'title': '一键历史🎬'})['project_id']
            image = node('image', 40, 80, '一张图片', '一封旧信🎬')
            workspace.mutate(pid, lambda data: data['graphs']['CANVAS001']['nodes'].append(image))
            item = workspace.mutate(pid, lambda data: workspace._archive(
                data, 'CANVAS001', data['graphs']['CANVAS001'], '手动保存'))
            workspace.mutate(pid, lambda data: data['graphs']['CANVAS001']['nodes'][0].update(text='新内容'))
            workspace.restore(pid, 'CANVAS001', item['id'])
            self.assertEqual(workspace.graph(pid, 'CANVAS001')['nodes'][0]['text'], '一封旧信🎬')
            write_json(legacy.root/'assets/video/response.json', {
                'note': '一段历史视频🎬', 'output': {'video_url': 'https://cdn.example/video.mp4'}})
            self.assertEqual(workspace.remote_video(pid, {'legacy_path': 'assets/video/video.mp4'}),
                             'https://cdn.example/video.mp4')


if __name__ == '__main__':
    unittest.main()
