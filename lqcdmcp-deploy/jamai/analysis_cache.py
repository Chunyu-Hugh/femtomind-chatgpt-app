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
made from (signature: replica identity, .msr content and summary metadata, plus
the stage input content). The incremental prediction store reuses unchanged
replica rows, journals missing rows, and publishes complete pinned cohorts.
The legacy dataset files stay readable and untouched. Timestamps are not
scientific source identities.

Everything here is stdlib only: the gateway ships this module's source to the
cluster in front of its scripts (the plain python3 reader and the container
builder) and the tests import it directly.
"""

import json
import hashlib
import math
import os
import re
from statistics import NormalDist

VERSION = 2                         # 2: datasets carry the point-identity check
METRICS_VERSION = 1                 # Full-precision, central-data objective components.
SCORING_VERSION = 2                 # Report/rank only central point residuals.
CHI2_BASIS = 'central_data_point_residuals'
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


def file_fingerprint(path):
    """Content identity: replacements within one timestamp tick must invalidate."""
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def signature(records, stage=None):
    rows = [[s['identity'], file_fingerprint(s['msr_path']),
             # Selection/table metadata may change without changing the .msr.
             hashlib.sha256(json.dumps(s, sort_keys=True, separators=(',', ':'),
                                       allow_nan=False).encode()).hexdigest()] for s in records]
    if stage is not None:
        rows.insert(0, ['input.py', file_fingerprint(os.path.join(stage, 'input.py'))])
    return rows


# ---- replica selection ------------------------------------------------------
#
# The statistics can use a subset of the fitted replicas, as JAM analyses do
# when they leave out replicas with a poor fit. The selection never changes a
# fit or the cache (which keeps every replica); only what is averaged and what
# the bands are made of. Rules, applied in this order, each to what is left:
#   only     [numbers]   keep just these replicas
#   exclude  [numbers]   leave these out
#   chi2_max x           leave out replicas whose central point chi2 is above x
#   sigma    k           leave outliers above median + k * 1.4826 * MAD of point chi2
#   best     n           keep the n replicas with the lowest central point chi2

SELECTION_KEYS = ('only', 'exclude', 'chi2_max', 'sigma', 'best')


def replica_number(identity):
    m = re.search(r'(\d+)$', str(identity))
    return int(m.group(1)) if m else None


def select_replicas(records, selection, stage=None, score_cache=None):
    """Keep original order; use central_chi2_reported or the pinned stage ledger.

    Legacy chi2_total may be bootstrap data-only and is never a score fallback.
    """
    if not selection:
        return list(records), []
    kept, dropped = list(records), []
    scores = central_scores(stage, records, score_cache) if stage is not None and any(k in selection for k in ('best', 'sigma', 'chi2_max')) else {}
    def chi2(s):
        value = scores.get(s['identity'], s.get('central_chi2_reported'))
        if value is None or not math.isfinite(float(value)):
            raise ValueError('Central-data point-residual chi2 is required for scientific replica selection; bootstrap or penalty-inclusive scores are not a fallback.')
        return float(value)

    def drop(rule, keep):
        nonlocal kept
        out = [s for s in kept if not keep(s)]
        dropped.extend({'replica': replica_number(s['identity']), 'chi2_total': scores.get(s['identity'], s.get('central_chi2_reported')),
                        'chi2_basis': CHI2_BASIS, 'reason': rule} for s in out)
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


def central_scores(stage, records, cache=None):
    """Revision-bound point-only central scores; reject old inclusive ledgers."""
    root = os.path.join(os.path.dirname(os.path.dirname(stage)), 'analysis', 'cache')
    paths = [os.path.join(root, 'central_metrics.json')]
    active = read_json(os.path.join(root, 'incremental', 'active.json')) or {}
    token = active.get('cohort', '')
    if re.fullmatch(r'[0-9a-f]{64}', token):
        paths.insert(0, os.path.join(root, 'incremental', 'cohorts', token, 'central_metrics.json'))
    if cache is not None: paths = [os.path.join(cache, 'central_metrics.json')]
    sig = signature(records, stage)
    for path in paths:
        doc = read_json(path) or {}
        if (doc.get('metrics_version') == METRICS_VERSION and doc.get('scoring_version') == SCORING_VERSION
                and doc.get('chi2_basis') == CHI2_BASIS and doc.get('signature') == sig):
            return {r['identity']: r['chi2_reported'] for r in doc['individual']}
    raise ValueError('Central-data metrics are missing or stale; refresh campaign_summary before chi2-based selection.')


def central_report(docs, records, selection=None, allow_empty=False):
    """Point-only reported chi2 and separately named full objective diagnostics.

    Native FitPack profiles each replica against central data. Ensemble penalties
    are squares of mean signed nuisance/normalization residuals, not mean squares.
    For fixed additive beta (V0/beta=1), mean profiling equals reprofiling the mean.
    Covariance-mode objective residuals are whitened natively; their mean preserves
    the same covariance and avoids double counting uncertainties/normalizations.
    """
    identities = [r['identity'] for r in records]
    if len(set(identities)) != len(identities) or len({d['key'] for d in docs}) != len(docs):
        raise ValueError('Duplicate replica or dataset identities.')
    individual = {k: {'identity': k, 'replica': replica_number(k), 'data': 0., 'objective_data': 0., 'correlated': 0., 'normalization': 0.} for k in identities}
    available, zero, per_dataset = [], [], {}
    for doc in docs:
        if doc.get('unavailable'):
            zero.append(doc['key']); continue
        if doc.get('metrics_version') != METRICS_VERSION or doc.get('replicas') != identities:
            raise ValueError('Incomplete/misaligned central-data metrics: ' + doc['key'])
        n = doc['npts']; st = doc['stats']; columns = doc['columns']
        if len(columns['value']) != n or len(columns['alpha']) != n or any(
                x is None or not math.isfinite(x) for x in columns['value']) or any(
                x is None or not math.isfinite(x) or x <= 0 for x in columns['alpha']):
            raise ValueError('Invalid central point count/data/uncertainties: ' + doc['key'])
        if len(doc['theory']) != len(identities) or any(len(row) != n for row in doc['theory']):
            raise ValueError('Prediction shape mismatch: ' + doc['key'])
        if any(not math.isfinite(float(x)) for row in doc['theory'] for x in row):
            raise ValueError('Nonfinite central prediction: ' + doc['key'])
        if any(len(st[name]) != len(identities) for name in ('chi2', 'chi2_cor', 'chi2_norm', 'norm')):
            raise ValueError('Penalty/replica alignment mismatch: ' + doc['key'])
        if len(doc['nuisance_residuals']) != len(identities) or len(doc['normalization_residuals']) != len(identities):
            raise ValueError('Penalty/replica shape mismatch: ' + doc['key'])
        if len(set(doc['nuisance_order'])) != len(doc['nuisance_order']):
            raise ValueError('Duplicate nuisance identity: ' + doc['key'])
        per_dataset[doc['key']] = []
        for i, key in enumerate(identities):
            residual = [(d-p)/a for d,p,a in zip(columns['value'],doc['theory'][i],columns['alpha'])]
            if doc['resampling'] == 0:
                objective_residual = residual
            elif doc['resampling'] == 1:
                objective_residual = doc['objective_residuals'][i]
                if len(objective_residual) != n: raise ValueError('Whitened residual shape mismatch: ' + doc['key'])
            else: raise ValueError('Unsupported native residual mode: ' + doc['key'])
            nuisance = doc['nuisance_residuals'][i]
            if len(nuisance) != len(doc['nuisance_order']): raise ValueError('Nuisance shape mismatch: ' + doc['key'])
            nr = doc['normalization_residuals'][i]
            if any(not math.isfinite(x) for x in [*residual,*objective_residual,*nuisance,nr]):
                raise ValueError('Nonfinite central residual/penalty: ' + doc['key'])
            terms = {'data': math.fsum(x*x for x in residual), 'objective_data': math.fsum(x*x for x in objective_residual),
                     'correlated': math.fsum(x*x for x in nuisance),
                     'normalization': nr*nr}
            per_dataset[doc['key']].append(terms)
            for name,value in terms.items(): individual[key][name] += value
        available.append(doc)
    npts = sum(doc['npts'] for doc in available)
    if records and not npts: raise ValueError('No accepted experimental points for central-data chi2.')
    for row in individual.values():
        row['chi2_reported'] = row['data']
        row['chi2_total'] = row['data']  # Legacy report alias; never includes penalties.
        row['objective_total'] = math.fsum(row[k] for k in ('objective_data', 'correlated', 'normalization'))
        row['npts'] = npts
        row['chi2_per_point'] = row['chi2_total']/npts if npts else None
    scored = [dict(r, central_chi2_reported=individual[r['identity']]['chi2_reported']) for r in records]
    kept, dropped = select_replicas(scored, selection)
    keep = [r['identity'] for r in kept]
    if records and not keep and not allow_empty: raise ValueError('The replica selection leaves no fitted replica.')
    positions = [identities.index(k) for k in keep]
    rows = []
    for doc in available:
        if not positions: continue
        n = doc['npts']; columns = doc['columns']
        if any(x is None or not math.isfinite(x) or x <= 0 for x in columns['alpha']):
            raise ValueError('Invalid central uncertainty: ' + doc['key'])
        prediction = [math.fsum(doc['theory'][i][j] for i in positions)/len(positions) for j in range(n)]
        residual = [(d-p)/a for d,p,a in zip(columns['value'], prediction, columns['alpha'])]
        if doc['resampling'] == 0:
            objective_residual = residual
        elif doc['resampling'] == 1:
            objective_residual = [math.fsum(doc['objective_residuals'][i][j] for i in positions)/len(positions) for j in range(n)]
        else: raise ValueError('Unsupported native residual mode: ' + doc['key'])
        nuisance = doc['nuisance_residuals']
        size = len(doc['nuisance_order'])
        if len(nuisance) != len(identities) or any(len(r) != size for r in nuisance):
            raise ValueError('Correlated nuisance identity/shape mismatch: ' + doc['key'])
        mean_r = [math.fsum(nuisance[i][j] for i in positions)/len(positions) for j in range(size)]
        nr = doc['normalization_residuals']
        if len(nr) != len(identities): raise ValueError('Normalization alignment mismatch: ' + doc['key'])
        terms = {'data': math.fsum(x*x for x in residual), 'objective_data': math.fsum(x*x for x in objective_residual),
                 'correlated': math.fsum(x*x for x in mean_r),
                 'normalization': (math.fsum(nr[i] for i in positions)/len(positions))**2}
        terms['chi2_reported'] = terms['total'] = terms['data']
        terms['objective_total'] = math.fsum(terms[k] for k in ('objective_data','correlated','normalization'))
        mean_individual = {name: mean([per_dataset[doc['key']][i][name] for i in positions])
                           for name in ('data','correlated','normalization')}
        totals = [per_dataset[doc['key']][i]['data'] for i in positions]
        rows.append({'dataset': doc['key'], 'npts': n, **terms, 'mean_individual': mean_individual,
                     'profiling_mean_equivalent': doc['resampling'] == 0 and doc['beta_version'] == 1,
                     'individual_total_min': min(totals), 'individual_total_max': max(totals),
                     'normalization_definition': doc['normalization'], 'mean_norm': mean([doc['stats']['norm'][i] for i in positions if doc['stats']['norm'][i] is not None])})
    ensemble = {name: math.fsum(row[name] for row in rows) for name in ('data','objective_data','correlated','normalization','total','objective_total')}
    ensemble.update(chi2_reported=ensemble['data'], chi2_basis=CHI2_BASIS)
    npts = sum(doc['npts'] for doc in available)
    ensemble.update(npts=npts, chi2_per_point=ensemble['total']/npts if npts else None,
                    prediction_convention='mean(theory/normalization + central_profiled_shift)',
                    penalty_convention='squares of mean signed nuisance and normalization residuals; fixed/link/covariance exclusions native',
                    reprofiling='individual shifts reprofiled against central data; mean predictions retained, no second fit')
    if not keep:
        ensemble.update(data=None, objective_data=None, correlated=None, normalization=None, total=None,
                        chi2_reported=None, objective_total=None, chi2_per_point=None,
                        npts=npts if records else None)
    chosen = [individual[k] for k in keep]
    return {'metrics_version': METRICS_VERSION, 'scoring_version': SCORING_VERSION, 'chi2_basis': CHI2_BASIS,
            'individual': list(individual.values()),
            'selected_individual': chosen, 'ensemble': ensemble, 'datasets': rows,
            'configured_datasets': len(docs), 'nonempty_datasets': len(available), 'zero_point_datasets': zero,
            'replica_selection': selection_report(selection, kept, len(records), dropped),
            'best_replica': min(chosen, key=lambda r:r['chi2_total']) if chosen else None,
            'quality_criterion': 'No automatic scientific goodness or convergence threshold; optional user chi2_max/sigma cuts only.'}


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
    for k in ('nuisance_residuals', 'normalization_residuals', 'objective_residuals'):
        if k in doc: out[k] = [doc[k][i] for i in idx]
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


def fill_experiments(info, known):
    """info with each empty experiment name (a data file without a 'col'
    column, such as idis 10005) taken from known ({reaction: {id: name}}, the
    gateway's experiment headings), so that experiment names still find it."""
    for item in info.values():
        if not item.get('experiment'):
            item['experiment'] = known.get(item['reaction'], {}).get(str(item['dataset_id']), '')
    return info


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


def table_row(doc, best=None, ensemble=None):
    """Dataset statistics for the selected, pinned ensemble (not mean chi2).

    Cached theory already includes normalization and central-profiled shifts.
    Reuse central_report's pointwise mean prediction and mean signed nuisance
    residuals. Native normalization residuals are affine, (N-1)/dN, or zero
    for fixed/linked/covariance exclusions: their squared mean is exactly the
    native penalty at mean N, not the mean of individual penalties. No second
    normalization, profiling, or scientific evaluation belongs here.
    """
    st, n = doc['stats'], doc['npts']
    replicas = doc.get('replicas') or []
    if ensemble is None:
        records = [{'identity': identity} for identity in replicas]
        report = central_report([doc], records)
        ensemble = next((r for r in report['datasets'] if r['dataset'] == doc['key']), None)
    if ensemble is None or ensemble['dataset'] != doc['key'] or ensemble['npts'] != n:
        raise ValueError('Ensemble dataset identity/point-count mismatch: ' + doc['key'])
    points = [math.fsum(((d-p)/a)**2 for d,p,a in zip(doc['columns']['value'],row,doc['columns']['alpha']))
              for row in doc['theory']]
    chi2 = ensemble['data']
    chi2_best = points[replicas.index(best)] if best in replicas else None
    norm = [v for v in st.get('norm') or [] if v is not None]
    total = [math.fsum(v) for v in zip(st['chi2'], st['chi2_cor'], st['chi2_norm'])]
    return {'dataset': doc['key'], 'reaction': doc['reaction'], 'dataset_id': doc['dataset_id'],
            'experiment': doc.get('experiment', ''), 'observable': doc.get('observable', ''),
            'target': doc.get('target', ''), 'npts': n, 'npts_raw': doc.get('npts_raw'),
            'ncor': doc.get('ncor', 0), 'chi2': chi2, 'chi2_red': chi2 / n if n else None,
            'z_score': z_score(chi2, n), 'chi2_cor': ensemble['correlated'], 'chi2_norm': ensemble['normalization'],
            'norm': mean(norm) if doc.get('norm_fitted') and norm else None,
            'norm_std': std(norm) if doc.get('norm_fitted') and norm else None,
            'chi2_best': chi2_best, 'chi2_point': chi2, 'chi2_total': chi2,
            'chi2_basis': CHI2_BASIS, 'chi2_total_red': chi2/n if n else None,
            'chi2_best_total': chi2_best, 'objective_mean': mean(total),
            'objective_best': total[replicas.index(best)] if best in replicas else None,
            'chi2_estimator': 'pointwise_mean_central_prediction',
            'penalty_estimator': 'squared_mean_native_residuals; normalization penalty at mean fitted norm',
            'mean_individual_chi2': mean(points), 'mean_individual_chi2_red': mean(points)/n if n else None,
            'mean_individual_chi2_cor': mean(st['chi2_cor']), 'mean_individual_chi2_norm': mean(st['chi2_norm']),
            'point_order_sha256': doc.get('point_order_sha256')}


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
        score = r['chi2_best']
        g['datasets'].append({'reaction': r['reaction'], 'dataset_id': r['dataset_id'], 'target': r['target'],
                              'observable': r['observable'], 'npts': r['npts'], 'chi2': score,
                              'chi2_per_npt': score / r['npts'] if r['npts'] else None,
                              'point_order_sha256': r.get('point_order_sha256')})
        g['chi2'] += score
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
