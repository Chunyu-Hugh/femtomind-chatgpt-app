"""Bounded diagnostics and restartable objective checkpoints around native FitPack.

No optimizer mathematics or residual calculations are replaced. A checkpoint is
the last completed finite objective evaluation (possibly a finite-difference or
trial point), not an assertion of the solver's last accepted step/internal state.
"""
from __future__ import annotations

from collections import deque
import json
import math
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import uuid

from . import replica_checkpoint as store


class RuntimeAbort(BaseException):
    """Bypass FitPack's optimization-error fallback on checkpoint mismatch/stop."""


_COMPUTED = {'thy', 'prediction', 'shift', 'Shift', 'N', 'residuals',
             'r-residuals', 'n-residuals', 'sigtot'}


def table_identity(table):
    import numpy as np
    def clean(value):
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, (list, tuple)):
            return [clean(x) for x in value]
        if isinstance(value, float) and not math.isfinite(value):
            return {'nonfinite': repr(value)}
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        return str(value)
    return store.digest({str(k): {'shape': list(np.asarray(v).shape),
                                 'values': clean(np.asarray(v).tolist())}
                         for k, v in table.items() if k not in _COMPUTED})


class BoundedStream:
    """Append to the provided Slurm stream; never reopen/truncate its file."""
    def __init__(self, stream, limit, runtime, channel):
        self.stream, self.limit, self.runtime, self.channel = stream, limit, runtime, channel
        self.written = 0
        self.warned = False

    def write(self, text):
        text = str(text)
        self.runtime.tail.append((self.channel, text[-2048:]))
        data = text.encode('utf-8', errors='replace')
        room = max(0, self.limit - self.written)
        if room:
            shown = data[:room].decode('utf-8', errors='ignore')
            self.stream.write(shown)
            self.written += len(shown.encode('utf-8'))
        if len(data) > room and not self.warned:
            self.stream.write('\n[FEMTOMIND log budget reached; current diagnostics remain on Ceph.]\n')
            self.warned = True
        return len(text)

    def flush(self):
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


class Runtime:
    def __init__(self, manifest, output, replica, resume_sha=None,
                 checkpoint_seconds=30., progress_seconds=5., console_seconds=30.):
        self.stage = Path(manifest).resolve().parent
        self.output, self.replica = Path(output), int(replica)
        self.input_sha = store.file_hash(self.stage / 'input.py')
        self.root = store.checkpoint_root(self.stage, self.replica)
        self.attempt = 'attempt-' + uuid.uuid4().hex
        self.folder = self.root / self.attempt
        self.resume, self.payload = (store.resolve(self.stage, self.replica, resume_sha)
                                     if resume_sha else (None, None))
        self.bootstrap = {}; self.arrays = {}
        self.latest = None; self.evaluations = 0; self.offset = self.resume['evaluations'] if self.resume else 0
        self.last_checkpoint = self.last_progress = self.last_console = -math.inf
        self.checkpoint_seconds = checkpoint_seconds
        self.progress_seconds, self.console_seconds = progress_seconds, console_seconds
        self.tail = deque(maxlen=32)
        self.restores = []; self.bootstrap_path = None
        self.bootstrap_sha = None; self.current_metrics = {}
        self._undo = []; self._signals = {}; self._streams = None
        self.solver_result = None
        import multiprocessing
        self._previous_children = {p.pid for p in multiprocessing.active_children()}
        self.bootstrap_enabled = self.resume.get('bootstrap_enabled') if self.resume else None
        if self.resume:
            import numpy as np
            with np.load(self.payload, allow_pickle=False) as data:
                self.restore_meta = json.loads(str(data['metadata'].item()))
                self.restore_arrays = {k: data[k].copy() for k in data.files if k != 'metadata'}
        else:
            self.restore_meta, self.restore_arrays = {}, {}

    def _abort(self, exc):
        raise RuntimeAbort(str(exc)) from exc

    def capture_reaction(self, residual):
        """Called after native sampling, before RESMAN starts/forks workers."""
        from tools.config import conf
        enabled = bool(conf.get('bootstrap', False))
        if self.bootstrap_enabled is None:
            self.bootstrap_enabled = enabled
        if enabled != self.bootstrap_enabled:
            if self.latest is not None:  # Native post-fit central-data diagnostic.
                return
            raise RuntimeAbort('Checkpoint bootstrap mode changed')
        import numpy as np
        for idx, table in residual.tabs.items():
            key = str(residual.reaction) + ':' + str(idx)
            original = residual.original[idx]
            identity = table_identity(original)
            if self.resume:
                entry = self.restore_meta.get(key)
                if not entry or entry['original_sha256'] != identity:
                    raise RuntimeAbort('Checkpoint experimental identity changed: ' + key)
                values = self.restore_arrays[entry['array']]
                if values.shape != np.asarray(table['value']).shape:
                    raise RuntimeAbort('Checkpoint bootstrap point count changed: ' + key)
                table['value'] = values.copy()
                if entry.get('deviation'):
                    conf.setdefault('deviation-buffer', {}).setdefault(residual.reaction, {})[idx] = self.restore_arrays[entry['deviation']].copy()
            # Repeated RESMAN construction (audit and fit) must reproduce the same realization.
            values = np.asarray(table['value']).copy()
            if key in self.bootstrap:
                entry = self.bootstrap[key]
                if entry['original_sha256'] != identity or not np.array_equal(self.arrays[entry['array']], values):
                    raise RuntimeAbort('Bootstrap changed between runtime initializations: ' + key)
                continue
            name = 'values_' + str(len(self.bootstrap))
            entry = {'array': name, 'original_sha256': identity}
            self.arrays[name] = values
            deviations = conf.get('deviation-buffer', {}).get(residual.reaction, {}).get(idx)
            if deviations is not None:
                dname = 'deviation_' + str(len(self.bootstrap))
                entry['deviation'] = dname; self.arrays[dname] = np.asarray(deviations).copy()
            self.bootstrap[key] = entry

    def save_bootstrap(self, owner=None):
        if self.bootstrap_path is not None:
            return
        import numpy as np
        from tools.config import conf
        model = getattr(owner, 'resman', None)
        if model is not None:
            expected = {str(res.reaction) + ':' + str(idx)
                        for res in vars(model).values()
                        if hasattr(res, 'reaction') and isinstance(getattr(res, 'tabs', None), dict)
                        for idx, tab in res.tabs.items() if 'value' in tab}
            if not expected or expected != set(self.bootstrap):
                raise RuntimeAbort('Checkpoint cannot capture every active experimental dataset')
        if conf.get('bootstrap') and not self.bootstrap:
            raise RuntimeAbort('No bootstrap realization captured; checkpointing refused')
        if self.resume and set(self.bootstrap) != set(self.restore_meta):
            raise RuntimeAbort('Checkpoint dataset selection changed')
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / 'bootstrap.npz'
        fd, tmp = tempfile.mkstemp(prefix='bootstrap.', suffix='.tmp', dir=self.folder)
        try:
            with os.fdopen(fd, 'wb') as stream:
                np.savez_compressed(stream, metadata=np.array(json.dumps(self.bootstrap, sort_keys=True)), **self.arrays)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        self.bootstrap_path = path
        self.bootstrap_sha = store.file_hash(path)

    def starting_values(self, owner, guess):
        if not self.resume:
            return guess
        import numpy as np
        from tools.config import conf
        def identity(k): return (int(k[0]), str(k[1]), str(k[2]))
        positions = {identity(k): i for i, k in enumerate(self.resume['order'])}
        order = [identity(k) for k in owner.parman.order]
        if set(order) != set(positions) or len(order) != len(positions):
            raise RuntimeAbort('Checkpoint independent parameter identities changed')
        values = np.array([self.resume['values'][positions[k]] for k in order], dtype=float)
        for k, value in zip(order, values):
            definition = conf['params'][k[1]][k[2]] if k[0] == 1 else conf['datasets'][k[1]]['norm'][int(k[2])]
            if definition.get('fixed') is not False or not definition['min'] <= value <= definition['max']:
                raise RuntimeAbort('Checkpoint fixed/link status or bounds incompatible: ' + str(k))
        owner.parman.set_new_params(values, initial=True)
        self.output.mkdir(parents=True, exist_ok=True)
        store.atomic_json(self.output / 'resume_initialization.json',
                          {'checkpoint_sha256': self.resume['sha256'], 'replica': self.replica,
                           'order': [list(k) for k in order], 'effective_start': values.tolist(),
                           'bootstrap_sha256': self.resume['bootstrap_sha256'],
                           'optimizer_state_restored': False})
        self.restores.append(values.tolist())
        return values

    def evaluated(self, owner, values, residuals):
        import numpy as np
        from tools.config import conf
        self.evaluations += 1
        if np.iscomplexobj(values) or np.iscomplexobj(residuals):
            return  # Complex-step probes are not real restart points.
        values, residuals = np.asarray(values, dtype=float), np.asarray(residuals, dtype=float)
        if not np.isfinite(values).all() or not np.isfinite(residuals).all():
            return  # Never replace a valid checkpoint with an unusable evaluation.
        self.save_bootstrap(owner)
        order = [[int(k[0]), str(k[1]), str(k[2])] for k in owner.parman.order]
        if len(order) != values.size:
            raise RuntimeAbort('Objective parameter/order length mismatch')
        self.latest = {'schema': store.SCHEMA, 'replica': self.replica,
                       'campaign': store.campaign_identity(self.stage),
                       'input_sha256': self.input_sha, 'order': order, 'values': values.tolist(),
                       'evaluations': self.offset + self.evaluations,
                       'attempt_evaluations': self.evaluations,
                       'objective': float(np.dot(residuals.ravel(), residuals.ravel())),
                       'bootstrap_enabled': bool(conf.get('bootstrap')),
                       'bootstrap_seed': conf.get('jamai runtime bootstrap seed'),
                       'bootstrap_file': self.attempt + '/bootstrap.npz',
                       'bootstrap_sha256': self.bootstrap_sha,
                       'checkpoint_point': 'last_completed_objective_evaluation',
                       'optimizer_state_restored': False, 'metrics': dict(self.current_metrics),
                       'updated_at': time.time()}
        self.flush()

    def flush(self, force=False, status='optimizing'):
        if self.latest is None:
            return
        now = time.monotonic()
        try:
            if force or now - self.last_checkpoint >= self.checkpoint_seconds:
                doc = dict(self.latest); doc['sha256'] = store.digest(doc)
                store.atomic_json(self.folder / 'checkpoint.json', doc)
                store.atomic_json(self.root / 'latest.json', doc)
                self.last_checkpoint = now
            if force or now - self.last_progress >= self.progress_seconds:
                store.atomic_json(self.root / 'current_progress.json',
                                  {**self.latest, 'status': status, 'solver_result': self.solver_result})
                store.atomic_json(self.root / 'diagnostic_tail.json', {'text': list(self.tail)})
                self.last_progress = now
            if force or now - self.last_console >= self.console_seconds:
                print('[FEMTOMIND] replica=%d evaluations=%d objective=%.10g status=%s checkpoint=%s' %
                      (self.replica, self.latest['evaluations'], self.latest['objective'], status, self.root / 'latest.json'), flush=True)
                self.last_console = now
        except Exception as exc:
            self._abort(exc)

    def install(self):
        from tools.residuals import _RESIDUALS
        from fitlib.maxlike import MAXLIKE
        def replace(obj, name, value):
            self._undo.append((obj, name, getattr(obj, name))); setattr(obj, name, value)
        setup = _RESIDUALS.setup
        def setup_wrapped(residual, *args, **kwargs):
            result = setup(residual, *args, **kwargs)
            try: self.capture_reaction(residual)
            except Exception as exc: self._abort(exc)
            return result
        replace(_RESIDUALS, 'setup', setup_wrapped)
        guess = MAXLIKE.get_guess
        def starting(owner, *args, **kwargs):
            try: return self.starting_values(owner, guess(owner, *args, **kwargs))
            except Exception as exc: self._abort(exc)
        replace(MAXLIKE, 'get_guess', starting)
        def status(owner, res, rres, nres):
            import numpy as np
            data, cor, norm = (float(np.sum(np.asarray(x)**2)) for x in (res, rres, nres))
            total = data + cor + norm
            delta = total - owner.chi2tot
            if owner.parman.shifts > 2 and total < owner.chi2tot:
                owner.dchi2, owner.chi2tot = owner.chi2tot - total, total
            self.current_metrics = {'data': data, 'correlated': cor, 'normalization': norm,
                                    'objective': total, 'delta': delta if math.isfinite(delta) else None,
                                    'native_count': owner.cnt}
            return [], []
        replace(MAXLIKE, 'print_status', status)
        # maxlike imported least_squares by name: wrap that binding, not only SciPy's.
        import fitlib.maxlike as module
        original_solver = module.least_squares
        def solver(fun, x0, *args, **kwargs):
            owner = getattr(fun, '__self__', None)
            if owner is None or not hasattr(owner, 'parman'):
                raise RuntimeAbort('Unsupported optimizer objective; no checkpoint owner')
            def evaluate(values, *a, **k):
                # Copy x: SciPy can reuse/mutate finite-difference work arrays.
                import numpy as np
                point = np.asarray(values).copy()
                residuals = fun(values, *a, **k)
                self.evaluated(owner, point, residuals)
                return residuals
            try:
                if self.resume:
                    import numpy as np
                    # Verify before handing x0 to SciPy (which may internally
                    # move boundary points into the feasible interior).
                    residuals = fun(x0)
                    objective = float(np.dot(np.asarray(residuals).ravel(), np.asarray(residuals).ravel()))
                    if objective != self.resume['objective']:
                        raise RuntimeAbort('Checkpoint objective changed at the restored parameters/bootstrap')
                result = original_solver(evaluate, x0, *args, **kwargs)
                self.solver_result = {k: (getattr(result, k).item() if hasattr(getattr(result, k, None), 'item') else getattr(result, k, None))
                                      for k in ('success', 'status', 'message', 'nfev', 'njev', 'optimality')}
                self.flush(True, 'optimizer_returned')
                return result
            finally:
                self.flush(True, 'optimizer_stopped')
        replace(module, 'least_squares', solver)
        self._streams = sys.stdout, sys.stderr
        sys.stdout = BoundedStream(sys.stdout, 1024*1024, self, 'stdout')
        sys.stderr = BoundedStream(sys.stderr, 512*1024, self, 'stderr')
        def stopping(number, frame):
            self.flush(True, 'interrupted')
            raise RuntimeAbort('Replica interrupted by signal %d; restart checkpoint saved' % number)
        for number in (signal.SIGTERM, signal.SIGINT):
            self._signals[number] = signal.getsignal(number); signal.signal(number, stopping)
        return self

    def close(self):
        try:
            self.flush(True, 'runner_stopped')
        finally:
            # FitPack does not guarantee worker shutdown when optimization raises.
            # Terminate/join only child workers created by this Python runner;
            # this never sends a signal to any other replica or Slurm allocation.
            import multiprocessing
            children = [p for p in multiprocessing.active_children() if p.pid not in self._previous_children]
            for child in children:
                child.terminate()
            for child in children:
                child.join(timeout=3)
                if child.is_alive():
                    child.kill(); child.join(timeout=3)
            for obj, name, old in reversed(self._undo): setattr(obj, name, old)
            for number, old in self._signals.items(): signal.signal(number, old)
            if self._streams:
                sys.stdout, sys.stderr = self._streams
            store.atomic_json(self.folder / 'diagnostic_tail.json', {'text': list(self.tail)})
