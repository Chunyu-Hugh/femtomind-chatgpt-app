"""Run one replica of a materialized stage: fitlib/ai.py with an iteration cap.

    python -m modules.jam.jamai.run_replica --manifest ... --output ... --replica N [-n 1]

fitlib/ai.py (the FitPack `ai` branch) fits through fitlib.maxlike, which
calls scipy's least_squares with method='trf' and no max_nfev, so the
composer's "Max iterations" and "Optimizer" had no effect. JAMAI_MAX_NFEV and
JAMAI_METHOD (trf or dogbox: the bounded methods) are applied by wrapping
least_squares before fitlib imports it; everything else is ai.py unchanged.
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


def main() -> None:
    cap = os.environ.get('JAMAI_MAX_NFEV', '').strip()
    method = os.environ.get('JAMAI_METHOD', '').strip()
    max_nfev = int(cap) if cap.isdigit() and int(cap) > 0 else None
    method = method if method in ('trf', 'dogbox') else None
    if max_nfev or method:
        _wrap_least_squares(max_nfev, method)
    sys.argv = ['fitlib/ai.py', '--mode', 'fit'] + sys.argv[1:]
    runpy.run_module('fitlib.ai', run_name='__main__')


if __name__ == '__main__':
    main()
