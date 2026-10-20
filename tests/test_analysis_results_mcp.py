"""Real loopback MCP protocol tests; fixtures or optional read-only JLab SSH."""
import base64
from contextlib import redirect_stdout
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'lqcdmcp-deploy'
sys.path.insert(0, str(APP))
from jamai import analysis_results as ar


class ResultsMCPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='femtomind-results-')
        cls.sandbox = Path(cls.temp.name)
        cls.real = os.environ.get('JAM_RESULTS_REAL_SSH') == '1'
        cls.token = secrets.token_hex(40)
        cls.env = patch.dict(os.environ, {'JAM_GW_OWNER': 'resultstest', 'JAM_GW_USER': 'resultstest',
            'JAM_GW_TEST_TOKEN': cls.token, 'LQCDMCP_TOKEN': cls.token, 'JAM_GW_PLUGINS': 'none',
            'JAM_GW_ACCESS_DIR': str(cls.sandbox/'access'), 'JAM_GW_SITE_FILE': str(cls.sandbox/'no-site'),
            'SLURM_JOB_ID': '', 'PYTHONDONTWRITEBYTECODE': '1'})
        cls.env.start()
        cls.gw = importlib.import_module('jam_gateway')
        cls.gw.TEST_TOKEN = cls.token
        cls.gw.GATEWAY_OWNER = 'resultstest'
        cls.gw.ACCESS_DIR = str(cls.sandbox/'access')
        cls.gw.HTTPLOG = str(cls.sandbox/'http.log')
        cls.expected = json.loads((ROOT/'reference_results/JAM24/fit_statistics.json').read_bytes())
        cls.manifest = json.loads((ROOT/'reference_results/JAM24/reference_manifest.json').read_bytes())
        cls.remote_commands, cls.reads, cls.sizes, cls.samples = [], [], {}, {}
        cls.fixture = cls.sandbox/'reference_results/JAM24/data'/cls.manifest['archive_id']
        cls.fixture.mkdir(parents=True)
        cls.local_manifest = dict(cls.manifest, server_directory=cls.fixture.as_posix()[2:] if os.name == 'nt' else cls.fixture.as_posix())
        # Synthetic arrays are protocol fixtures, never presented as JAM24 predictions.
        n = 11
        cls.theory = np.linspace(0.123456789012345, 2, n)
        (cls.fixture/'means').mkdir(); (cls.fixture/'datasets').mkdir()
        np.savez(cls.fixture/'means/wc_10002.npz', thy=cls.theory, value=cls.theory+0.1,
                 alpha=np.ones(n), source_row=np.arange(n), prediction_std=np.ones(n)*0.2,
                 shift=np.zeros(n), shift_std=np.zeros(n), point_residual=np.ones(n)*-0.1)
        np.savez(cls.fixture/'datasets/wc_10002.npz', value=cls.theory+0.1, alpha=np.ones(n),
                 archive_source_row=np.arange(n), y=np.arange(n)/10, units=np.full(n, np.nan),
                 tag=np.array(['null']*n), __archive_json_columns__=json.dumps(['tag']))
        (cls.fixture/'summary.json').write_text(json.dumps(cls.expected), encoding='utf-8')
        inv = {'archive_id': cls.manifest['archive_id'], 'catalog_ref': cls.manifest['catalog_ref'], 'files': {}}
        for p in cls.fixture.rglob('*'):
            if p.is_file(): inv['files'][p.relative_to(cls.fixture).as_posix()] = {'bytes': p.stat().st_size, 'sha256': ar.digest(p.read_bytes())}
        raw = json.dumps(inv).encode(); (cls.fixture/'manifest.json').write_bytes(raw)
        cls.local_manifest['archive_manifest_sha256'] = ar.digest(raw)
        if not cls.real:
            (cls.sandbox/'reference_results/JAM24/reference_manifest.json').write_text(json.dumps(cls.local_manifest))
        cls.patches = [patch.object(cls.gw, '_run_ifarm_py', side_effect=cls.worker)]
        if not cls.real:
            cls.patches.append(patch.object(cls.gw, '_analysis_results_store', side_effect=lambda: ar.Results(cls.sandbox/'reference_results')))
            cls.patches.append(patch.dict(cls.gw.SITE, {'read_roots':[cls.local_manifest['server_directory']]}))
        for name in ('_dashboard_identify', '_remote', '_run', 'register'):
            cls.patches.append(patch.object(cls.gw, name, side_effect=AssertionError('Forbidden production operation')))
        for p in cls.patches: p.start()
        cls.server = cls.gw.ThreadingHTTPServer(('127.0.0.1', 0), cls.gw.H)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.url = 'http://127.0.0.1:%s/mcp' % cls.server.server_port

    @classmethod
    def worker(cls, user, code, **kwargs):
        cls.remote_commands.append(code)
        if cls.real:
            cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'ProxyCommand=none',
                   '-o', 'ProxyJump=none', '-p', '2201', '-i', 'C:/Users/treya/.ssh/id_ed25519_codex_ifarm',
                   'treyand@127.0.0.1', 'python3 -B -']
            r = subprocess.run(cmd, input=code, text=True, capture_output=True, timeout=65)
            return {'rc': r.returncode, 'out': r.stdout, 'err': r.stderr}
        out = io.StringIO()
        read = Path.read_bytes
        def logged(p):
            cls.reads.append(str(p))
            return read(p)
        with redirect_stdout(out), patch.object(Path, 'read_bytes', logged): exec(code, {'__name__': 'results_worker'})
        return {'rc': 0, 'out': out.getvalue(), 'err': ''}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=5)
        for p in reversed(cls.patches): p.stop()
        cls.env.stop()
        if os.environ.get('RESULTS_MCP_TEST_REPORT'):
            Path(os.environ['RESULTS_MCP_TEST_REPORT']).write_text(json.dumps({'real_jlab_reads': cls.real,
                'http_response_bytes': cls.sizes, 'samples': cls.samples, 'remote_requests': len(cls.remote_commands)}, indent=2))
        cls.temp.cleanup()

    @classmethod
    def rpc(cls, method, params, auth=True):
        headers = {'Content-Type':'application/json'}
        if auth: headers['Authorization'] = 'Bearer '+cls.token
        req = urllib.request.Request(cls.url, data=json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params}).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=75) as r: raw = r.read()
        return json.loads(raw)['result'], len(raw)

    @classmethod
    def call(cls, name, args=None, error=False):
        result, size = cls.rpc('tools/call', {'name':name, 'arguments':args or {}})
        if error:
            assert result['isError'], result
            return result
        assert not result['isError'], result
        assert size <= ar.MAX_RESPONSE_BYTES, size
        cls.sizes[name] = max(size, cls.sizes.get(name, 0))
        return result['structuredContent']

    @classmethod
    def pages(cls, name, args):
        rows, cursor = [], ''
        while True:
            p = cls.call(name, dict(args, cursor=cursor, limit=50))
            assert p['offset'] == len(rows)
            rows.extend(p['items'])
            if p['complete']:
                assert len(rows) == p['total_items']; return rows
            assert p['next_cursor'] != cursor
            cursor = p['next_cursor']

    def test_01_discovery_and_protocol(self):
        tools,_ = self.rpc('tools/list', {})
        for n in ('list_analysis_results','get_analysis_fit_statistics','get_analysis_theory_predictions'):
            self.assertTrue(next(t for t in tools['tools'] if t['name']==n)['annotations']['readOnlyHint'])
        p=self.call('list_analysis_results'); self.assertEqual(p['items'][0]['analysis_id'],'JAM24.final')
        self.assertEqual(p['items'][0]['replicas'],791)
        self.samples['discovery']=p['items'][0]
        result,_=self.rpc('tools/call', {'name':'list_analysis_results','arguments':{}},auth=False)
        self.assertTrue(result['isError'])

    def test_02_all_statistics_exact(self):
        args={'analysis_id':'JAM24.final'}
        rows=self.pages('get_analysis_fit_statistics',args)
        self.assertEqual(len(rows),96)
        for got,expected in zip(rows,self.expected['datasets']):
            for k,v in got.items(): self.assertEqual(v,expected[k])
        for scope,key in [('overall','overall'),('reactions','reactions')]:
            got=self.pages('get_analysis_fit_statistics',dict(args,scope=scope))
            expected=[self.expected[key]] if scope=='overall' else [dict(v,reaction=k) for k,v in self.expected[key].items()]
            self.assertEqual(got,expected)
        wc=self.pages('get_analysis_fit_statistics',dict(args,reaction='wc'))
        self.assertEqual(len(wc),5); self.samples['wc']=wc
        _,size=self.rpc('tools/call',{'name':'get_analysis_fit_statistics','arguments':dict(args,reaction='wc')})
        self.sizes['example_wc_first_page']=size
        self.assertEqual(next(d for d in wc if d['dataset_id']=='10002')['chi2_cor'],4.315956884961062)
        wz=self.pages('get_analysis_fit_statistics',dict(args,reaction='wzrv',dataset_id='2012'))[0]
        self.assertEqual(wz['chi2_cor'],0.3292591128313843)
        sia=self.pages('get_analysis_fit_statistics',dict(args,reaction='sia',dataset_id='1028'))[0]
        self.assertEqual(sia['normalization']['chi2'],40.13318385349661)
        absent=next(d for d in rows if d['normalization']['value'] is None)
        self.assertIsNone(absent['normalization']['chi2']);self.assertTrue(absent['normalization']['missing_reason'])

    def test_03_predictions_and_order(self):
        args={'analysis_id':'JAM24.final','reaction':'wc','dataset_id':'10002'}
        p=self.call('get_analysis_theory_predictions',args)
        self.assertEqual(p['points'],11);self.assertIn('exp.y',[f for row in self.pages('get_analysis_theory_predictions',args) for f in row.get('available_fields',[])])
        self.assertNotIn('theory',p['items'][0]);self.samples['prediction_summary']=p
        rows=self.pages('get_analysis_theory_predictions',dict(args,detail='points'))
        self.assertEqual([r['point_index'] for r in rows],list(range(11)))
        if not self.real:
            self.assertEqual([r['theory'] for r in rows],self.theory.tolist())
            extra=self.pages('get_analysis_theory_predictions',dict(args,detail='points',fields=['exp.y','exp.units','exp.tag']))
            self.assertTrue(all(r['exp.units'] is None and r['exp.tag'] is None for r in extra))
            self.assertTrue(all('/replicas/' not in p.replace('\\','/') for p in self.reads))
            npz={Path(p).name for p in self.reads if p.endswith('.npz')}
            self.assertEqual(npz,{'wc_10002.npz'})
        else:
            # Independent direct-shard comparison, outside the MCP worker.
            code="import numpy as np,json; p="+repr(self.manifest['server_directory'])+"; z=np.load(p+'/means/wc_10002.npz',allow_pickle=False); print(json.dumps({k:z[k].tolist() for k in ['thy','value','alpha','source_row']}))"
            direct=json.loads(self.worker('',code)['out'])
            for api,stored in [('theory','thy'),('value','value'),('alpha','alpha'),('source_row','source_row')]:
                self.assertEqual([r[api] for r in rows],direct[stored])
        self.samples['first_point']=rows[0]

    def test_04_invalid_and_stale_requests(self):
        ref=self.call('list_analysis_results')['items'][0]['results_ref']
        self.call('get_analysis_fit_statistics',{'analysis_id':ref,'reaction':'wc'})
        for args in ({'analysis_id':'../../etc/passwd'}, {'analysis_id':ref[:-1]+'X'},
                     {'analysis_id':'JAM24.final','reaction':'wc;id'}, {'analysis_id':'JAM24.final','dataset_id':'10002'},
                     {'analysis_id':'JAM24.final','reaction':'wc','dataset_id':'99999'},
                     {'analysis_id':'JAM24.final','path':'/etc/passwd'}, {'analysis_id':'JAM24.final','cursor':'!'},
                     {'analysis_id':'JAM24.final','limit':True}):
            self.call('get_analysis_fit_statistics',args,error=True)
        first=self.call('get_analysis_fit_statistics',{'analysis_id':ref,'limit':1})
        self.call('get_analysis_fit_statistics',{'analysis_id':ref,'reaction':'wc','cursor':first['next_cursor']},error=True)
        self.call('get_analysis_theory_predictions',{'analysis_id':ref,'reaction':'wc','dataset_id':'10002','detail':'replicas'},error=True)
        self.call('get_analysis_theory_predictions',{'analysis_id':ref,'reaction':'wc','dataset_id':'10002','detail':'points','fields':['../path']},error=True)
        with patch.object(self.gw,'_run_ifarm_py',return_value={'rc':255,'out':'','err':'unavailable'}):
            self.call('get_analysis_fit_statistics',{'analysis_id':ref},error=True)
        with patch.object(self.gw,'_run_ifarm_py',return_value={'rc':0,'out':'{"truncated":','err':''}):
            self.call('get_analysis_fit_statistics',{'analysis_id':ref},error=True)

    def test_05_manifest_integrity_and_future_analysis(self):
        m=dict(self.local_manifest,archive_manifest_sha256='0'*64)
        with self.assertRaisesRegex(ValueError,'checksum'): ar.Archive(m)
        for change in ({'server_directory':'/etc/passwd'}, {'archive_id':'../path'}, {'catalog_ref':'bad'}):
            with self.assertRaises(ValueError): ar.validate_manifest(dict(self.manifest,**change),'JAM24')
        folder=self.sandbox/'future/reference_results/Example'
        folder.mkdir(parents=True)
        m=dict(self.manifest,analysis_id='Example.final',catalog_ref='catalog:Example.final@'+'a'*64,server_directory='/data/reference_results/Example/data/'+self.manifest['archive_id'])
        (folder/'reference_manifest.json').write_text(json.dumps(m))
        self.assertEqual(ar.Results(folder.parent).list()['items'][0]['analysis_id'],'Example.final')
        # A modified shard is rejected rather than silently accepted or recalculated.
        p=self.fixture/'means/wc_10002.npz';raw=p.read_bytes()
        try:
            p.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
            with self.assertRaisesRegex(ValueError,'checksum'):
                ar.Archive(self.local_manifest).read('means/wc_10002.npz')
        finally: p.write_bytes(raw)
        with self.assertRaises(ValueError):
            ar.regular(self.fixture/'..'/'manifest.json',self.fixture)

    def test_06_deployment_includes_only_manifest_and_code(self):
        hot=importlib.import_module('hot_redeploy'); normal=importlib.import_module('redeploy_jam')
        with patch.object(hot,'LOCAL',str(APP/'jam_gateway.py')), patch.object(hot,'PANEL',str(APP/'jam_panel.html')):
            files=hot.deployment_files()
        self.assertIn('jamai/analysis_results.py',files)
        self.assertIn('reference_results/JAM24/reference_manifest.json',files)
        self.assertFalse(any('/data/' in k for k in files))
        payload=normal._payload()
        self.assertIn('reference_results/JAM24/reference_manifest.json',payload)
        self.assertFalse(any('/data/' in k for k in payload))
        stage=self.sandbox/'stage';stage.mkdir()
        for rel,p in files.items():
            dest=stage/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,dest)
        args={'stage':str(stage),'hashes':{rel:ar.digest(Path(p).read_bytes()) for rel,p in files.items()}}
        program='args='+repr(args)+'\n'+hot.STAGE_CHECK
        r=subprocess.run([sys.executable,'-B','-c',program],capture_output=True,text=True,timeout=60)
        self.assertEqual(r.returncode,0,r.stderr);self.assertIn('IMPORT-OK',r.stdout)

    def test_07_all_real_dataset_arrays(self):
        if not self.real:
            self.skipTest('Set JAM_RESULTS_REAL_SSH=1 for all 96 real dataset shards')
        code=Path(ar.__file__).read_text(encoding='utf-8')+'\nmanifest='+repr(self.manifest)+'''
import numpy as np
a=Archive(manifest)
s=a.summary()
read_paths=[]
original=a.read
def observed(rel):
    read_paths.append(rel)
    return original(rel)
a.read=observed
points=0;pages=0
for d in s['datasets']:
    args={'analysis_id':manifest['analysis_id'],'reaction':d['reaction'],'dataset_id':d['dataset_id'],'limit':50}
    for detail in ['summary','points']:
        cursor='';rows=[]
        while True:
            p=a.retrieve('predictions',dict(args,detail=detail,cursor=cursor))
            wrapper={'content':[{'type':'text','text':encoded(p)}],'structuredContent':p,'isError':False}
            assert len(encoded(wrapper).encode())+384 <= MAX_RESPONSE_BYTES
            rows.extend(p['items']);pages+=1
            if p['complete']:break
            cursor=p['next_cursor']
        if detail=='points':
            with np.load(io.BytesIO(original('means/'+d['reaction']+'_'+d['dataset_id']+'.npz')),allow_pickle=False) as z:
                for api,stored in [('theory','thy'),('value','value'),('alpha','alpha'),('source_row','source_row')]:
                    assert np.array_equal(np.array([r[api] for r in rows]),z[stored]),(d['key'],api)
            assert [r['point_index'] for r in rows]==list(range(d['accepted_points']))
            points+=len(rows)
assert not any(p.startswith('replicas/') for p in read_paths)
print(encoded({'datasets':len(s['datasets']),'points':points,'pages':pages,'array_difference':0,'replica_files_loaded':0}))
'''
        r=self.worker('',code)
        self.assertEqual(r['rc'],0,r['err'])
        report=json.loads(r['out']);self.assertEqual(report['datasets'],96);self.assertEqual(report['points'],5853)
        self.samples['all_dataset_arrays']=report


if __name__=='__main__': unittest.main()
