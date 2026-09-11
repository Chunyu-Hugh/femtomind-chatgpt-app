"""Analyze an L2 msr: per-dataset chi2 table + norms + params.
Usage: python3 analyze_msr.py <msr-path>
Run on ifarm (fitpack python) or in container.
"""
import sys
sys.path.insert(0, '/w/ceph24/JAM/chunyu/fitpack_legacy')
import numpy as np
from tools.tools import load

msr = sys.argv[1]
d = load(msr)

print('== chi2 by dataset ==')
tot_chi2 = 0.0
tot_npts = 0
for reaction in sorted(d['chi2']):
    for idx in sorted(d['chi2'][reaction]):
        c = d['chi2'][reaction][idx]
        chi2 = c.get('chi2', np.nan)
        npts = c.get('npts', np.nan)
        tot_chi2 += chi2
        tot_npts += npts
        print('%-10s %-6s npts=%4d chi2=%8.2f chi2/N=%6.3f' % (reaction, idx, npts, chi2, chi2 / npts))
print('TOTAL npts=%d chi2=%.2f chi2/N=%.3f' % (tot_npts, tot_chi2, tot_chi2 / tot_npts))

print()
print('== final step params ==')
last = max(d['order'].keys())
order = d['order'][last]
vals = d['params'][last]
for (typ, dist, par), v in zip(order, vals):
    if typ == 2:
        print('norm %-10s %-6s = %.4f' % (dist, par, v))
    else:
        print('%-14s %-10s = %.4f' % (dist, par, v))
