"""Incremental central predictions. No FitPack calculations or scoring changes.

Legacy dataset documents stay intact. New rows are journaled by immutable
scientific source identity; complete cohorts are published through one atomic
pointer. An OS advisory lock serializes builders and is released on process exit.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
import uuid


def pc_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def pc_read(path):
    try:
        with open(path, encoding='utf-8') as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def pc_write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True, separators=(',', ':'), allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            descriptor = os.open(str(path.parent), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
    finally:
        if temporary.exists(): temporary.unlink()


class PredictionBuildLock:
    def __init__(self, root):
        self.path = Path(root) / 'incremental' / 'builder.lock'
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.stream.write(b'0'); self.stream.flush()
                self.stream.seek(0); msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.stream.close(); self.stream = None
        return self.stream is not None

    def __exit__(self, *ignored):
        if self.stream is not None:
            if os.name == 'nt':
                import msvcrt
                self.stream.seek(0); msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            self.stream.close()


class PredictionCache:
    schema = 'femtomind.central-predictions.v1'
    dynamic = ('theory', 'shift', 'nuisance_residuals',
               'normalization_residuals', 'objective_residuals')

    def __init__(self, cache, stage, records, signature, version=2, metrics_version=1):
        self.cache = Path(cache); self.root = self.cache / 'incremental'
        self.stage = os.path.realpath(stage)
        self.records = list(records); self.signature = signature
        self.sources = {row[0]: row for row in signature}
        self.ids = [row['identity'] for row in records]
        if len(self.ids) != len(set(self.ids)) or set(self.ids) != set(self.sources) - {'input.py'}:
            raise ValueError('Duplicate or incomplete pinned replica identities')
        self.version = version; self.metrics_version = metrics_version
        self.context = pc_hash([self.schema, self.stage, signature[0], version, metrics_version])
        self.cohort = pc_hash([self.context, signature])
        self.rows = {}; self.static = {}; self.keys = []; self.invalid = []
        self.reused = set(); self.evaluated = set(); self.failed = {}

    def _name(self, key):
        return re.sub(r'[^A-Za-z0-9._-]', '_', key.replace(':', '_')) + '.json'

    def _source(self, identity):
        return pc_hash([self.context, self.sources[identity]])

    def complete(self):
        """Validate a published cohort without reading its large prediction arrays."""
        pointer = pc_read(self.root / 'active.json') or {}
        token = pointer.get('cohort', '')
        if not re.fullmatch(r'[0-9a-f]{64}', token):
            return self._legacy_complete()
        folder = self.root / 'cohorts' / token
        manifest = pc_read(folder / 'cohort.json') or {}
        if (manifest.get('context') != self.context or manifest.get('schema') != self.schema
                or not all(self.sources.get(row[0]) == row for row in manifest.get('signature', []))):
            return self._legacy_complete()
        signature = manifest.get('signature', [])
        identities = manifest.get('replicas', [])
        if (not signature or token != pc_hash([self.context, signature])
                or len(identities) != len(set(identities))
                or set(identities) != {r[0] for r in signature} - {'input.py'}
                or not manifest.get('files')): return None
        for name, identity in manifest.get('files', {}).items():
            if not re.fullmatch(r'[A-Za-z0-9._-]+\.json', name): return None
            p = folder / 'datasets' / name
            try: stat = p.stat()
            except OSError: return None
            if [stat.st_size, stat.st_mtime_ns] != identity: return None
        return {'cache': str(folder), 'cohort': token, 'replicas': identities,
                'signature': signature, 'complete': True}

    def _legacy_complete(self):
        meta = pc_read(self.cache / 'meta.json') or {}; info = meta.get('info') or {}
        if meta.get('version') != self.version or not info: return None
        cohort = None; signature = None
        for key in info:
            path = self.cache / 'datasets' / self._name(key)
            # Signature and identities precede numerical arrays in legacy docs.
            try:
                with path.open(encoding='utf-8') as stream: prefix = stream.read(1024 * 1024)
            except OSError: return None
            fields = {}
            for field in ('version', 'metrics_version', 'signature', 'replicas'):
                match = re.search(r'"' + field + r'"\s*:', prefix)
                try: fields[field] = json.JSONDecoder().raw_decode(prefix[match.end():].lstrip())[0]
                except (ValueError, AttributeError):
                    # Legacy JSON key ordering is not a schema guarantee.
                    full = pc_read(path) or {}
                    if field not in full: return None
                    fields[field] = full[field]
            if (fields['version'] != self.version or fields['metrics_version'] != self.metrics_version
                    or not fields['replicas'] or len(set(fields['replicas'])) != len(fields['replicas'])
                    or not all(self.sources.get(row[0]) == row for row in fields['signature'])): return None
            if cohort is None: cohort, signature = fields['replicas'], fields['signature']
            if cohort != fields['replicas'] or signature != fields['signature']: return None
        return {'cache': str(self.cache), 'cohort': pc_hash([self.context, signature]),
                'replicas': cohort, 'signature': signature, 'complete': True, 'legacy': True}

    def adopt(self, complete):
        """Pin legacy files by hard link (copy fallback); never rewrite originals."""
        if not complete or not complete.get('legacy'): return complete
        folder = self.root / 'cohorts' / complete['cohort']; target = folder / 'datasets'
        target.mkdir(parents=True, exist_ok=True)
        info = pc_read(self.cache / 'meta.json')
        for key in info['info']:
            source = self.cache / 'datasets' / self._name(key); dest = target / source.name
            if not dest.exists():
                try: os.link(source, dest)
                except OSError: shutil.copyfile(source, dest)
        pc_write(folder / 'meta.json', info)
        self._publish(folder, complete['replicas'], complete['signature'])
        return self.complete()

    def _publish(self, folder, identities, signature):
        files = {p.name: [p.stat().st_size, p.stat().st_mtime_ns] for p in (folder / 'datasets').glob('*.json')}
        manifest = {'schema': self.schema, 'context': self.context, 'campaign_stage': self.stage,
                    'cohort': folder.name, 'replicas': identities, 'signature': signature, 'files': files}
        pc_write(folder / 'cohort.json', manifest)
        active = pc_read(self.root / 'active.json') or {}
        previous = (pc_read(self.root/'cohorts'/active['cohort']/'cohort.json') or {}
                    if re.fullmatch(r'[0-9a-f]{64}', active.get('cohort','')) else {})
        previous_sources = {row[0]:row for row in previous.get('signature',[])}
        if (previous.get('context') == self.context and set(identities) < set(previous.get('replicas',[]))
                and all(previous_sources.get(row[0]) == row for row in signature)):
            # A late finisher of the older target cannot replace a newer,
            # already published complete superset with fewer replicas.
            return False
        pc_write(self.root / 'active.json', {'schema': self.schema, 'cohort': folder.name})
        return True

    def load(self, keys):
        self.keys = list(keys); self.rows = {key: {} for key in keys}
        complete = self.complete()
        # Reuse individual legacy rows even if another row's source changed.
        base = Path(complete['cache']) if complete else self.cache
        bases = [base] + ([self.cache] if base != self.cache else [])
        for key in keys:
            seen_files = set()
            for folder in bases:
                path = folder / 'datasets' / self._name(key)
                try: stat = path.stat()
                except OSError: continue
                file_id = (stat.st_dev, stat.st_ino)
                if file_id in seen_files: continue  # Unchanged adopted hard link.
                seen_files.add(file_id)
                doc = pc_read(path)
                if not doc or doc.get('version') != self.version or doc.get('metrics_version') != self.metrics_version: continue
                sources = {r[0]: r for r in doc.get('signature', [])}
                if sources.get('input.py') != self.sources['input.py']: continue
                if len(doc.get('replicas', [])) != len(set(doc.get('replicas', []))):
                    raise ValueError('Duplicate cached replica identities: ' + key)
                static = {k:v for k,v in doc.items() if k not in (*self.dynamic, 'stats', 'replicas', 'signature')}
                self._check_static(key, static)
                for i, identity in enumerate(doc.get('replicas', [])):
                    if (identity in self.rows[key] or identity not in self.ids
                            or sources.get(identity) != self.sources[identity]): continue
                    row = self._row(doc, i); self._validate_row(static, row)
                    self.rows[key][identity] = row
        for identity in self.ids:
            index = pc_read(self.root / 'rows' / (self._source(identity) + '.index.json'))
            if not index: continue
            try:
                if index['source'] != self.sources[identity] or index['context'] != self.context: raise ValueError('row source')
                name = index['file']
                if not re.fullmatch(r'[0-9a-f]{32}\.json', name): raise ValueError('row file')
                doc = pc_read(self.root / 'rows' / name)
                if not doc or pc_hash(doc) != index['sha256']: raise ValueError('row integrity')
                if (doc.get('identity') != identity or doc.get('source') != self.sources[identity]
                        or doc.get('context') != self.context): raise ValueError('payload source identity')
                validated = {}
                for key, item in doc['datasets'].items():
                    if key not in self.rows: continue
                    if not re.fullmatch(r'[0-9a-f]{64}', item['static']): raise ValueError('dataset reference')
                    static = pc_read(self.root / 'static' / (item['static'] + '.json'))
                    if not static or pc_hash(static) != item['static']: raise ValueError('dataset integrity')
                    self._validate_row(static, item['row']); validated[key] = (static, item['row'])
            except (KeyError, ValueError, TypeError, IndexError) as exc:
                self.invalid.append({'replica': identity, 'message': str(exc)}); continue
            # A valid row whose experimental identity differs is a scientific
            # hard failure, not permission to reevaluate or silently replace it.
            for key, (static, row) in validated.items(): self._check_static(key, static)
            for key, (static, row) in validated.items(): self.rows[key][identity] = row
        self.reused = {i for i in self.ids if all(i in self.rows[k] for k in keys)}
        return [r for r in self.records if r['identity'] not in self.reused]

    def _row(self, doc, i):
        if doc.get('unavailable'): return {}
        return {**{key: doc[key][i] for key in self.dynamic}, 'stats': {k:v[i] for k,v in doc['stats'].items()}}

    def _check_static(self, key, static):
        if key in self.static and self.static[key] != static:
            raise ValueError('Experimental points, normalization or nuisance identity changed: ' + key)
        self.static[key] = static

    def _validate_row(self, static, row):
        if static.get('unavailable'):
            if row: raise ValueError('Nonempty row for zero-point dataset')
            return
        n = static['npts']; cols = static['columns']
        if len(cols['value']) != n or len(cols['alpha']) != n: raise ValueError('Experimental point shape')
        if any(not math.isfinite(float(x)) for x in cols['value']) or any(not math.isfinite(float(x)) or x <= 0 for x in cols['alpha']):
            raise ValueError('Experimental values/uncertainties')
        for field in ('theory','shift','objective_residuals'):
            if len(row[field]) != n or any(not math.isfinite(float(v)) for v in row[field]): raise ValueError('Prediction shape/values')
        if (len(row['nuisance_residuals']) != len(static['nuisance_order'])
                or any(not math.isfinite(float(v)) for v in row['nuisance_residuals'])
                or not math.isfinite(float(row['normalization_residuals']))): raise ValueError('Penalty shape/values')
        if set(row['stats']) != {'chi2','chi2_cor','chi2_norm','norm'}: raise ValueError('Missing replica statistics')
        for key, value in row['stats'].items():
            if not (key == 'norm' and value is None) and not math.isfinite(float(value)): raise ValueError('Nonfinite statistic')

    def save(self, identity, docs):
        """One replica is a bounded atomic batch, committed only after every dataset."""
        if identity not in self.ids or set(docs) != set(self.keys): raise ValueError('Incomplete replica batch')
        payload = {'schema': self.schema, 'context': self.context, 'identity': identity,
                   'source': self.sources[identity], 'datasets': {}}
        for key, doc in docs.items():
            static = {k:v for k,v in doc.items() if k not in (*self.dynamic, 'stats', 'replicas', 'signature')}
            self._check_static(key, static); token = pc_hash(static)
            path = self.root / 'static' / (token + '.json')
            if not path.exists(): pc_write(path, static)
            row = self._row(doc, 0); self._validate_row(static, row)
            payload['datasets'][key] = {'static': token, 'row': row}
        name = uuid.uuid4().hex + '.json'; pc_write(self.root / 'rows' / name, payload)
        pc_write(self.root / 'rows' / (self._source(identity) + '.index.json'),
                 {'context': self.context, 'source': self.sources[identity], 'file': name, 'sha256': pc_hash(payload)})
        for key, item in payload['datasets'].items(): self.rows[key][identity] = item['row']
        self.evaluated.add(identity); self.progress('running')

    def progress(self, state, error=None):
        done = {i for i in self.ids if all(i in self.rows.get(k,{}) for k in self.keys)}
        target_path = self.root / 'builds' / (self.cohort + '.json')
        target = pc_read(target_path)
        if target is None:
            target = {'schema':self.schema, 'context':self.context, 'campaign_stage':self.stage,
                      'signature':self.signature, 'replicas':self.ids, 'initial_cached':len(self.reused)}
            pc_write(target_path, target)
        baseline = min(target['initial_cached'], len(done))
        result = {'schema': self.schema, 'context': self.context, 'cohort': self.cohort,
                  'campaign_stage': self.stage, 'status': state, 'target_replicas': len(self.ids),
                  'cached_replicas': baseline, 'newly_evaluated': len(done)-baseline,
                  'completed_replicas': len(done), 'remaining_replicas': len(self.ids)-len(done),
                  'failed_replicas': len(self.failed), 'failures': self.failed, 'invalid_cache': self.invalid,
                  'updated_at': time.time(), 'error': error}
        pc_write(self.root / 'progress.json', result); return result

    def finish(self, info):
        if any(set(self.rows[k]) != set(self.ids) for k in self.keys): raise ValueError('Incomplete pinned cohort')
        complete = self.complete()
        if complete and complete['cohort'] == self.cohort and not complete.get('legacy'):
            return complete
        folder = self.root / 'cohorts' / self.cohort
        for key in self.keys:
            doc = dict(self.static[key], signature=self.signature, replicas=self.ids)
            if not doc.get('unavailable'):
                doc.update({field:[self.rows[key][i][field] for i in self.ids] for field in self.dynamic})
                doc['stats'] = {field:[self.rows[key][i]['stats'][field] for i in self.ids] for field in self.rows[key][self.ids[0]]['stats']}
            pc_write(folder / 'datasets' / self._name(key), doc)
        pc_write(folder / 'meta.json', {'version': self.version, 'info': info})
        if self._publish(folder, self.ids, self.signature): self.progress('complete')
        return self.complete()
