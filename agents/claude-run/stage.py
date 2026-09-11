import json, glob, os, sys
fs = sorted(glob.glob('raw/*orchestrate_jam25.json'), key=os.path.getmtime)
if not fs:
    print('NOFILE'); sys.exit()
f = fs[-1]
try:
    d = json.load(open(f))
except Exception:
    print('BADRESPONSE', repr(open(f).read()[:200])); sys.exit()
parts = [str(d.get('stage') or d.get('status') or 'UNKNOWN')]
for k in ('step', 'job_id', 'retry', 'message', 'chi2N_so_far', 'prior_msr'):
    if d.get(k) not in (None, '', []):
        parts.append(f'{k}={d[k]}')
print(' '.join(parts))
