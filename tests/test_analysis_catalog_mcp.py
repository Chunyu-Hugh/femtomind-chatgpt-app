"""Real loopback MCP catalog requests; no dashboard, HPC, fits or production state.

Run: python -B -m unittest discover -s tests -p test_analysis_catalog_mcp.py -v
"""
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'lqcdmcp-deploy'
sys.path.insert(0, str(APP))


class CatalogMCPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='femtomind-catalog-mcp-')
        cls.sandbox = Path(cls.temp.name)
        cls.token = secrets.token_hex(40)  # local disposable test credential only
        cls.environment = patch.dict(os.environ, {
            'JAM_GW_OWNER': 'catalogtest', 'JAM_GW_USER': 'catalogtest',
            'JAM_GW_TEST_TOKEN': cls.token, 'LQCDMCP_TOKEN': cls.token,
            'JAM_GW_PLUGINS': 'none', 'JAM_GW_BIND': '127.0.0.1',
            'JAM_GW_ACCESS_DIR': str(cls.sandbox / 'access'),
            'JAM_GW_SITE_FILE': str(cls.sandbox / 'no-site.json'),
            'SLURM_JOB_ID': '', 'PYTHONDONTWRITEBYTECODE': '1',
        })
        cls.environment.start()
        import jam_gateway
        cls.gateway = jam_gateway
        cls.gateway.TEST_TOKEN = cls.token
        cls.gateway.GATEWAY_OWNER = 'catalogtest'
        cls.gateway.ACCESS_DIR = str(cls.sandbox / 'access')
        cls.gateway.HTTPLOG = str(cls.sandbox / 'http.log')
        def forbidden(*args, **kwargs):
            raise AssertionError('Catalog request attempted external/production operation')
        cls.patches = [patch.object(cls.gateway, name, side_effect=forbidden)
                       for name in ('_dashboard_identify', '_remote', '_run', 'register')]
        for item in cls.patches:
            item.start()
        cls.server = cls.gateway.ThreadingHTTPServer(('127.0.0.1', 0), cls.gateway.H)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:%d/mcp' % cls.server.server_port
        cls.next_id = 0
        cls.max_response_bytes = 0
        cls.max_catalog_response_bytes = 0
        cls.requests = 0
        cls.samples = {}
        cls.response_measurements = {}
        cls.original_files = {p.name: p.read_bytes() for p in (ROOT / 'analysis_catalog/JAM24').iterdir()}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        for p in cls.patches:
            p.stop()
        cls.environment.stop()
        for name, raw in cls.original_files.items():
            assert (ROOT / 'analysis_catalog/JAM24' / name).read_bytes() == raw
        report = os.environ.get('CATALOG_MCP_TEST_REPORT')
        if report:
            Path(report).write_text(json.dumps({
                'transport': 'HTTP JSON-RPC using the real gateway.H dispatcher and registered tools',
                'requests': cls.requests, 'max_response_bytes': cls.max_response_bytes,
                'max_catalog_response_bytes': cls.max_catalog_response_bytes,
                'samples': cls.samples, 'approved_catalog_unchanged': True,
                'response_measurements': cls.response_measurements,
                'production_operations': 0,
            }, indent=2) + '\n', encoding='utf-8')
        cls.temp.cleanup()

    @classmethod
    def rpc(cls, method, params=None, authenticated=True, notification=False):
        cls.next_id += 1
        body = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if not notification:
            body['id'] = cls.next_id
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + cls.token
        request = urllib.request.Request(cls.url, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            cls.requests += 1
            cls.max_response_bytes = max(cls.max_response_bytes, len(raw))
            if notification:
                assert response.status == 202 and not raw
                return None
            result = json.loads(raw)
        assert 'error' not in result, result
        return result['result'], len(raw)

    @classmethod
    def call(cls, name, arguments=None, error=False):
        result, size = cls.rpc('tools/call', {'name': name, 'arguments': arguments or {}})
        if error:
            assert result['isError'] is True, result
            return result
        assert result['isError'] is False, result
        assert size <= cls.gateway.analysis_catalog.MAX_RESPONSE_BYTES, size
        cls.max_catalog_response_bytes = max(cls.max_catalog_response_bytes, size)
        return result['structuredContent']

    @classmethod
    def pages(cls, name, arguments):
        cursor = ''
        items = []
        visited = set()
        last = None
        while True:
            args = dict(arguments, cursor=cursor, limit=50)
            if name in ('get_analysis_catalog', 'get_analysis_requirements'):
                args.setdefault('detail', 'full')
            page = cls.call(name, args)
            assert page['offset'] == len(items), page
            items.extend(page['items'])
            last = page
            if page['complete']:
                assert page['next_cursor'] is None and len(items) == page['total_items']
                return cls.records(items), last
            cursor = page['next_cursor']
            assert cursor and cursor not in visited
            visited.add(cursor)

    @staticmethod
    def records(items):
        """Losslessly join any oversized-record fragments emitted by the service."""
        output, chunks, current = [], [], None
        for item in items:
            if 'fragment_sha256' not in item:
                assert not chunks
                output.append(item)
                continue
            identity = (item['json_pointer'], item['fragment_sha256'])
            assert current is None or current == identity
            current = identity
            assert item['fragment_offset'] == sum(len(s) for s in chunks)
            chunks.append(item['text'])
            text = ''.join(chunks)
            if len(text) == item['fragment_total_chars']:
                assert hashlib.sha256(text.encode()).hexdigest() == item['fragment_sha256']
                output.append(json.loads(text))
                chunks, current = [], None
        assert not chunks
        return output

    def test_01_discover_over_actual_mcp(self):
        result, _ = self.rpc('initialize', {'protocolVersion': '2025-03-26', 'clientInfo': {'name': 'catalog-regression', 'version': '1'}})
        self.assertIn('tools', result['capabilities'])
        self.rpc('notifications/initialized', notification=True)
        listing, _ = self.rpc('tools/list')
        tools = {item['name']: item for item in listing['tools']}
        for name in ('list_analysis_catalog', 'get_analysis_catalog', 'get_analysis_requirements'):
            self.assertTrue(tools[name]['annotations']['readOnlyHint'])
            self.assertFalse(tools[name]['annotations']['destructiveHint'])
        self.assertIn('analyze_by_experiment', tools)
        self.assertIn('campaign_pdf_plots', tools)
        result = self.call('list_analysis_catalog')
        jam = next(item for item in result['items'] if item['analysis_id'] == 'JAM24.final')
        self.assertFalse(jam['reproduction_ready'])
        self.assertEqual(len(jam['scientific_objects']), 4)
        self.samples['available_analyses'] = jam
        missing, _ = self.rpc('tools/call', {'name': 'list_analysis_catalog', 'arguments': {}}, authenticated=False)
        self.assertTrue(missing['isError'])
        self.assertEqual(missing['structuredContent']['status'], 'unauthorized')

    def test_02_configuration_and_parametrization(self):
        overview, _ = self.pages('get_analysis_catalog', {'analysis_id': 'JAM24', 'section': 'overview'})
        self.assertTrue(overview)
        forms, _ = self.pages('get_analysis_catalog', {'analysis_id': 'JAM24', 'section': 'parametrization', 'object_key': 'pdf'})
        self.assertTrue(any(item.get('record', {}).get('id') == 'model.template' for item in forms if isinstance(item.get('record'), dict)))
        self.samples['pdf_parametrization'] = next(item for item in forms if item.get('record', {}).get('id') == 'model.template')
        rows, _ = self.pages('get_analysis_catalog', {'analysis_id': 'JAM24', 'section': 'parameters'})
        self.assertEqual(len(rows), 369)
        original = json.loads(self.original_files['analysis_config.json'])
        for obj, definitions in original['source_verification']['parameter_definitions'].items():
            self.assertEqual([r['record'] for r in rows if r['object_key'] == obj], definitions)
        self.assertEqual(sum(r['record']['status'] == 'linked' for r in rows), 150)
        pion, _ = self.pages('get_analysis_catalog', {'analysis_id': 'JAM24', 'section': 'parameters', 'object_key': 'ffpion'})
        self.assertEqual(sum(r['record']['status'] == 'free' for r in pion), 60)
        self.assertEqual(sum(r['record']['status'] == 'linked' for r in pion), 50)
        self.samples['pion_parameters'] = {'independent': 60, 'linked': 50, 'example': pion[0]}

    def test_03_datasets_positivity_and_initialization(self):
        rows, _ = self.pages('get_analysis_catalog', {'analysis_id': 'JAM24', 'section': 'datasets'})
        self.assertEqual(len(rows), 98)
        self.assertEqual(sum(r['record']['current_reader_verification']['retained_count'] for r in rows), 5853)
        self.assertEqual(sum(r['record']['normalization'] is not None and r['record']['normalization']['definition']['fixed'] is False for r in rows), 43)
        self.samples['datasets'] = {'catalog_entries': 98, 'accepted_points': 5853, 'independent_normalizations': 43}
        qcd, _ = self.pages('get_analysis_catalog', {'analysis_id': 'JAM24', 'section': 'qcd'})
        chosen = {r['json_pointer']: r['record'] for r in qcd}
        self.assertEqual(chosen['/reproduction_configuration/Q20']['value'], 1.6129)
        positivity = chosen['/reproduction_configuration/positivity']
        self.assertTrue(all(flag is False for flag in positivity['fitpack_flags'].values()))
        stats, statistics_page = self.pages('get_analysis_catalog', {'analysis_id': 'JAM24', 'section': 'statistics'})
        chosen = {r['json_pointer']: r['record'] for r in stats}
        gaussian = chosen['/reproduction_configuration/Gaussian_initialization']
        self.assertTrue(gaussian['flat par'])
        self.assertEqual(gaussian['parameter_key'], 'gaussian')
        self.assertFalse(gaussian['Gaussian_template_penalties'])
        self.assertEqual(gaussian['means_by_coefficient']['N'], .02)
        self.assertIn('/reproduction_configuration/Gaussian_initialization', json.dumps(statistics_page['current_initialization_context']))
        self.assertEqual(next(r['layer'] for r in qcd if r['json_pointer'] == '/source_verification/latest_author_corrections'), 'author_confirmation')
        self.samples['positivity'] = positivity
        self.samples['Gaussian_initialization'] = gaussian

    def test_04_current_requirements_and_errors(self):
        rows, page = self.pages('get_analysis_requirements', {'analysis_id': 'JAM24'})
        blockers = [r['record'] for r in rows if r['record'].get('reproduction_blocker')]
        self.assertEqual({r['id'] for r in blockers}, {'U03', 'U05', 'U09', 'U11', 'U12'})
        self.assertTrue(all(r['priority'] and r['why_it_matters'] and r['evidence_sources'] for r in blockers))
        self.assertTrue(all(r['classification'] == 'historical_information' for r in blockers))
        notes = [r['record'] for r in rows if 'remaining_unknown' in r['record']]
        self.assertTrue(notes)
        self.assertTrue(all(r['priority'] is None for r in notes))
        self.samples['requirements'] = blockers
        for arguments in ({'analysis_id': '../JAM24'}, {'analysis_id': 'unknown'},
                          {'analysis_id': 'JAM24', 'section': '../input.py'},
                          {'analysis_id': 'JAM24', 'path': '/etc/passwd'},
                          {'analysis_id': 'JAM24', 'section': 'parameters', 'object_key': 'invalid'},
                          {'analysis_id': 'JAM24', 'limit': True}, {'analysis_id': 'JAM24', 'limit': 0},
                          {'analysis_id': 'JAM24', 'limit': 51}, {'analysis_id': 'JAM24', 'cursor': 'not-a-cursor'}):
            self.call('get_analysis_catalog', arguments, error=True)
        page = self.call('get_analysis_catalog', {'analysis_id': 'JAM24', 'section': 'parameters', 'limit': 1})
        self.call('get_analysis_catalog', {'analysis_id': 'JAM24', 'section': 'datasets', 'cursor': page['next_cursor']}, error=True)

    def test_05_complete_documents_without_truncation(self):
        for section, filename in self.gateway.analysis_catalog.DOCUMENTS.items():
            items, last = self.pages('get_analysis_catalog', {'analysis_id': 'JAM24', 'section': section})
            text, offset = '', 0
            for item in items:
                self.assertEqual(item['char_offset'], offset)
                text += item['text']
                offset += len(item['text'])
            raw = text.encode('utf-8')
            self.assertEqual(raw, self.original_files[filename])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), last['document_sha256'])
            if section == 'analysis_config':
                self.assertEqual(json.loads(text), json.loads(self.original_files[filename]))

    def test_06_deployment_package_and_staged_import(self):
        with patch.object(sys, 'argv', ['hot_redeploy.py']):
            hot = importlib.import_module('hot_redeploy')
        with patch.object(sys, 'argv', ['redeploy_jam.py', 'script']):
            redeploy = importlib.import_module('redeploy_jam')
        files = hot.deployment_files()
        payload = redeploy._payload()
        self.assertIn('jamai/analysis_catalog.py', files)
        self.assertEqual(zlib.decompress(base64.b64decode(payload['jamai/analysis_catalog.py'])), (APP / 'jamai/analysis_catalog.py').read_bytes())
        for name, expected in self.original_files.items():
            rel = 'analysis_catalog/JAM24/' + name
            self.assertEqual(Path(files[rel]).read_bytes(), expected)
            self.assertEqual(zlib.decompress(base64.b64decode(payload[rel])), expected)
        self.assertIn('analysis_catalog', hot.INSTALL)
        stage = self.sandbox / 'staged-app'
        for rel, encoded in payload.items():
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zlib.decompress(base64.b64decode(encoded)))
        hashes = {rel: hashlib.sha256((stage / rel).read_bytes()).hexdigest() for rel in payload}
        program = 'args = ' + repr({'stage': str(stage), 'hashes': hashes}) + '\n' + hot.STAGE_CHECK
        result = subprocess.run([sys.executable, '-B', '-c', program], cwd=stage, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('IMPORT-OK', result.stdout)
        (stage / 'analysis_catalog/JAM24/paper_findings.md').unlink()
        remaining = {rel: digest for rel, digest in hashes.items() if rel != 'analysis_catalog/JAM24/paper_findings.md'}
        program = 'args = ' + repr({'stage': str(stage), 'hashes': remaining}) + '\n' + hot.STAGE_CHECK
        result = subprocess.run([sys.executable, '-B', '-c', program], cwd=stage, text=True, capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0, 'Staging accepted an incomplete catalog')
        empty = self.sandbox / 'empty-catalog'
        empty.mkdir()
        with patch.object(self.gateway.analysis_catalog, 'catalog_root', return_value=empty):
            for build in (hot.deployment_files, redeploy._payload):
                with self.assertRaisesRegex(ValueError, 'requires at least one'):
                    build()

    def test_07_generic_discovery_stale_cursors_and_validation(self):
        helper = self.gateway.analysis_catalog
        catalog = self.sandbox / 'generic-catalog'
        for folder, analysis_id in [('first', 'Example.one'), ('second', 'Example.two')]:
            directory = catalog / folder
            directory.mkdir(parents=True)
            for name, raw in self.original_files.items():
                if name == 'analysis_config.json':
                    config = json.loads(raw)
                    config['analysis_id'] = analysis_id
                    raw = json.dumps(config).encode()
                (directory / name).write_bytes(raw)
        store = helper.Catalog(catalog)
        first = store.list(limit=1)
        second = store.list(cursor=first['next_cursor'], limit=1)
        self.assertEqual({first['items'][0]['analysis_id'], second['items'][0]['analysis_id']}, {'Example.one', 'Example.two'})
        self.assertEqual(len(store.deployment_files()), 6)
        page = store.get('Example.one', 'parameters', limit=1)
        (catalog / 'first/paper_findings.md').write_bytes(b'Changed test-only metadata')
        with self.assertRaisesRegex(ValueError, 'stale cursor'):
            helper.Catalog(catalog).get('Example.one', 'parameters', cursor=page['next_cursor'])
        config_path = catalog / 'second/analysis_config.json'
        config = json.loads(config_path.read_bytes())
        config['analysis_id'] = 'Example.one'
        config_path.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            helper.Catalog(catalog)
        config['analysis_id'] = 'Example.two'
        config['catalog_schema']['version'] = 'unsupported'
        config_path.write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, 'schema'):
            helper.Catalog(catalog)
        original = json.loads(self.original_files['analysis_config.json'])
        invalid_updates = [
            ('parameter_definitions', {'pdf': 'not a record list'}),
            ('datasets', {'sidis': []}),
            ('resolution_register', 'not a register'),
            ('remaining_unknowns', [42]),
        ]
        for key, value in invalid_updates:
            invalid = json.loads(json.dumps(original))
            invalid['analysis_id'] = 'Example.two'
            invalid['source_verification'][key] = value
            config_path.write_text(json.dumps(invalid))
            with self.assertRaises(ValueError, msg=key):
                helper.Catalog(catalog)
        invalid = json.loads(json.dumps(original))
        invalid['analysis_id'] = 'Example.two'
        invalid['catalog_validation']['blocking_unresolved_items'].append('missing-current-record')
        config_path.write_text(json.dumps(invalid))
        with self.assertRaises(ValueError):
            helper.Catalog(catalog)

    def test_08_catalog_participates_in_swap_and_rollback(self):
        hot = importlib.import_module('hot_redeploy')
        for fail in (False, True):
            app = self.sandbox / ('rollback-case' if fail else 'install-case')
            stage = app / '.hot_redeploy.stage-test'
            stage.mkdir(parents=True)
            old = {}
            for component in ('jamai', 'plugins', 'analysis_catalog'):
                (app / component).mkdir()
                (app / component / 'old.txt').write_text('old ' + component)
                old[component + '/old.txt'] = (app / component / 'old.txt').read_bytes()
                (stage / component).mkdir()
                (stage / component / 'new.txt').write_text('new ' + component)
            for name, raw in self.original_files.items():
                directory = stage / 'analysis_catalog/JAM24'
                directory.mkdir(exist_ok=True)
                (directory / name).write_bytes(raw)
            for component in ('jam_panel.html', 'jam_gateway.py'):
                (app / component).write_text('old ' + component)
                old[component] = (app / component).read_bytes()
                if not (fail and component == 'jam_gateway.py'):
                    (stage / component).write_text('new ' + component)
            self.assertTrue(self.sandbox.resolve() in app.resolve().parents)
            args = {'app': str(app), 'stage': str(stage)}
            if fail:
                with self.assertRaises(FileNotFoundError):
                    exec(hot.INSTALL, {'args': args})
                for rel, raw in old.items():
                    self.assertEqual((app / rel).read_bytes(), raw)
                self.assertFalse((app / 'analysis_catalog/JAM24').exists())
            else:
                exec(hot.INSTALL, {'args': args})
                for rel, raw in old.items():
                    self.assertEqual((app / '.hot_redeploy.previous' / rel).read_bytes(), raw)
                for name, raw in self.original_files.items():
                    self.assertEqual((app / 'analysis_catalog/JAM24' / name).read_bytes(), raw)

    def test_09_compact_defaults_targeting_and_reusable_references(self):
        def measured(label, name, arguments):
            result, size = self.rpc('tools/call', {'name': name, 'arguments': arguments})
            self.assertFalse(result['isError'], result)
            self.assertLessEqual(size, self.gateway.analysis_catalog.MAX_RESPONSE_BYTES)
            content = result['structuredContent']
            visible = result['content'][0]['text']
            self.assertEqual(visible, json.dumps(content, separators=(',', ':')))
            self.response_measurements[label] = {
                'http_response_bytes': size, 'text_characters': len(visible),
                'structured_json_bytes': len(json.dumps(content, separators=(',', ':')).encode()),
            }
            return content

        discovery = measured('discovery_default', 'list_analysis_catalog', {})
        self.assertTrue(discovery['complete'])
        self.assertEqual(len(discovery['items']), 1)
        reference = discovery['items'][0]['catalog_ref']
        self.assertTrue(reference.startswith('catalog:JAM24.final@'))
        self.assertNotIn('pdf_sha256', discovery['items'][0]['publication'])
        self.assertNotIn('parameter_definitions', json.dumps(discovery))
        self.assertLess(self.response_measurements['discovery_default']['http_response_bytes'], 4000)
        overview = measured('overview_default', 'get_analysis_catalog', {'analysis_id': reference})
        self.assertTrue(overview['complete'])
        self.assertEqual(overview['catalog_ref'], reference)
        self.assertNotIn('definition', json.dumps(overview['items'][0]))
        self.assertLess(self.response_measurements['overview_default']['http_response_bytes'], 7000)
        parameters = measured('parameters_summary', 'get_analysis_catalog', {'analysis_id': reference, 'section': 'parameters'})
        self.assertEqual(sum(r['record']['definition_count'] for r in parameters['items']), 369)
        self.assertTrue(parameters['complete'])
        self.assertNotIn('definition', parameters['items'][0]['record'])
        forms = measured('pdf_parametrization_summary', 'get_analysis_catalog', {
            'analysis_id': reference, 'section': 'parametrization', 'object_key': 'pdf'})
        self.assertTrue(all('/ff_link_verification/' not in r.get('json_pointer', '') for r in forms['items']))
        self.assertTrue(any(r.get('record', {}).get('id') == 'model.template' for r in self.records(forms['items']) if isinstance(r.get('record'), dict)))
        selected = measured('pion_u1_full', 'get_analysis_catalog', {
            'analysis_id': reference, 'section': 'parameters', 'object_key': 'ffpion',
            'group': 'u1', 'status': 'free', 'detail': 'full'})
        self.assertTrue(selected['complete'])
        config = json.loads(self.original_files['analysis_config.json'])
        expected = [p for p in config['source_verification']['parameter_definitions']['ffpion']
                    if p['name'].startswith('u1 ') and p['status'] == 'free']
        self.assertEqual([r['record'] for r in selected['items']], expected)
        fixed, _ = self.pages('get_analysis_catalog', {
            'analysis_id': reference, 'section': 'parameters', 'object_key': 'pdf', 'status': 'fixed', 'detail': 'full'})
        self.assertEqual([r['record'] for r in fixed], [p for p in config['source_verification']['parameter_definitions']['pdf'] if p['status'] == 'fixed_true'])
        self.assertEqual(len(fixed), 4)
        datasets = measured('datasets_summary', 'get_analysis_catalog', {'analysis_id': reference, 'section': 'datasets'})
        self.assertTrue(datasets['complete'])
        self.assertEqual(sum(r['record']['dataset_count'] for r in datasets['items']), 98)
        self.assertEqual(sum(r['record']['accepted_points'] for r in datasets['items']), 5853)
        dataset = measured('sia_2029_full', 'get_analysis_catalog', {
            'analysis_id': reference, 'section': 'datasets', 'object_key': 'sia', 'dataset_id': '2029', 'detail': 'full'})
        self.assertEqual(len(dataset['items']), 1)
        self.assertEqual(dataset['items'][0]['record'], next(d for d in config['source_verification']['datasets']['sia']['entries'] if d['id'] == '2029'))
        positivity = measured('positivity_summary', 'get_analysis_catalog', {'analysis_id': reference, 'section': 'qcd', 'setting_id': 'positivity'})
        self.assertEqual(positivity['items'][0]['record']['fitpack_flags'], config['reproduction_configuration']['positivity']['fitpack_flags'])
        requirement = measured('requirement_U03', 'get_analysis_requirements', {'analysis_id': reference, 'requirement_id': 'U03'})
        self.assertEqual([r['record']['id'] for r in requirement['items']], ['U03'])
        requirements, _ = self.pages('get_analysis_requirements', {'analysis_id': reference, 'detail': 'summary'})
        self.assertEqual(len(requirements), 5)
        self.assertTrue(all(r['record']['reproduction_blocker'] for r in requirements))
        page = self.call('get_analysis_catalog', {'analysis_id': reference, 'section': 'parameters', 'object_key': 'ffpion', 'detail': 'full', 'limit': 1})
        self.call('get_analysis_catalog', {'analysis_id': reference, 'section': 'parameters', 'object_key': 'ffpion', 'detail': 'full', 'group': 'u1', 'cursor': page['next_cursor']}, error=True)
        for arguments in (
            {'analysis_id': reference[:-1] + ('0' if reference[-1] != '0' else '1')},
            {'analysis_id': reference, 'section': 'parameters', 'group': 'missing'},
            {'analysis_id': reference, 'section': 'datasets', 'dataset_id': '2029'},
            {'analysis_id': reference, 'section': 'datasets', 'object_key': 'sia', 'dataset_id': 'missing'},
            {'analysis_id': reference, 'section': 'qcd', 'setting_id': 'missing'},
            {'analysis_id': reference, 'detail': 'everything'},
        ):
            self.call('get_analysis_catalog', arguments, error=True)
        self.call('get_analysis_requirements', {'analysis_id': reference, 'requirement_id': 'resolved'}, error=True)

    def test_10_existing_tool_response_format_unchanged(self):
        import ast
        previous = subprocess.check_output(['git', 'show', 'HEAD:lqcdmcp-deploy/jam_gateway.py'], cwd=ROOT).decode('utf-8')
        tree = ast.parse(previous)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_tool_result')
        scope = {'json': json}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<baseline-tool-result>', 'exec'), scope)
        samples = ['pong', [1, {'summary': 'example'}], {'status': 'success', 'items': [1, 2]},
                   {'_images': [{'data': 'test', 'mimeType': 'image/png'}], '_ui': {'chart': 'test'}, 'summary': 'example'},
                   {'_panel_plots': ['test'], 'summary': 'legacy'}]
        for sample in samples:
            for error in (False, True):
                self.assertEqual(self.gateway._tool_result(sample, is_error=error), scope['_tool_result'](sample, is_error=error))

    def test_11_plugin_tools_resources_and_existing_ping(self):
        import importlib.util
        folder = self.sandbox / 'plugins/catalogprobe'
        folder.mkdir(parents=True)
        (folder / 'plugin.py').write_text('''NAME = 'catalogprobe'
SHIP = ['plugin.py']
ROUTING = 'Catalog regression fixture.'
INSTRUCTIONS = 'Read-only test fixture.'
def load(): pass
def tools():
    return {'catalogprobe_echo': {'desc': 'Read-only echo.', 'schema': {'type':'object'},
            'fn': lambda a: {'echo': a.get('value'), 'source': 'plugin'}}}
def resources():
    return {'ui://catalogprobe/test': {'name':'Probe', 'mimeType':'text/html', 'text':lambda:'<p>probe</p>'}}
''')
        spec = importlib.util.spec_from_file_location('catalog_test_plugins', APP / 'plugins/__init__.py')
        plugins = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugins)
        tools = dict(self.gateway.TOOLS)
        with patch.object(plugins, 'ROOT', str(folder.parent)), patch.dict(os.environ, {'JAM_GW_PLUGINS': 'catalogprobe'}):
            resources, pages, routing, instructions, status = plugins.attach(tools, set())
            self.assertEqual(status, {'loaded': ['catalogprobe'], 'errors': {}})
            shipped, skipped = plugins.ship_files()
            self.assertFalse(skipped)
            self.assertIn('plugins/catalogprobe/plugin.py', shipped)
            with patch.object(self.gateway, 'TOOLS', tools), patch.object(self.gateway, 'PLUGIN_RESOURCES', resources):
                listing, _ = self.rpc('tools/list')
                self.assertIn('catalogprobe_echo', {t['name'] for t in listing['tools']})
                echo = self.call('catalogprobe_echo', {'value': 'preserved'})
                self.assertEqual(echo, {'echo': 'preserved', 'source': 'plugin'})
                result, _ = self.rpc('resources/read', {'uri': 'ui://catalogprobe/test'})
                self.assertEqual(result['contents'][0]['text'], '<p>probe</p>')
                ping, _ = self.rpc('tools/call', {'name': 'ping', 'arguments': {}})
                self.assertFalse(ping['isError'])
                self.assertIn('pong', ping['content'][0]['text'])
                self.assertTrue(self.call('list_analysis_catalog')['items'])


if __name__ == '__main__':
    unittest.main()
