import base64
import io
import json

import pytest
from PIL import Image

from sv import mcp_server as ms


class Bridge:
    def __init__(self, routes=None, assets=None):
        self.routes=routes or {};self.assets=assets or {};self.calls=[];self.reads=[]
    def call(self,method,path,body=None):
        self.calls.append((method,path,body));value=self.routes[(method,path)]
        if isinstance(value,Exception):raise value
        return value
    def read(self,path):
        self.reads.append(path);value=self.assets[path]
        if isinstance(value,Exception):raise value
        return value


def tool(monkeypatch,name,args,bridge=None):
    bridge=bridge or Bridge();monkeypatch.setattr(ms,'_BRIDGE',bridge)
    return bridge,ms.dispatch({'jsonrpc':'2.0','id':1,'method':'tools/call',
                              'params':{'name':name,'arguments':args}})


def text(response):
    assert not response['result'].get('isError'),response
    return json.loads(response['result']['content'][0]['text'])


def png(size=(1800,900)):
    b=io.BytesIO();Image.new('RGB',size,(100,140,180)).save(b,format='PNG');return b.getvalue()


@pytest.mark.parametrize('name,args',[
    ('rf_task_create',{'model_id':'m','input':'x','overwrite':'false'}),
    ('rf_task_create',{'model_id':'m','input':'x','codec':'hevc'}),
    ('rf_task_create',{'model_id':'m','input':'x','tile':True}),
    ('rf_task_create',{'model_id':'m','inputs':[]}),
    ('rf_tasks',{'offset':-1}),('rf_tasks',{'limit':101}),
    ('rf_trim_create',{'input':'x','start_s':0,'end_s':2,'overwrite':0}),
    ('rf_compare_create',{'kind':'image','input':'x','scale':2,'models':['a','a']}),
    ('rf_watermark_preview',{'path':'x','mask':{'width':'75'}}),
    ('rf_diagnostics',{'perf_limit':2.5}),('rf_task_preview',{'task_id':'x','sample_index':-1}),
    ('rf_watermark_preview',{'path':'x','threshold':float('nan')}),
    ('rf_tasks',[]),
    ('rf_video_watermark_preview', {'input': 'x', 'width': 320, 'height': 180, 'watermark': {'position': 'bottom'}}),
    ('rf_subtitle_preview', {'input': 'x', 'width': 320, 'height': 180, 'subtitle': {'style_mode': 'bad'}}),
    ('rf_subtitle_preview', {'input': 'x', 'width': 320, 'height': 180, 'subtitle': {}, 'time_s': float('nan')}),
    ('rf_task_create', {'input': 'x', 'extra_params': {'watermark': {'duration_s': 0}}}),
])
def test_invalid_args_are_rejected_before_side_effects(monkeypatch,name,args):
    bridge,response=tool(monkeypatch,name,args)
    assert response['error']['code']==-32602
    assert not bridge.calls and not bridge.reads


def test_codec_schema_agrees_with_backend_and_false_stays_false(monkeypatch):
    from sv.server.consts import _CODECS
    schema=next(t[2] for t in ms._TOOLS if t[0]=='rf_task_create')
    assert set(schema['properties']['codec']['enum'])==set(_CODECS)
    bridge=Bridge({('POST','/api/tasks'):{'id':'t','params':{}}})
    bridge,response=tool(monkeypatch,'rf_task_create',{
        'model_id':'m','input':'x','codec':'h265','overwrite':False},bridge)
    assert text(response)['id']=='t'
    assert bridge.calls[0][2]['overwrite'] is False


def test_task_pagination_filters_before_slicing(monkeypatch):
    rows=[{'id':str(i),'status':'done' if i%2 else 'running','params':{}} for i in range(80)]
    bridge=Bridge({('GET','/api/tasks'):rows})
    _,response=tool(monkeypatch,'rf_tasks',{'status':'done','offset':30,'limit':5},bridge)
    out=text(response)
    assert out['total_matching']==40 and out['next_offset']==35
    assert [r['id'] for r in out['tasks']]==['61','63','65','67','69']
    _,last=tool(monkeypatch,'rf_tasks',{'status':'done','offset':35,'limit':5},bridge)
    assert text(last)['has_more'] is False and 'next_offset' not in text(last)


def test_diagnostics_bounds_history_and_logs(monkeypatch):
    bridge=Bridge({('GET','/api/stats'):{'queue_gate':{'blocked':True}},
        ('GET','/api/perf/history'):{'interval_s':1,'samples':list(range(100))},
        ('GET','/api/log-tail?n=2'):{'lines':['old','a'*10000,'latest']},
        ('GET','/api/tasks/t'):{'id':'t','has_sr_log':True,'params':{}}},
        {'/api/tasks/t/sr-log?n=2':b'old\nline1\nline2'})
    _,response=tool(monkeypatch,'rf_diagnostics',{'task_id':'t','log_lines':2,'perf_limit':3},bridge)
    out=text(response)
    assert out['performance']['samples']==[97,98,99]
    assert len(out['log_lines'][0])==2000 and len(out['log_lines'])==2
    assert out['sr_log_lines']==['line1','line2']


def test_task_preview_returns_resized_native_images_and_handles_missing_side(monkeypatch):
    bridge=Bridge(assets={'/api/tasks/t/preview?src=1':png(),'/api/tasks/t/preview':ms.ApiError(404,'暂无')})
    _,response=tool(monkeypatch,'rf_task_preview',{'task_id':'t'},bridge)
    assert text(response)['missing']==['处理后']
    blocks=[c for c in response['result']['content'] if c['type']=='image']
    with Image.open(io.BytesIO(base64.b64decode(blocks[0]['data']))) as im:assert im.size==(1200,600)


def test_still_preview_waits_then_reads_corresponding_pair(monkeypatch):
    bridge=Bridge({('GET','/api/tasks/t/stills'):{'status':'building','count':4}})
    _,response=tool(monkeypatch,'rf_task_preview',{'task_id':'t','sample_index':1},bridge)
    assert text(response)['status']=='building' and not bridge.reads
    bridge.routes[('GET','/api/tasks/t/stills')]={'status':'ready','count':4}
    bridge.assets={'/api/tasks/t/stills/1?src=1':png((80,60)),'/api/tasks/t/stills/1':png((160,120))}
    _,response=tool(monkeypatch,'rf_task_preview',{'task_id':'t','sample_index':1},bridge)
    assert len([c for c in response['result']['content'] if c['type']=='image'])==2


def test_compare_workflow_and_matching_sample_assets(monkeypatch):
    job={'id':'j','kind':'video','still_count':4,'entries':[{'model_id':'m'}]}
    bridge=Bridge({('POST','/api/compare'):job,('GET','/api/compare/j'):job,
                   ('POST','/api/compare/j/cancel'):{'ok':True}},
                  {'/api/compare/j/asset/src_still/2':png((80,60)),
                   '/api/compare/j/asset/still/m/2':png((160,120))})
    args={'kind':'video','input':'x.mp4','models':['m','n'],'scale':2,'start_s':1,'end_s':7}
    _,created=tool(monkeypatch,'rf_compare_create',args,bridge)
    assert text(created)['id']=='j' and bridge.calls[0][2]==args
    _,preview=tool(monkeypatch,'rf_compare_preview',{'job_id':'j','model_id':'m','sample_index':2},bridge)
    assert len([c for c in preview['result']['content'] if c['type']=='image'])==2
    _,cancel=tool(monkeypatch,'rf_compare_cancel',{'job_id':'j'},bridge)
    assert text(cancel)['ok'] is True
    _,status=tool(monkeypatch,'rf_compare_job',{'job_id':'j'},bridge)
    assert text(status)['still_count']==4


def test_trim_create_status_and_cancel_forward_exact_parameters(monkeypatch):
    args={'input':'x.mp4','start_s':1,'end_s':7,'mode':'exact','output':'out.mp4','overwrite':False}
    bridge=Bridge({('POST','/api/trim'):{'job_id':'j','output':'out.mp4'},
                   ('GET','/api/trim/j'):{'state':'done','output':'out.mp4'},
                   ('POST','/api/trim/j/cancel'):{'ok':True}})
    _,created=tool(monkeypatch,'rf_trim_create',args,bridge)
    assert text(created)['job_id']=='j' and bridge.calls[0][2]==args
    _,status=tool(monkeypatch,'rf_trim_job',{'job_id':'j'},bridge)
    assert text(status)['state']=='done'
    _,cancel=tool(monkeypatch,'rf_trim_cancel',{'job_id':'j'},bridge)
    assert text(cancel)['ok'] is True


def test_asset_read_recovers_rotated_token_and_resets_when_offline(monkeypatch):
    b=ms._Bridge();b.base='http://localhost';b.token='old'
    seen=[]
    def read(url,token,timeout):
        seen.append(token)
        if token=='old':raise ms.ApiError(401,'bad')
        return b'image'
    def discover():b.base='http://localhost';b.token='new'
    monkeypatch.setattr(ms,'_http_read',read);monkeypatch.setattr(b,'discover',discover)
    assert b.read('/api/preview')==b'image' and seen==['old','new']
    def offline(*args):raise ms.SidecarOfflineError('offline')
    monkeypatch.setattr(ms,'_http_read',offline)
    with pytest.raises(ms.SidecarOfflineError):b.read('/api/preview')
    assert b.base is None


def test_sr_log_api_tail_and_preview_use_real_backend(monkeypatch,tmp_path):
    from fastapi.testclient import TestClient
    from sv.server.app import app
    from sv.server.routes import tasks
    source=tmp_path/'src.png';source.write_bytes(png((90,60)))
    result=tmp_path/'out.png';result.write_bytes(png((180,120)))
    monkeypatch.setattr(tasks.db,'get_task',lambda tid:{'preview_src':str(source),'preview_path':str(result)})
    monkeypatch.setattr(tasks,'SR_LOG_DIR',tmp_path)
    (tmp_path/'t.log').write_text('\n'.join(str(i) for i in range(1000)),encoding='utf8')
    client=TestClient(app)
    class ApiBridge:
        def read(self,path):
            r=client.get(path)
            if r.status_code>=400:raise ms.ApiError(r.status_code,str(r.json()))
            return r.content
    _,response=tool(monkeypatch,'rf_task_preview',{'task_id':'t'},ApiBridge())
    assert len([c for c in response['result']['content'] if c['type']=='image'])==2
    assert client.get('/api/tasks/t/sr-log?n=3').text=='997\n998\n999'
    assert client.get('/api/tasks/t/sr-log?n=0').status_code==422


def test_tail_lines_bounds_single_large_line(tmp_path):
    from sv.utils.log_tail import tail_lines
    p=tmp_path/'log';p.write_text('x'*10000,encoding='utf8')
    assert tail_lines(p,10,max_bytes=100)==['x'*100]

@pytest.mark.parametrize("args,model,scale,kind", [
    ({"input": "v.mp4"}, "animejanai-v31-hd-balanced-sharp", 2, None),
    ({"inputs": ["page.png"], "kind": "manga"}, "mangajanai", 2, "manga"),
    ({"input": "page.png", "content_type": "manga_color"}, "illustrationjanai-4x-dat2", 4, "manga"),
    ({"input": "page.png", "content_type": "manga_bw", "scale": 4}, "mangajanai", 4, "manga"),
    ({"input": "v.mp4", "model_id": "custom", "scale": 3}, "custom", 3, None),
])
def test_task_default_models(monkeypatch, args, model, scale, kind):
    bridge = Bridge({("POST", "/api/tasks"): {"id": "t", "params": {}}})
    _, response = tool(monkeypatch, "rf_task_create", args, bridge)
    assert text(response)["id"] == "t"
    body = bridge.calls[-1][2]
    assert body["model_id"] == model
    assert body["params"]["scale"] == scale
    assert body["params"].get("kind") == kind


def test_overlay_task_parameters_and_extra_override(monkeypatch):
    bridge = Bridge({('POST', '/api/tasks'): {'id': 't', 'params': {}}})
    sub = {'source': 'embedded', 'stream': 0, 'style_mode': 'custom', 'outline': 4}
    wm = {'kind': 'text', 'text': '雨帧\n制作'}
    _, response = tool(monkeypatch, 'rf_task_create', {
        'input': 'x.mkv', 'subtitle_mode': 'burn', 'subtitle': sub, 'watermark': wm,
        'extra_params': {'subtitle_mode': 'burn_keep'}}, bridge)
    assert text(response)['id'] == 't'
    params = bridge.calls[0][2]['params']
    assert params['subtitle'] == sub and params['watermark'] == wm
    assert params['subtitle_mode'] == 'burn_keep'


def test_overlay_preview_auth_rotation_and_offline(monkeypatch):
    bridge = ms._Bridge(); bridge.base = 'http://localhost'; bridge.token = 'old'
    seen = []
    def read(url, token, timeout, body, metadata):
        seen.append((token, body, metadata))
        if token == 'old': raise ms.ApiError(401, 'expired')
        return png(), {'time_s': .2, 'warnings': ['字体替代']}
    def discover(): bridge.base = 'http://localhost'; bridge.token = 'new'
    monkeypatch.setattr(ms, '_http_read', read)
    monkeypatch.setattr(bridge, 'discover', discover)
    args = {'input': 'x.mp4', 'width': 320, 'height': 180, 'watermark': {'text': '雨帧'}}
    _, response = tool(monkeypatch, 'rf_video_watermark_preview', args, bridge)
    assert text(response)['warnings'] == ['字体替代']
    assert [item[0] for item in seen] == ['old', 'new']
    assert seen[-1][1]['time_s'] == .2 and seen[-1][2]
    def offline(*args): raise ms.SidecarOfflineError('offline')
    monkeypatch.setattr(ms, '_http_read', offline)
    _, response = tool(monkeypatch, 'rf_video_watermark_preview', args, bridge)
    assert response['result']['isError'] and bridge.base is None


def test_overlay_http_posts_json_and_reads_image_metadata(monkeypatch):
    from email.message import Message
    headers = Message()
    headers['X-Subtitle-Time'] = '.200'
    headers['X-Subtitle-Warnings'] = json.dumps(['字体替代'], ensure_ascii=True)
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return png((320, 180))
    response = Response(); response.headers = headers
    class Opener:
        def open(self, req, timeout):
            assert req.get_method() == 'POST'
            assert req.get_header('Content-type') == 'application/json'
            assert req.get_header('X-sv-token') == 'token'
            assert json.loads(req.data)['watermark']['text'] == '雨帧\n制作'
            return response
    monkeypatch.setattr(ms, '_OPENER', Opener())
    data, metadata = ms._http_read('http://localhost/api/subtitles/preview', 'token', 60,
                                  {'watermark': {'text': '雨帧\n制作'}}, True)
    assert data.startswith(b'\x89PNG') and metadata == {'time_s': .2, 'warnings': ['字体替代']}


def test_overlay_tools_render_real_backend(monkeypatch, tmp_path):
    import numpy as np
    from test_subtitle_burn import ffmpeg
    from fastapi.testclient import TestClient
    from sv.server.app import app
    from sv.server.routes import models
    monkeypatch.setattr(models, 'TEMP_DIR', tmp_path)
    video = tmp_path / 'input.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'color=c=0x304050:s=320x180:r=24', '-t', '2',
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video))
    sub = tmp_path / 'test.srt'
    sub.write_text('1\n00:00:00,500 --> 00:00:01,500\nSubtitle\n', encoding='utf-8')
    client = TestClient(app)
    class ApiBridge:
        def preview(self, body):
            response = client.post('/api/subtitles/preview', json=body)
            if response.status_code >= 400: raise ms.ApiError(response.status_code, str(response.json()))
            return response.content, {'time_s': float(response.headers['X-Subtitle-Time']),
                                      'warnings': json.loads(response.headers['X-Subtitle-Warnings'])}
    args = {'input': str(video), 'width': 320, 'height': 180,
            'subtitle': {'source': 'external', 'path': str(sub), 'font_size': 96},
            'watermark': {'kind': 'text', 'text': 'RainFrame', 'font_size': 96, 'duration_s': .4}}
    for name, extra, top, bottom in [
        ('rf_video_watermark_preview', {}, True, False),
        ('rf_subtitle_preview', {}, False, True),
        ('rf_video_watermark_preview', {'time_s': .6, 'watermark': {**args['watermark'], 'duration_s': 1}}, True, True),
    ]:
        _, response = tool(monkeypatch, name, {**args, **extra}, ApiBridge())
        metadata = text(response)
        block = response['result']['content'][1]
        with Image.open(io.BytesIO(base64.b64decode(block['data']))) as image:
            pixels = np.array(image.convert('RGB'))
        assert (np.count_nonzero(pixels[:70] > 180) > 30) == top
        assert (np.count_nonzero(pixels[100:] > 180) > 30) == bottom
        assert 'warnings' in metadata
    _, response = tool(monkeypatch, 'rf_subtitle_preview', {**args, 'subtitle': {**args['subtitle'], 'path': 'missing.srt'}}, ApiBridge())
    assert response['result']['isError']
