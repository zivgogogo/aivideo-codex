import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
import zipfile

from backend.canvas.workspace import CanvasWorkspace, graph_from_shots, read_script, split_script
from core.store import now, Store


class CanvasTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.workspace=CanvasWorkspace(Path(self.temp.name)/'canvas')
        self.pid='test'
        self.workspace.save(dict(id=self.pid,title='测试短剧',style='电影质感',ratio='9:16',status='ready',
            created_at=now(),episodes=[dict(id='EP001',title='第一集',source_text='小林：你好。'),dict(id='EP002',title='第二集',source_text='第二集正文')],
            graphs={},jobs=[]))
        self.graph=graph_from_shots([dict(prompt='林间咖啡馆，窗边一杯拿铁，阳光穿过树叶。',duration=3)])
        self.workspace.mutate(self.pid,lambda d:d['graphs'].update(EP001=self.graph))
        self.p,self.im,self.v=self.graph['nodes']

    def tearDown(self):
        self.workspace.pool.shutdown(wait=True)
        self.temp.cleanup()

    def wait(self,result):
        with self.workspace.lock:
            futures=[]
        # Submit a barrier to the executor; actual completion checked by a bounded event.
        for _ in range(200):
            job=next(j for j in self.workspace.read(self.pid)['jobs'] if j['id']==result['job_id'])
            if job['status'] not in {'queued','running'}:return job
            threading.Event().wait(.01)
        self.fail('job did not complete')

    def test_split_headings_preserve_all_source_and_preface(self):
        text='故事名\n人物介绍\n\n第一集\n林：你好\n\n第二集：重逢\n林：再见\n'
        episodes,method=split_script(text,client_factory=Mock(side_effect=AssertionError('must not call AI')))
        self.assertEqual(len(episodes),2)
        self.assertEqual(''.join(e['source_text'] for e in episodes),text)
        self.assertIn('故事名',episodes[0]['source_text'])

    def test_ai_split_only_chooses_boundaries(self):
        client=Mock();client.generate.return_value='{"episodes":[{"start_line":1,"title":"相遇"},{"start_line":3,"title":"离开"}]}'
        text='他推开门。\n林：你来了。\n她走出门。\n'
        eps,_=split_script(text,lambda:client)
        self.assertEqual(len(eps),2)
        self.assertEqual(''.join(e['source_text'] for e in eps),text)
        client.generate.return_value='{"episodes":[{"start_line":2}]}'
        with self.assertRaisesRegex(ValueError,'边界'):split_script(text,lambda:client)

    def test_docx_import_uses_text_without_running_embedded_content(self):
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as f:f.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>第一集</w:t></w:r></w:p><w:p><w:r><w:t>台词：你好</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(read_script(dict(filename='剧本.docx',file_data=base64.b64encode(out.getvalue()).decode())),'第一集\n台词：你好')

    def test_graph_rejects_invalid_edges_conflicts_and_forged_assets(self):
        forged=deepcopy(self.graph);forged['nodes'][1]['assets']=[dict(id='secret',path='../../.env')]
        result=self.workspace.save_graph(self.pid,'EP001',forged)
        self.assertEqual(result['nodes'][1]['assets'],[])
        with self.assertRaisesRegex(ValueError,'其他窗口'):self.workspace.save_graph(self.pid,'EP001',forged)
        forged['revision']=1;forged['edges']=[dict(source=self.v['id'],target=self.p['id'])]
        with self.assertRaisesRegex(ValueError,'仅支持'):self.workspace.save_graph(self.pid,'EP001',forged)
        forged['edges']=[];forged['nodes'][0]['x']=float('nan')
        with self.assertRaisesRegex(ValueError,'坐标'):self.workspace.save_graph(self.pid,'EP001',forged)

    def test_delete_undo_keeps_immutable_assets(self):
        self.workspace.mutate(self.pid,lambda d:d['graphs']['EP001']['nodes'][1].update(assets=[dict(id='keep',path='assets/x.png')],selected='keep'))
        graph=self.workspace.graph(self.pid,'EP001');original=deepcopy(graph)
        graph['nodes']=[n for n in graph['nodes'] if n['id']!=self.im['id']];graph['edges']=[]
        saved=self.workspace.save_graph(self.pid,'EP001',graph)
        original['revision']=saved['revision'];restored=self.workspace.save_graph(self.pid,'EP001',original)
        self.assertEqual(restored['nodes'][1]['selected'],'keep')

    def test_other_episode_job_does_not_block_saving(self):
        self.workspace.mutate(self.pid,lambda d:d['jobs'].append(dict(id='j',status='running',episode_id='EP002',node_id='other',kind='image')))
        self.workspace.save_graph(self.pid,'EP001',self.graph)

    def test_image_generation_and_download_resume_never_resubmit(self):
        response={'output':{'choices':[{'message':{'content':[{'image':'https://cdn.example/result.png'}]}}]}}
        with patch('backend.canvas.workspace.generate_image',return_value=response) as generate,patch('backend.canvas.workspace.download_image',side_effect=RuntimeError('download failed')):
            result=self.workspace.generate(self.pid,'EP001',self.im['id']);job=self.wait(result)
            self.assertEqual(job['status'],'failed');self.assertEqual(generate.call_count,1)
        def download(url,path):Path(path).write_bytes(b'PNG')
        with patch('backend.canvas.workspace.generate_image') as generate,patch('backend.canvas.workspace.download_image',side_effect=download):
            result=self.workspace.generate(self.pid,'EP001',self.im['id']);self.assertEqual(self.wait(result)['status'],'succeeded');generate.assert_not_called()
        graph=self.workspace.graph(self.pid,'EP001');self.assertEqual(len(graph['nodes'][1]['assets']),1)
        with self.assertRaisesRegex(ValueError,'调用记录'):self.workspace.generate(self.pid,'EP001',self.im['id'])

    def test_generate_deduplicates_and_disallows_deleting_busy_node(self):
        gate=threading.Event()
        def block(*a,**kw):gate.wait(2);raise RuntimeError('fake failure')
        try:
            with patch('backend.canvas.workspace.generate_image',side_effect=block) as generate:
                a=self.workspace.generate(self.pid,'EP001',self.im['id']);b=self.workspace.generate(self.pid,'EP001',self.im['id'])
                self.assertEqual(a,b)
                g=deepcopy(self.graph);g['nodes']=[self.p,self.v];g['edges']=[e for e in g['edges'] if e['source']==self.p['id'] and e['target']==self.v['id']]
                with self.assertRaisesRegex(ValueError,'生成中'):self.workspace.save_graph(self.pid,'EP001',g)
                gate.set();self.wait(a);self.assertEqual(generate.call_count,1)
        finally:gate.set()

    def add_frame(self):
        from PIL import Image
        image=io.BytesIO();Image.new('RGB',(64,64),'blue').save(image,'PNG')
        return self.workspace.upload(self.pid,'EP001',self.im['id'],dict(data=base64.b64encode(image.getvalue()).decode()))

    def test_video_receives_frame_and_prompt_and_task_id_is_durable(self):
        self.add_frame();client=Mock();client.submit.return_value={'output':{'task_id':'remote-123'}}
        def wait(taskid,**kwargs):
            job=self.workspace.read(self.pid)['jobs'][-1]
            self.assertEqual(job['task_id'],'remote-123')
            return {'output':{'task_status':'SUCCEEDED','video_url':'https://cdn.example/result.mp4'}}
        client.wait_for_task.side_effect=wait;client.download_video.side_effect=lambda url,path:Path(path).write_bytes(b'video')
        with patch('backend.canvas.workspace.DashScopeVideoClient',return_value=client):
            result=self.workspace.generate(self.pid,'EP001',self.v['id']);self.assertEqual(self.wait(result)['status'],'succeeded')
        payload=client.submit.call_args.args[0]
        self.assertEqual(payload['model'],'happyhorse-1.1-i2v')
        self.assertTrue(payload['input']['media'][0]['url'].startswith('data:image/png;base64,'))
        self.assertIn('林间咖啡馆',payload['input']['prompt'])

    def test_video_resume_queries_existing_remote_task(self):
        self.add_frame();client=Mock();client.submit.return_value={'output':{'task_id':'remote-existing'}};client.wait_for_task.side_effect=TimeoutError('pending')
        with patch('backend.canvas.workspace.DashScopeVideoClient',return_value=client):
            result=self.workspace.generate(self.pid,'EP001',self.v['id']);self.assertEqual(self.wait(result)['status'],'failed')
            client.submit.reset_mock();client.wait_for_task.side_effect=None;client.wait_for_task.return_value={'output':{'video_url':'https://cdn.example/v.mp4'}}
            result=self.workspace.generate(self.pid,'EP001',self.v['id']);self.assertEqual(self.wait(result)['status'],'succeeded');client.submit.assert_not_called()

    def test_text_to_video_and_missing_upstream_validation(self):
        with self.assertRaisesRegex(ValueError,'尚未生成'):self.workspace.generate(self.pid,'EP001',self.v['id'])
        g=deepcopy(self.graph);g['edges']=[e for e in g['edges'] if e['source']!=self.im['id']]
        self.workspace.save_graph(self.pid,'EP001',g)
        client=Mock();client.submit.return_value={'output':{'task_id':'t2v'}};client.wait_for_task.return_value={'output':{'video_url':'https://cdn.example/v.mp4'}}
        with patch('backend.canvas.workspace.DashScopeVideoClient',return_value=client):
            result=self.workspace.generate(self.pid,'EP001',self.v['id']);self.assertEqual(self.wait(result)['status'],'succeeded')
        self.assertEqual(client.submit.call_args.args[0]['model'],'happyhorse-1.1-t2v')

    def test_path_traversal_and_legacy_read_only(self):
        for pid in ('../secret','a/b','', '/tmp/a'):
            with self.assertRaises(ValueError):self.workspace.path(pid)
        legacyroot=Path(self.temp.name)/'legacy';legacyroot.mkdir()
        state=dict(project=dict(title='原项目'),episodes=[dict(id='EP001',shots=[],source_text='原文')])
        path=legacyroot/'project.json';path.write_text(json.dumps(state))
        before=path.read_bytes();self.workspace.legacy=Store(legacyroot)
        project=next(p for p in self.workspace.projects() if p['legacy'])
        self.workspace.graph(project['id'],'EP001')
        self.assertEqual(path.read_bytes(),before)

    def test_restart_marks_inflight_jobs_without_repeating_calls(self):
        self.workspace.mutate(self.pid,lambda d:(d.update(status='initializing'),d['jobs'].append(dict(id='init',kind='initialize',status='running'))))
        other=CanvasWorkspace(self.workspace.root)
        try:
            self.assertEqual(other.read(self.pid)['jobs'][-1]['status'],'interrupted')
            self.assertEqual(other.read(self.pid)['status'],'failed')
        finally:other.pool.shutdown(wait=True)

    def test_empty_workbench_is_not_listed_as_an_imported_script(self):
        store = Store(Path(self.temp.name) / 'empty-workbench')
        store.initialize()
        before = store.path.read_bytes()
        workspace = CanvasWorkspace(Path(self.temp.name) / 'empty-canvas', store)
        try:
            self.assertEqual(workspace.projects(), [])
            self.assertEqual(store.path.read_bytes(), before)
        finally:
            workspace.pool.shutdown(wait=True)


if __name__=='__main__':unittest.main()
