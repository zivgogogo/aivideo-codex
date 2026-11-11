from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import imageio_ffmpeg
from PIL import Image
from backend.canvas.workspace import node
from backend.canvas.studio import StudioCanvas
from ai.video import DashScopeVideoClient, build_multimodal_reference_payload
from ai.video.payloads import WAN_REFERENCE_MODEL


class MediaUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture=tempfile.TemporaryDirectory();cls.video=Path(cls.fixture.name)/'sample.mp4'
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-v','error','-f','lavfi','-i','color=c=blue:s=256x256:r=12',
                        '-t','2','-c:v','libx264','-pix_fmt','yuv420p',str(cls.video)],check=True,capture_output=True)
        cls.raw=cls.video.read_bytes()

    @classmethod
    def tearDownClass(cls):cls.fixture.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.ws=StudioCanvas(self.temp.name)
        self.pid=self.ws.create({'mode':'blank'})['project_id'];self.eid='CANVAS001'
        g=self.ws.graph(self.pid,self.eid)
        g['nodes']=[node('prompt',400,200,'Prompt卡','人物转头，镜头缓慢推进。'),node('image',30,200,'图片卡'),
                    node('video',30,700,'视频卡'),node('video',800,200,'视频卡'),node('image',800,700,'图片卡')]
        self.p,self.im,self.source,self.out,self.image_out=g['nodes']
        self.out['settings']['model']='auto'
        g['edges']=[dict(source=self.source['id'],target=self.p['id']),dict(source=self.im['id'],target=self.p['id']),
                    dict(source=self.p['id'],target=self.out['id']),dict(source=self.p['id'],target=self.image_out['id'])]
        self.g=self.ws.save_graph(self.pid,self.eid,g)
        image=io.BytesIO();Image.new('RGB',(256,256),'red').save(image,'PNG');raw=image.getvalue()
        self.ws.upload_media(self.pid,self.eid,self.im['id'],io.BytesIO(raw),len(raw),'frame.png')

    def tearDown(self):self.ws.pool.shutdown(wait=True);self.temp.cleanup()

    def upload(self):
        return self.ws.upload_media(self.pid,self.eid,self.source['id'],io.BytesIO(self.raw),len(self.raw),'参考视频.mp4')

    def test_upload_is_local_playable_and_compile_keeps_video_image_and_prompt(self):
        asset=self.upload();self.assertTrue(self.ws.asset_path(self.pid,asset).is_file())
        self.assertEqual(asset['kind'],'video');self.assertEqual(asset['duration'],2)
        with patch.object(DashScopeVideoClient,'upload_file',side_effect=AssertionError('preview must not upload')):
            c=self.ws.compile(self.pid,self.eid,self.out['id'])
        self.assertEqual(c['route'],'wan-multimodal-reference')
        self.assertEqual([m['type'] for m in c['payload']['input']['media']],['reference_image','reference_video'])
        self.assertIn('人物转头',c['prompt']);self.assertEqual(len(c['local_videos']),1)

    def test_video_input_blocks_image_before_missing_media_checks(self):
        with self.assertRaisesRegex(ValueError,'只能生成视频'):
            self.ws.compile(self.pid,self.eid,self.image_out['id'])

    def test_reject_truncated_corrupt_wrong_type_and_oversize_without_adopting(self):
        cases=[(io.BytesIO(b'bad'),3,'bad.mp4'),(io.BytesIO(self.raw[:-10]),len(self.raw),'short.mp4'),
               (io.BytesIO(b''),101*1024*1024,'large.mp4')]
        for stream,length,name in cases:
            with self.assertRaises(ValueError):self.ws.upload_media(self.pid,self.eid,self.source['id'],stream,length,name)
        with self.assertRaisesRegex(ValueError,'对应'):
            self.ws.upload_media(self.pid,self.eid,self.im['id'],io.BytesIO(self.raw),len(self.raw),'video.mp4')
        n=next(n for n in self.ws.graph(self.pid,self.eid)['nodes'] if n['id']==self.source['id'])
        self.assertFalse(n['assets']);self.assertFalse(list(self.ws.path(self.pid).glob('assets/*.part')))

    def test_generate_uploads_before_submission_and_reuses_model_bound_cache(self):
        self.upload();client=Mock()
        client.upload_file.return_value='oss://dashscope-instant/test/video.mp4'
        client.submit.return_value={'output':{'task_id':'task-1'}}
        client.wait_for_task.return_value={'output':{'video_url':'https://example.com/result.mp4'}}
        client.download_video.side_effect=lambda url,path:Path(path).write_bytes(self.raw)
        def wait(result):
            for _ in range(300):
                j=next(j for j in self.ws.read(self.pid)['jobs'] if j['id']==result['job_id'])
                if j['status'] not in {'queued','running'}:return j
                threading.Event().wait(.01)
            self.fail('job did not finish')
        with patch('backend.canvas.studio.DashScopeVideoClient',return_value=client):
            first=wait(self.ws.generate(self.pid,self.eid,self.out['id']))
            self.assertEqual(first['status'],'succeeded',first.get('error'))
            second=wait(self.ws.generate(self.pid,self.eid,self.out['id'],force=True))
            self.assertEqual(second['status'],'succeeded',second.get('error'))
        self.assertEqual(client.upload_file.call_count,1)
        self.assertEqual(client.submit.call_count,2)
        self.assertEqual(client.submit.call_args.args[0]['input']['media'][-1]['url'],client.upload_file.return_value)

    def test_temporary_storage_upload_uses_stream_and_private_model_policy(self):
        client=DashScopeVideoClient(api_key='test-only')
        policy=dict(upload_host='https://test.oss-cn-beijing.aliyuncs.com',upload_dir='dashscope-instant/test',
                    oss_access_key_id='upload-only',policy='policy',signature='signature',x_oss_object_acl='private',x_oss_forbid_overwrite='true')
        response=io.BytesIO();response.status=200
        with patch.object(client,'_request',return_value={'data':policy}) as request,patch('ai.video.client.urlopen',return_value=response) as upload:
            url=client.upload_file(self.video,WAN_REFERENCE_MODEL)
            storage_request=upload.call_args.args[0];body=b''.join(storage_request.data)
        self.assertTrue(url.startswith('oss://dashscope-instant/test/'))
        self.assertIn('model=wan2.7-r2v-2026-06-12',request.call_args.args[1])
        self.assertNotIn('Authorization',dict(storage_request.header_items()))
        self.assertIn(b'private',body);self.assertIn(self.raw,body)

    def test_oss_payload_adds_resolve_header_on_submission(self):
        payload=build_multimodal_reference_payload('人物转头',reference_videos=['oss://dashscope-instant/test/input.mp4'],duration=3)
        response=io.BytesIO(json.dumps({'output':{'task_id':'task-test'}}).encode());response.status=200
        with patch('ai.video.client.urlopen',return_value=response) as send:
            DashScopeVideoClient(api_key='test-only').submit(payload)
        headers={k.lower():v for k,v in send.call_args.args[0].header_items()}
        self.assertEqual(headers['x-dashscope-ossresourceresolve'],'enable')

    @unittest.skipUnless(shutil.which('node'),'Node required')
    def test_frontend_video_dependency_follows_prompts_and_mentions_but_stops_at_images(self):
        module=Path(__file__).resolve().parents[2]/'frontend/canvas/canvas-sync.js'
        script=r'''
const assert=require('node:assert/strict'),sync=require(process.argv[1]);
const g={nodes:[{id:'v1',type:'video',ref_name:'视频1',title:'甲'},{id:'v10',type:'video',ref_name:'视频10',title:'乙'},
 {id:'p1',type:'prompt',text:''},{id:'p2',type:'prompt',text:''},{id:'im',type:'image',text:''}],
 edges:[{source:'v1',target:'p1'},{source:'p1',target:'p2'},{source:'p2',target:'im'}]};
assert.deepEqual(sync.videoReferences(g,'p2'),['v1']);assert.deepEqual(sync.videoReferences(g,'im'),['v1']);
g.edges=[];g.nodes[2].text='参考@视频10';assert.deepEqual(sync.videoReferences(g,'p1'),['v10']);
g.edges=[{source:'v1',target:'im'},{source:'im',target:'p2'}];assert.deepEqual(sync.videoReferences(g,'p2'),[]);
'''
        subprocess.run(['node','-e',script,str(module)],check=True,capture_output=True,text=True)
