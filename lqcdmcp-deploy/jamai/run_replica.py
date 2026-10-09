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
import runpy
import sys


def _wrap_least_squares(max_nfev: int | None, method: str | None) -> None:
    import scipy.optimize as opt

    original = opt.least_squares

    def wrapped(*args, **kwargs):
        if max_nfev:
            kwargs['max_nfev'] = max_nfev
        if method:
            kwargs['method'] = method
        return original(*args, **kwargs)

    opt.least_squares = wrapped


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
    sys.argv = ['fitlib/ai.py', '--mode', 'fit'] + sys.argv[1:]
    _guard_status_report()
    runpy.run_module('fitlib.ai', run_name='__main__')


if __name__ == '__main__':
    main()
