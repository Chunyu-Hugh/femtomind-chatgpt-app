"""Run one replica of a materialized stage: fitlib/ai.py with an iteration cap.

    python -m modules.jam.jamai.run_replica --manifest ... --output ... --replica N [-n 1]

fitlib/ai.py (the FitPack `ai` branch) fits through fitlib.maxlike, which
calls scipy's least_squares with method='trf' and no max_nfev, so the
composer's "Max iterations" and "Optimizer" had no effect. JAMAI_MAX_NFEV and
JAMAI_METHOD (trf or dogbox: the bounded methods) are applied by wrapping
least_squares before fitlib imports it.

While it fits, fitlib.maxlike also prints a status report
(fitlib.resman.RESMAN.gen_report). The report is output only, but it reads
each data table's 'col', 'obs' and 'target' columns; idis 10005 has no 'col',
so the report raised KeyError inside the fit and ai.py fell back to computing
chi2 at the starting values. The report is wrapped so that an error becomes
one line, "(status report unavailable: <type>: <message>)", and the fit goes
on. Everything else is ai.py unchanged.

Before the fit it writes replica_provenance.json into the replica's output
folder: the FitPack commit the array task found (the workspace can change while
a task waits in the queue), the image, the Slurm ids and the JAMAI files, so
that the replica can be run again exactly. ai.py records the seeds it used in
fit_summary.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import socket
import sys
import time
from pathlib import Path


def _wrap_least_squares(max_nfev: int | None, method: str | None) -> None:
    import scipy.optimize as opt
    from functools import wraps

    original = opt.least_squares

    def bind(core):
        @wraps(core)
        def wrapped(*args, **kwargs):
            if max_nfev:
                kwargs['max_nfev'] = max_nfev
            if method:
                kwargs['method'] = method
            return core(*args, **kwargs)
        # Allow the checkpoint observer to wrap the core without bypassing the
        # existing campaign evaluation cap or optimizer-method selection.
        wrapped._femtomind_solver = core
        wrapped._femtomind_options = {k: v for k, v in
                                      (('max_nfev', max_nfev), ('method', method)) if v}
        wrapped._femtomind_rebind = bind
        return wrapped

    opt.least_squares = bind(original)


def _guard_status_report() -> None:
    # Called after the least_squares wrap and the argv rewrite, where ai.py
    # would import fitlib.resman (lazily), so importing it here first is the
    # same module. If the import fails, ai.py reports that error itself.
    try:
        from fitlib.resman import RESMAN
        original = RESMAN.gen_report
    except Exception:
        return

    def gen_report(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        except Exception as exc:
            why = type(exc).__name__
            text = ' '.join(str(exc).split())
            if text:
                why += f': {text}'
            return [f'(status report unavailable: {why})']

    RESMAN.gen_report = gen_report


def _arg(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv[:-1] else None


def _version(package: str) -> str | None:
    try:
        from importlib.metadata import version
        return version(package)
    except Exception:
        return None


def _write_provenance(argv: list[str], max_nfev: int | None, method: str | None) -> None:
    # A record that cannot be written is one printed line, never a failed fit.
    try:
        output = _arg(argv, '--output')
        if not output:
            return
        here = os.path.dirname(os.path.abspath(__file__))
        jamai = {}
        for name in sorted(os.listdir(here)):
            if name.endswith('.py'):
                with open(os.path.join(here, name), 'rb') as fh:
                    jamai[name] = hashlib.sha256(fh.read()).hexdigest()
        changed = os.environ.get('JAMAI_FITPACK_CHANGED_FILES', '').strip()
        record = {
            'schema': 'femtomind.replica_provenance.v1',
            'replica': _arg(argv, '--replica'),
            'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'host': socket.gethostname(),
            'slurm': {'job_id': os.environ.get('SLURM_JOB_ID'),
                      'array_job_id': os.environ.get('SLURM_ARRAY_JOB_ID'),
                      'array_task_id': os.environ.get('SLURM_ARRAY_TASK_ID')},
            'fitpack': {'path': os.environ.get('FITPACK'),
                        'commit': os.environ.get('JAMAI_FITPACK_COMMIT') or None,
                        'changed_tracked_files': int(changed) if changed.isdigit() else (changed or None)},
            'image': os.environ.get('JAMAI_IMAGE') or None,
            'image_realpath': os.environ.get('JAMAI_IMAGE_REALPATH') or None,   # resolved outside the container
            'python': sys.version.split()[0],
            'libraries': {p: _version(p) for p in ('numpy', 'scipy', 'pandas')},
            'optimizer': {'max_nfev': max_nfev, 'method': method},
            'jamai_sha256': jamai,
            'seeds': 'fit_summary.json (seed, bootstrap_seed), written by fitlib/ai.py',
        }
        os.makedirs(output, exist_ok=True)
        with open(os.path.join(output, 'replica_provenance.json'), 'w', encoding='utf-8') as fh:
            json.dump(record, fh, indent=1)
    except Exception as exc:
        print(f'(replica provenance not written: {type(exc).__name__}: {exc})')


def main() -> None:
    cap = os.environ.get('JAMAI_MAX_NFEV', '').strip()
    method = os.environ.get('JAMAI_METHOD', '').strip()
    max_nfev = int(cap) if cap.isdigit() and int(cap) > 0 else None
    method = method if method in ('trf', 'dogbox') else None
    _write_provenance(sys.argv[1:], max_nfev, method)
    if max_nfev or method:
        _wrap_least_squares(max_nfev, method)
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--manifest')
    parser.add_argument('--replica', type=int, default=1)
    parser.add_argument('--output')
    parser.add_argument('--resume-plan')
    prior_args, _ = parser.parse_known_args()
    # This is an application-runner option, not a FitPack CLI option.
    if prior_args.resume_plan:
        for at, argument in enumerate(sys.argv):
            if argument == '--resume-plan':
                del sys.argv[at:at + 2]
                break
            if argument.startswith('--resume-plan='):
                del sys.argv[at]
                break
    if prior_args.manifest:
        from .campaign_manifest import _load_manifest_payload
        manifest_path = Path(prior_args.manifest)
        # The existing materializer emits JSON when optional PyYAML is absent.
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            manifest = _load_manifest_payload(manifest_path)
        if manifest.get('params', {}).get('prior_replicas'):
            from .prior_replicas import install
            install(prior_args.replica)
    from .replica_runtime import Runtime, RuntimeAbort
    resume_sha = None
    if prior_args.resume_plan:
        plan = json.loads(Path(prior_args.resume_plan).read_text())
        if plan.get('schema') != 'femtomind.resume-plan.v1' or plan.get('failures'):
            raise ValueError('Invalid checkpoint resume plan')
        resume_sha = plan['records'][str(prior_args.replica)]['sha256']
    runtime = Runtime(prior_args.manifest, prior_args.output, prior_args.replica, resume_sha)
    sys.argv = ['fitlib/ai.py', '--mode', 'fit'] + sys.argv[1:]
    _guard_status_report()
    try:
        runtime.install()
        runpy.run_module('fitlib.ai', run_name='__main__')
    except RuntimeAbort as exc:
        print('FEMTOMIND replica stopped: ' + str(exc), file=sys.stderr, flush=True)
        raise SystemExit(2)
    finally:
        runtime.close()


if __name__ == '__main__':
    main()
