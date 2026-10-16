"""Application metadata from the proton library and authoritative pion reference."""

from __future__ import annotations

import ast
import copy
import hashlib
import io
import math
import runpy
from pathlib import Path


PION_REFERENCE = '/w/ceph24/JAM/cyang/all_2shapes_H1ext/pion_PDFs_cov_exp/input.npy'
DIS_COMPATIBILITY = {
    'temporary': True,
    'rule': 'Fitted pdf-pion selects DIS version 0, including combined campaigns; otherwise 1.',
    'reason': 'Current pion/LeadingBaryon machinery requires the version-0 IDIS API.',
    'removal': 'Remove this policy when pion PDFs support DIS version 1.',
}


def dis_version_for(fitted_objects):
    return 0 if 'pdf-pion' in fitted_objects else 1


OBJECTS = {
    'pdf': {
        'label': 'Unpolarized PDF', 'template': 'examples/input02.py',
        'reactions': ['idis', 'dy', 'wzrv', 'wasym', 'zrap', 'jet', 'wc'], 'settings': [],
        'note': 'Proton PDF basis and sum rules from the workspace library.',
        'flavors': ['u', 'd', 'g', 's', 'ub', 'db', 'sb'],
    },
    'pdf-pion': {
        'label': 'Pion PDF',
        'reactions': ['dy-pion', 'ln'],
        'settings': ['pdf-pion valence parametrization', 'pion resummation', 'MP method', 'Cq',
                     'pion fitting scenario', 'notebook display'],
        'note': 'Native pi- convention. Valence = ub - u, sea = u, gluon = g. '
                'One shape only, using the final-fit reference values, bounds and ties. '
                'Temporary compatibility policy: fitted Pion PDF selects DIS version 0.',
        'flavors': ['valence', 'sea', 'g', 'u', 'd', 's', 'ub', 'db', 'sb'],
    },
    'p->pi,n': {
        'label': 'Leading-neutron companion',
        'reactions': ['ln'], 'settings': ['ln mode', 'SPLFUNC model', 'ln parametrization'],
        'note': 'Fitted companion required by leading-neutron data; no extra charge factor.',
        'flavors': [],
    },
}
SELECTABLE_OBJECTS = ('pdf', 'pdf-pion')


# Canonical dataset metadata from the supplied input.py (SHA256
# 218a8e633bf015899f9f7b68494a8a44d79b1998fb506ea99d2424c85c8f75a6).
# Jet intentionally keeps only its pT cut; no x expression is synthesized.
PROTON_REACTION_DATASETS = {
    'wzrv': {
        'filters': [],
        'xlsx': {
            2010: 'wzrv/expdata/2010.xlsx',
            2011: 'wzrv/expdata/2011.xlsx',
            2012: 'wzrv/expdata/2012.xlsx',
            2013: 'wzrv/expdata/2013.xlsx',
            2014: 'wzrv/expdata/2014.xlsx',
            2016: 'wzrv/expdata/2016.xlsx',
            2017: 'wzrv/expdata/2017.xlsx',
            2020: 'wzrv/expdata/2020.xlsx',
        },
        'norm': {},
    },
    'wasym': {
        'filters': [],
        'xlsx': {
            1000: 'wasym/expdata/1000.xlsx',
            1001: 'wasym/expdata/1001.xlsx',
        },
        'norm': {},
    },
    'zrap': {
        'filters': [],
        'xlsx': {
            1000: 'zrap/expdata/1000.xlsx',
            1001: 'zrap/expdata/1001.xlsx',
        },
        'norm': {
            1000: {'value': 1.0, 'fixed': False, 'min': 0.5, 'max': 1.5},
        },
    },
    'jet': {
        'filters': ['pT>8.000000'],
        'xlsx': {
            10001: 'jet/expdata/10001.xlsx',
            10002: 'jet/expdata/10002.xlsx',
            10003: 'jet/expdata/10003.xlsx',
            10004: 'jet/expdata/10004.xlsx',
        },
        'norm': {
            10001: {'value': 1.0, 'fixed': False, 'min': 0.5, 'max': 1.5},
            10002: {'value': 1.0, 'fixed': False, 'min': 0.5, 'max': 1.5},
            10003: {'value': 1.0, 'fixed': False, 'min': 0.5, 'max': 1.5},
            10004: {'value': 1.0, 'fixed': False, 'min': 0.5, 'max': 1.5},
        },
    },
    'wc': {
        'xlsx': {
            10001: 'wc/expdata/10001.xlsx',
            10002: 'wc/expdata/10002.xlsx',
            20001: 'wc/expdata/20001.xlsx',
            20002: 'wc/expdata/20002.xlsx',
            20003: 'wc/expdata/20003.xlsx',
        },
        'norm': {
            10001: {'value': 1.0, 'fixed': False, 'min': 0.8, 'max': 1.2},
            10002: {'value': 1.0, 'fixed': False, 'min': 0.8, 'max': 1.2},
            20003: {'value': 1.0, 'fixed': False, 'min': 0.8, 'max': 1.2},
        },
    },
}


def _cut_number(node):
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return (-1 if isinstance(node.op, ast.USub) else 1) * _cut_number(node.operand)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
        return _cut_number(node.left) ** _cut_number(node.right)
    raise ValueError('Library cut threshold is not a numeric constant')


def library_cuts(filters):
    if isinstance(filters, dict):
        return {str(key): library_cuts(value) for key, value in filters.items()}
    cuts = []
    ops = {ast.Gt: '>', ast.GtE: '>=', ast.Lt: '<', ast.LtE: '<='}
    for expression in filters:
        node = ast.parse(expression, mode='eval').body
        if (not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name)
                or len(node.ops) != 1 or type(node.ops[0]) not in ops):
            raise ValueError(f'Unsupported library cut: {expression}')
        value = float(_cut_number(node.comparators[0]))
        if not math.isfinite(value):
            raise ValueError(f'Nonfinite library cut: {expression}')
        cuts.append({'variable': node.left.id, 'op': ops[type(node.ops[0])],
                     'value': value, 'enabled': True})
    return cuts


def cut_filters(cuts):
    """Render global or per-dataset cuts without collapsing their scope."""
    if isinstance(cuts, dict):
        return {str(key): cut_filters(value) for key, value in cuts.items()}
    filters = []
    for cut in cuts:
        if not cut.get('enabled', True):
            continue
        variable, op, value = str(cut['variable']), cut['op'], float(cut['value'])
        if not variable.isidentifier() or op not in ('>', '>=', '<', '<=') or not math.isfinite(value):
            raise ValueError(f'Invalid cut: {cut!r}')
        filters.append(f'{variable}{op}{value:g}')
    return filters


def one_shape_pion_params(params):
    kept = {name: copy.deepcopy(entry) for name, entry in params.items()
            if not name.partition(' ')[0].endswith('2')}
    for name, entry in kept.items():
        tied = entry.get('fixed')
        if isinstance(tied, str) and tied not in kept:
            raise ValueError(f'Pion parameter {name} ties to excluded or unknown parameter {tied}')
    return kept


def _read_pion_reference(path):
    # Pickled NumPy configuration is trusted scientific input, never a user-uploaded card.
    import numpy as np
    raw = path.read_bytes()
    value = np.load(io.BytesIO(raw), allow_pickle=True)
    if not isinstance(value, np.ndarray) or value.shape != () or not isinstance(value.item(), dict):
        raise ValueError(f'{path} must contain one configuration dictionary')
    return value.item(), hashlib.sha256(raw).hexdigest()


def load_object_libraries(root, keys, pion_reference=None):
    """Read trusted FitPack input cards, without importing/initializing physics."""
    configs, libraries = {}, {}
    for key in keys:
        meta = OBJECTS[key]
        path = (Path(root) / meta['template'] if key == 'pdf'
                else Path(pion_reference or PION_REFERENCE))
        if path not in configs:
            configs[path] = ((runpy.run_path(str(path))['conf'], hashlib.sha256(path.read_bytes()).hexdigest())
                             if key == 'pdf' else _read_pion_reference(path))
        config, fingerprint = configs[path]
        params = config.get('params', {}).get(key)
        if not params:
            raise ValueError(f'{path} has no parameters for {key}')
        if key == 'pdf-pion':
            params = one_shape_pion_params(params)
        rows = []
        for name, entry in params.items():
            fixed = entry.get('fixed', False)
            block, _, parameter = name.partition(' ')
            row = {'name': name, 'block': block, 'parameter': parameter or name,
                   'value': float(entry['value']), 'min': entry.get('min'), 'max': entry.get('max'),
                   'fixed': bool(fixed)}
            if isinstance(fixed, str):
                row['tied_to'] = fixed
            rows.append(row)
        datasets = {r: copy.deepcopy(config['datasets'][r]) for r in meta['reactions']
                    if r in config.get('datasets', {})}
        if key == 'pdf':
            datasets.update(copy.deepcopy(PROTON_REACTION_DATASETS))
        libraries[key] = {
            'rows': rows, 'source': str(path),
            'sha256': fingerprint,
            'fit_basis_blocks': list(dict.fromkeys(r['block'] for r in rows)),
            'auxiliary_blocks': [],
            'parametrization': config.get('pdf-pion valence parametrization', '') if key == 'pdf-pion'
                               else config.get(f'{key} parametrization', ''),
            'note': meta['note'], 'free': sum(not r['fixed'] for r in rows),
            'fixed': sum(r['fixed'] for r in rows),
            'params': copy.deepcopy(params), 'datasets': datasets,
            'settings': {s: copy.deepcopy(config[s]) for s in meta['settings'] if s in config},
        }
        if key == 'pdf-pion':
            libraries[key].update(
                shape_count=1, compatibility=DIS_COMPATIBILITY,
                fixed_companions={'pdf': copy.deepcopy(config['params']['pdf'])},
                defaults={
                    'qcd': {'order': config['order'], 'Q20': config['Q20'],
                            'dglapMode': config['dglap mode'], 'asMode': config['alphaSmode']},
                    'job': {'tolerance': str(config['ftol']), 'bootstrap': config['bootstrap']},
                })
    return libraries


def distribution_value(distribution, x, q2, flavor, object_key):
    if object_key == 'pdf-pion' and flavor == 'valence':
        return distribution.get_xF(x, q2, 'ub') - distribution.get_xF(x, q2, 'u')
    if object_key == 'pdf-pion' and flavor == 'sea':
        flavor = 'u'
    return distribution.get_xF(x, q2, flavor)


def validate_credible_interval(credible_interval=95):
    """Normalize a percentage without accepting booleans or nonfinite values."""
    message = 'Credible interval (%) must be greater than 0 and at most 100.'
    if isinstance(credible_interval, bool) or not isinstance(credible_interval, (int, float, str)):
        raise ValueError(message)
    try:
        value = float(credible_interval)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(message) from exc
    if not math.isfinite(value) or not 0 < value <= 100:
        raise ValueError(message)
    return value


def pointwise_credible_interval(replica_curves, credible_interval=95):
    """Equal-tailed bounds across replicas of the already-transformed plotted quantity."""
    import numpy as np
    value = validate_credible_interval(credible_interval)
    curves = np.asarray(replica_curves, dtype=float)
    if curves.ndim != 2 or curves.shape[1] == 0:
        raise ValueError('Replica curves must have shape (n_replicas, n_x) with at least one x.')
    if curves.shape[0] < 2:
        raise ValueError('At least two valid fitted replicas are required for a credible interval.')
    if not np.isfinite(curves).all():
        raise ValueError('Replica curves contain nonfinite values; no credible interval was calculated.')
    lower = (100 - value) / 2
    return np.percentile(curves, [lower, 100 - lower], axis=0)
