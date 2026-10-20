"""Cached replica predictions behind the Analysis tools.

Evaluating every fitted replica's theory for every dataset takes minutes, so
it is done once per campaign and kept on the cluster, next to the campaign:

  <campaign>/analysis/cache/meta.json             every dataset of the campaign:
                                                   experiment, observable, target,
                                                   points before cuts
  <campaign>/analysis/cache/datasets/<r>_<id>.json one dataset: its columns, each
                                                   fitted replica's prediction and
                                                   shift, and its chi2 terms

A dataset file is valid while the campaign's fitted replicas are the ones it was
made from (signature: replica folder and .msr modification time); appended or
refitted replicas make it stale and only that dataset is computed again.

Everything here is stdlib only: the gateway ships this module's source to the
cluster in front of its scripts (the plain python3 reader and the container
builder) and the tests import it directly.
"""

import json
import math
import os
import re
from statistics import NormalDist

VERSION = 2                         # 2: datasets carry the point-identity check
# Credible intervals offered by the interactive charts, besides the requested one.
LEVELS = (68.27, 90.0, 95.0, 99.0, 100.0)
# Columns that are uncertainties or bookkeeping, never a kinematic axis.
_NOT_AXIS = re.compile(r'(_u$|_c$|%|^stat|^syst|norm|^alpha$|^value$|^idx$|^i$|^units?$|^col$|^obs$|^target$'
                       r'|^thy|^prediction$|^shift$|^N$|residuals)', re.I)


def key_of(reaction, dataset_id):
    return '%s:%s' % (reaction, dataset_id)


def file_of(cache, key):
    return os.path.join(cache, 'datasets', re.sub(r'[^A-Za-z0-9._-]', '_', key.replace(':', '_')) + '.json')


def read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(path, doc):
    """Atomic: a reader never sees half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = '%s.%d.tmp' % (path, os.getpid())
    with open(tmp, 'w') as fh:
        json.dump(doc, fh, separators=(',', ':'))
    os.replace(tmp, path)


def fitted_replicas(stage, replica_state):
    """(records, excluded): every replica that really fitted and still has its
    .msr, in folder order; the others counted by why they are left out."""
    records, excluded = [], {'not_fitted': 0, 'failed': 0, 'missing_msr': 0, 'missing_summary': 0}
    folder = os.path.join(stage, 'current')
    names = sorted((n for n in (os.listdir(folder) if os.path.isdir(folder) else [])
                    if n.startswith('replica_')), key=lambda n: (len(n), n))
    for name in names:
        directory = os.path.join(folder, name)
        if not os.path.isdir(directory):
            continue
        state, summary = replica_state(os.path.join(directory, 'fit_summary.json'))
        if state != 'fitted':
            excluded[state or 'missing_summary'] += 1
        elif not summary.get('msr_path') or not os.path.isfile(summary['msr_path']):
            excluded['missing_msr'] += 1
        else:
            records.append(dict(summary, identity=name))
    return records, excluded


def signature(records):
    return [[s['identity'], int(os.path.getmtime(s['msr_path']))] for s in records]


# ---- replica selection ------------------------------------------------------
#
# The statistics can use a subset of the fitted replicas, as JAM analyses do
# when they leave out replicas with a poor fit. The selection never changes a
# fit or the cache (which keeps every replica); only what is averaged and what
# the bands are made of. Rules, applied in this order, each to what is left:
#   only     [numbers]   keep just these replicas
#   exclude  [numbers]   leave these out
#   chi2_max x           leave out replicas whose total chi2 is above x
#   sigma    k           leave out outliers: total chi2 above median + k * 1.4826 * MAD
#   best     n           keep the n replicas with the lowest total chi2

SELECTION_KEYS = ('only', 'exclude', 'chi2_max', 'sigma', 'best')


def replica_number(identity):
    m = re.search(r'(\d+)$', str(identity))
    return int(m.group(1)) if m else None


def select_replicas(records, selection):
    """(kept, dropped) for records with identity and chi2_total: kept in their
    original order; dropped as [{replica, chi2_total, reason}]."""
    if not selection:
        return list(records), []
    kept, dropped = list(records), []
    chi2 = lambda s: float(s['chi2_total']) if s.get('chi2_total') is not None else float('inf')

    def drop(rule, keep):
        nonlocal kept
        out = [s for s in kept if not keep(s)]
        dropped.extend({'replica': replica_number(s['identity']), 'chi2_total': s.get('chi2_total'), 'reason': rule} for s in out)
        kept = [s for s in kept if keep(s)]

    if selection.get('only'):
        only = set(int(i) for i in selection['only'])
        drop('not in only', lambda s: replica_number(s['identity']) in only)
    if selection.get('exclude'):
        ex = set(int(i) for i in selection['exclude'])
        drop('excluded', lambda s: replica_number(s['identity']) not in ex)
    if selection.get('chi2_max') is not None:
        cap = float(selection['chi2_max'])
        drop('chi2 > %g' % cap, lambda s: chi2(s) <= cap)
    if selection.get('sigma') is not None and len(kept) > 2:
        values = sorted(chi2(s) for s in kept)
        med = values[len(values) // 2] if len(values) % 2 else (values[len(values) // 2 - 1] + values[len(values) // 2]) / 2
        dev = sorted(abs(v - med) for v in values)
        mad = dev[len(dev) // 2] if len(dev) % 2 else (dev[len(dev) // 2 - 1] + dev[len(dev) // 2]) / 2
        cut = med + float(selection['sigma']) * 1.4826 * mad
        drop('chi2 > median + %g sigma (%.6g)' % (float(selection['sigma']), cut), lambda s: chi2(s) <= cut)
    if selection.get('best') is not None:
        n = int(selection['best'])
        best = set(id(s) for s in sorted(kept, key=chi2)[:n])
        drop('not among the best %d' % n, lambda s: id(s) in best)
    return kept, dropped


def selection_report(selection, kept, total, dropped):
    return {'applied': selection, 'kept': len(kept), 'of': total,
            'kept_replicas': [replica_number(s['identity']) for s in kept], 'dropped': dropped}


def subset_doc(doc, keep):
    """A cached dataset limited to the replicas (identities) in keep."""
    ids = doc.get('replicas') or []
    idx = [i for i, r in enumerate(ids) if r in keep]
    if len(idx) == len(ids):
        return doc
    out = dict(doc, replicas=[ids[i] for i in idx])
    for k in ('theory', 'shift'):
        out[k] = [doc[k][i] for i in idx]
    out['stats'] = {k: [v[i] for i in idx] for k, v in doc['stats'].items()}
    return out


# ---- names as people say them ------------------------------------------------

def norm_name(text):
    """'DØ', 'D0', 'd0 ' and 'd-0' are one name."""
    text = str(text).lower().replace('\u00f8', '0').replace('\u00d8', '0')
    return re.sub(r'[^a-z0-9]', '', text)


def _dataset_words(text):
    """'idis:10010', 'idis 10010', 'IDIS-10010' or a bare '10010'."""
    m = re.fullmatch(r'\s*([A-Za-z][A-Za-z0-9_-]*?)?[\s:/_-]*(\d+)\s*', str(text))
    return (m.group(1) or '', m.group(2)) if m else None


def select(info, datasets=(), experiments=()):
    """The dataset keys of info (key -> {reaction, dataset_id, experiment})
    that the request names, in campaign order, with the names that matched
    nothing. datasets: 'reaction:id' (or a bare id when only one reaction has
    it); experiments: compared by norm_name, a prefix matches ('E866' finds
    'E866pp'). Both empty: every dataset."""
    keys = list(info)
    if not datasets and not experiments:
        return keys, [], []
    chosen, unknown, unmatched = set(), [], []
    for text in datasets:
        words = _dataset_words(text)
        hits = []
        if words:
            reaction, number = words
            hits = [k for k in keys if str(info[k]['dataset_id']) == number
                    and (not reaction or norm_name(info[k]['reaction']) == norm_name(reaction))]
        if hits:
            chosen.update(hits)
        else:
            unknown.append(str(text))
    for text in experiments:
        want = norm_name(text)
        hits = [k for k in keys if want and norm_name(info[k].get('experiment', '')).startswith(want)]
        if not hits:
            hits = [k for k in keys if want and want in norm_name(info[k].get('experiment', ''))]
        if hits:
            chosen.update(hits)
        else:
            unmatched.append(str(text))
    return [k for k in keys if k in chosen], unknown, unmatched


def catalog(info):
    """What the campaign has, for a request that named nothing it has."""
    return [{'dataset': k, 'experiment': v.get('experiment', ''), 'observable': v.get('observable', ''),
             'target': v.get('target', '')} for k, v in info.items()]


# ---- statistics -------------------------------------------------------------

def percentile(ordered, q):
    """numpy.percentile's default (linear) on an already sorted list."""
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def mean(values):
    return sum(values) / len(values) if values else None


def std(values):
    if len(values) < 2:
        return None
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def _chi2_cdf_sf(x, k):
    """(P, Q) of the chi2 distribution with k degrees of freedom at x: the
    regularized incomplete gamma functions, each computed directly so that
    neither loses its digits to 1 - the other."""
    if x <= 0:
        return 0.0, 1.0
    a, z = k / 2.0, x / 2.0
    front = math.exp(-z + a * math.log(z) - math.lgamma(a))
    if z < a + 1:
        term = total = 1.0 / a
        n = a
        for _ in range(100000):
            n += 1
            term *= z / n
            total += term
            if abs(term) < abs(total) * 1e-16:
                break
        p = front * total
        return p, 1.0 - p
    tiny = 1e-300
    b = z + 1 - a
    c, d = 1 / tiny, 1 / b
    h = d
    for i in range(1, 100000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = d if abs(d) > tiny else tiny
        c = b + an / c
        c = c if abs(c) > tiny else tiny
        d = 1 / d
        step = d * c
        h *= step
        if abs(step - 1) < 1e-16:
            break
    q = front * h
    return 1.0 - q, q


def z_score(chi2, npts):
    """The chi2 of npts points as a standard normal deviate (JAM convention:
    negative below the expected chi2, positive above it)."""
    if not npts or chi2 is None or not math.isfinite(chi2):
        return None
    p, q = _chi2_cdf_sf(chi2, npts)
    unit = NormalDist()
    if 1e-300 < p < 0.5:
        return unit.inv_cdf(p)
    if 1e-300 < q <= 0.5:
        return -unit.inv_cdf(q)
    # Far in a tail: the Wilson-Hilferty approximation.
    s = 2.0 / (9 * npts)
    return ((chi2 / npts) ** (1 / 3.0) - (1 - s)) / math.sqrt(s)


def table_row(doc, best=None):
    """One line of the per-dataset table (as in the JAM papers): points after and
    before cuts, correlated systematics, reduced chi2 and its Z-score, the
    chi2 of the correlated shifts and of the normalization, and the fitted
    normalization as mean(std). chi2 terms are means over the fitted
    replicas, each replica against the central data; chi2_best is the best
    replica's (the replica identity best, lowest total chi2)."""
    st, n = doc['stats'], doc['npts']
    chi2 = mean(st['chi2'])
    replicas = doc.get('replicas') or []
    chi2_best = st['chi2'][replicas.index(best)] if best in replicas else None
    norm = [v for v in st.get('norm') or [] if v is not None]
    return {'dataset': doc['key'], 'reaction': doc['reaction'], 'dataset_id': doc['dataset_id'],
            'experiment': doc.get('experiment', ''), 'observable': doc.get('observable', ''),
            'target': doc.get('target', ''), 'npts': n, 'npts_raw': doc.get('npts_raw'),
            'ncor': doc.get('ncor', 0), 'chi2': chi2, 'chi2_red': chi2 / n if n else None,
            'z_score': z_score(chi2, n), 'chi2_cor': mean(st['chi2_cor']), 'chi2_norm': mean(st['chi2_norm']),
            'norm': mean(norm) if doc.get('norm_fitted') and norm else None,
            'norm_std': std(norm) if doc.get('norm_fitted') and norm else None,
            'chi2_best': chi2_best, 'point_order_sha256': doc.get('point_order_sha256')}


def by_experiment(rows):
    """chi2/N of the best replica grouped by experiment, worst first: the
    analysis-by-experiment table (a best-replica diagnostic, not the chi2 of
    the ensemble mean)."""
    groups = {}
    for r in rows:
        if r.get('chi2_best') is None:
            continue
        g = groups.setdefault(r['experiment'] or 'unknown',
                              {'experiment': r['experiment'] or 'unknown', 'datasets': [], 'chi2': 0.0, 'npts': 0})
        g['datasets'].append({'reaction': r['reaction'], 'dataset_id': r['dataset_id'], 'target': r['target'],
                              'observable': r['observable'], 'npts': r['npts'], 'chi2': r['chi2_best'],
                              'chi2_per_npt': r['chi2_best'] / r['npts'] if r['npts'] else None,
                              'point_order_sha256': r.get('point_order_sha256')})
        g['chi2'] += r['chi2_best']
        g['npts'] += r['npts']
    out = list(groups.values())
    for g in out:
        g['chi2_per_npt'] = g['chi2'] / g['npts'] if g['npts'] else None
    return sorted(out, key=lambda g: -(g['chi2_per_npt'] or 0))


def _r(v):
    return None if v is None or not math.isfinite(v) else float('%.6g' % v)


def chart_data(doc, interval):
    """A dataset for the interactive data vs theory chart: the varying columns
    (the axes to choose from), the constant ones (for the caption), the data
    with its uncertainty, the mean prediction and its pointwise bands at
    LEVELS and at the requested interval. The replicas' arrays stay on the
    cluster."""
    n, theory = doc['npts'], doc['theory']
    levels = sorted(set(LEVELS) | {float(interval)})
    mean_p, shift_p, bands = [], [], {('%g' % L): [[], []] for L in levels}
    for j in range(n):
        vals = sorted(row[j] for row in theory)
        mean_p.append(_r(sum(vals) / len(vals)))
        shift_p.append(_r(mean([row[j] for row in doc['shift']])))
        if len(vals) > 1:
            for L in levels:
                lo, hi = bands['%g' % L]
                lo.append(_r(percentile(vals, (100 - L) / 2)))
                hi.append(_r(percentile(vals, 100 - (100 - L) / 2)))
    columns, constants = {}, {}
    for name, values in doc['columns'].items():
        if name in ('value', 'alpha') or _NOT_AXIS.search(name):
            continue
        distinct = set(values)
        if len(distinct) == 1:
            constants[name] = values[0]
        elif all(isinstance(v, (int, float)) or v is None for v in values):
            columns[name] = values
        elif len(distinct) <= 12:
            columns[name] = [str(v) for v in values]
    return {'dataset': doc['key'], 'reaction': doc['reaction'], 'dataset_id': doc['dataset_id'],
            'experiment': doc.get('experiment', ''), 'observable': doc.get('observable', ''),
            'target': doc.get('target', ''), 'npts': n, 'n_replicas': len(theory),
            'value': doc['columns']['value'], 'alpha': doc['columns']['alpha'],
            'mean': mean_p, 'shift': shift_p, 'bands': bands if len(theory) > 1 else {},
            'columns': columns, 'constants': constants}
