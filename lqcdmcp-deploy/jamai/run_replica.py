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
"""

from __future__ import annotations

import os
import json
import runpy
import sys
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


def main() -> None:
    cap = os.environ.get('JAMAI_MAX_NFEV', '').strip()
    method = os.environ.get('JAMAI_METHOD', '').strip()
    max_nfev = int(cap) if cap.isdigit() and int(cap) > 0 else None
    method = method if method in ('trf', 'dogbox') else None
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
