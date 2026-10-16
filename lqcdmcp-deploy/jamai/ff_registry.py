"""Fragmentation-function defaults transcribed from the supplied input.py excerpt.

Commented definitions remain available but are omitted unless explicitly enabled.
The values of linked parameters are preserved; FitPack applies their relationships.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math


FF_OBJECTS = ('ffpion', 'ffkaon', 'ffhadron')
FF_FLAVORS = ('g', 'u', 'd', 'ub', 'db', 's', 'sb', 'c', 'b')
FF_SETTINGS = {
    'ffpion': {'ffpion parametrization': 0},
    'ffkaon': {'ffkaon parametrization': 0},
    'ffhadron': {'ffhadron parametrization': 'sum2'},
}
FF_GAUSSIAN = {
    'N': {'mean': 0.02, 'std': 0.005},
    'a': {'mean': -0.5, 'std': 2},
    'b': {'mean': 3, 'std': 2},
    'c': {'mean': 0, 'std': 1.5},
    'd': {'mean': 0, 'std': 1.5},
}

# Every row is (N, a, b, c, d). These are source values, including intentional
# differences between a linked entry and its target. No effective value is inferred.
_VALUES = {
    'ffpion': {
        'g1': (0.295437, 1.00469, 6.85766, 4.81521, 4.81521),
        'u1': (0.0267821, 0.176877, 4.81521, 4.81521, 4.81521),
        'd1': (0.299974, -0.689477, 4.79992, 4.81521, 4.81521),
        's1': (0.154863, 0.300305, 1.83178, 4.81521, 4.81521),
        'c1': (0.184550, -0.0505798, 3.19952, 4.81521, 4.81521),
        'b1': (0.374125, -1.59541, 4.50102, 4.81521, 4.81521),
        'ub1': (0.299974, -0.689477, 4.79992, 4.81521, 4.81521),
        'db1': (0.0267821, 0.176877, 4.81521, 4.81521, 4.81521),
        'sb1': (0.154863, 0.300305, 1.83178, 4.81521, 4.81521),
        'cb1': (0.184550, -0.0505798, 3.19952, 4.81521, 4.81521),
        'bb1': (0.374125, -1.59541, 4.50102, 4.81521, 4.81521),
        'g2': (0.295437, 1.00469, 6.85766, 4.81521, 4.81521),
        'u2': (0.0, 0.176877, 4.81521, 4.81521, 4.81521),
        'd2': (0.0, 0.176877, 4.81521, 4.81521, 4.81521),
        's2': (0.154863, 0.300305, 1.83178, 4.81521, 4.81521),
        'c2': (0.184550, -0.0505798, 3.19952, 4.81521, 4.81521),
        'b2': (0.374125, -1.59541, 4.50102, 4.81521, 4.81521),
        'ub2': (0.0, 0.176877, 4.81521, 4.81521, 4.81521),
        'db2': (0.0, 0.176877, 4.81521, 4.81521, 4.81521),
        'sb2': (0.154863, 0.300305, 1.83178, 4.81521, 4.81521),
        'cb2': (0.184550, -0.0505798, 3.19952, 4.81521, 4.81521),
        'bb2': (0.374125, -1.59541, 4.50102, 4.81521, 4.81521),
    },
    'ffkaon': {
        'g1': (0.233320, 1.48737, 9.62755, 4.81521, 4.81521),
        'u1': (0.0403672, 1.26356, 1.62596, 4.81521, 4.81521),
        'd1': (0.251671, -1.43444, 5.65143, 4.81521, 4.81521),
        's1': (0.251671, -1.43444, 5.65143, 4.81521, 4.81521),
        'c1': (0.776923, -1.8, 2.50452, 4.81521, 4.81521),
        'b1': (0.566971, -1.8, 3.41727, 4.81521, 4.81521),
        'ub1': (0.0403672, 2.26356, 1.62596, 4.81521, 4.81521),
        'db1': (0.251671, -1.43444, 1.65143, 4.81521, 4.81521),
        'sb1': (0.251671, -1.43444, 5.65143, 4.81521, 4.81521),
        'cb1': (0.776923, -1.8, 2.50452, 4.81521, 4.81521),
        'bb1': (0.566971, -1.8, 3.41727, 4.81521, 4.81521),
        'g2': (0.233320, 1.48737, 9.62755, 4.81521, 4.81521),
        'u2': (0.0, 0.176877, 4.81521, 4.81521, 4.81521),
        'd2': (0.0, 0.176877, 4.81521, 4.81521, 4.81521),
        's2': (0.251671, -1.43444, 5.65143, 4.81521, 4.81521),
        'c2': (0.776923, -1.8, 2.50452, 4.81521, 4.81521),
        'b2': (0.566971, -1.8, 3.41727, 4.81521, 4.81521),
        'ub2': (0.0, 0.176877, 4.81521, 4.81521, 4.81521),
        'db2': (0.0, 0.176877, 4.81521, 4.81521, 4.81521),
        'sb2': (0.251671, -1.43444, 5.65143, 4.81521, 4.81521),
        'cb2': (0.776923, -1.8, 2.50452, 4.81521, 4.81521),
        'bb2': (0.566971, -1.8, 3.41727, 4.81521, 4.81521),
    },
    'ffhadron': {
        'g1': (0.233320, 1.48737, 9.62755, 4.81521, 4.81521),
        'u1': (0.0403672, 1.26356, 1.62596, 4.81521, 4.81521),
        'd1': (0.251671, -1.43444, 5.65143, 4.81521, 4.81521),
        's1': (0.251671, -1.43444, 5.65143, 4.81521, 4.81521),
        'c1': (0.776923, -1.8, 2.50452, 4.81521, 4.81521),
        'b1': (0.566971, -1.8, 3.41727, 4.81521, 4.81521),
        'ub1': (0.0403672, 2.26356, 1.62596, 4.81521, 4.81521),
        'db1': (0.251671, -1.43444, 1.65143, 4.81521, 4.81521),
        'sb1': (0.251671, -1.43444, 5.65143, 4.81521, 4.81521),
        'cb1': (0.776923, -1.8, 2.50452, 4.81521, 4.81521),
        'bb1': (0.566971, -1.8, 3.41727, 4.81521, 4.81521),
        'g2': (0.233320, 1.48737, 9.62755, 4.81521, 4.81521),
        'u2': (0.0, 0.176877, 4.81521, 4.81521, 4.81521),
        'd2': (0.0, 0.176877, 4.81521, 4.81521, 4.81521),
        's2': (0.251671, -1.43444, 5.65143, 4.81521, 4.81521),
        'c2': (0.776923, -1.8, 2.50452, 4.81521, 4.81521),
        'b2': (0.566971, -1.8, 3.41727, 4.81521, 4.81521),
        'ub2': (0.0, 0.176877, 4.81521, 4.81521, 4.81521),
        'db2': (0.0, 0.176877, 4.81521, 4.81521, 4.81521),
        'sb2': (0.251671, -1.43444, 1.65143, 4.81521, 4.81521),
        'cb2': (0.776923, -1.8, 2.50452, 4.81521, 4.81521),
        'bb2': (0.566971, -1.8, 3.41727, 4.81521, 4.81521),
    },
}
_LINKS = {
    'ffpion': {'ub': 'd', 'db': 'u', 'sb': 's', 'cb': 'c', 'bb': 'b'},
    'ffkaon': {'ub': 'd', 'db': 'd', 'cb': 'c', 'bb': 'b'},
    'ffhadron': {'db': 'ub', 'sb': 's', 'cb': 'c', 'bb': 'b'},
}
_BOUNDS = {'N': (0.0, 1), 'a': (-1.8, 10), 'b': (0, 10),
           'c': (-10, 10), 'd': (-10, 10)}


def _enabled_default(key, block, parameter):
    if key != 'ffhadron':
        return block.endswith('1') or block in ('b2', 'bb2')
    if block.endswith('1'):
        return parameter in ('N', 'a', 'b') or block in ('g1', 'c1', 'b1', 'cb1', 'bb1')
    return block in ('b2', 'bb2') and parameter in ('N', 'a', 'b')


FF_PARAMETERS = {}
FF_OPTIONAL = {}
for _key in FF_OBJECTS:
    FF_PARAMETERS[_key], FF_OPTIONAL[_key] = {}, set()
    for _block, _values in _VALUES[_key].items():
        for _parameter, _value in zip(('N', 'a', 'b', 'c', 'd'), _values):
            _name = f'{_block} {_parameter}'
            _minimum, _maximum = _BOUNDS[_parameter]
            if (_key, _block, _parameter) == ('ffpion', 'c2', 'b'):
                _maximum = 30
            _target = _LINKS[_key].get(_block[:-1])
            FF_PARAMETERS[_key][_name] = {
                'gaussian': True, **FF_GAUSSIAN[_parameter], 'value': _value,
                'min': _minimum, 'max': _maximum,
                'fixed': f'{_target}{_block[-1]} {_parameter}' if _target else False,
            }
            if not _enabled_default(_key, _block, _parameter):
                FF_OPTIONAL[_key].add(_name)


def ff_library(key):
    """Return UI rows for active and optional definitions plus raw FitPack metadata."""
    if key not in FF_OBJECTS:
        raise ValueError(f'Unknown fragmentation function {key!r}')
    rows = []
    for name, entry in FF_PARAMETERS[key].items():
        block, parameter = name.split(' ')
        enabled = name not in FF_OPTIONAL[key]
        fixed = entry['fixed']
        row = {
            **copy.deepcopy(entry), 'name': name, 'block': block,
            'parameter': parameter, 'fixed': bool(fixed),
            'enabled': enabled, 'default_enabled': enabled, 'optional': not enabled,
        }
        if isinstance(fixed, str):
            row['tied_to'] = fixed
        if block[:-1] in ('cb', 'bb'):
            row['relationship_locked'] = True
            row['locked_to'] = f'{block[:-1][0]}{block[-1]} {parameter}'
        rows.append(row)
    params = {name: copy.deepcopy(entry) for name, entry in FF_PARAMETERS[key].items()
              if name not in FF_OPTIONAL[key]}
    canonical = json.dumps({'parameters': FF_PARAMETERS[key], 'optional': sorted(FF_OPTIONAL[key]),
                            'settings': FF_SETTINGS[key]}, sort_keys=True).encode()
    return {
        'rows': rows, 'params': params,
        'optional_params': {name: copy.deepcopy(entry) for name, entry in FF_PARAMETERS[key].items()
                            if name in FF_OPTIONAL[key]},
        'all_params': copy.deepcopy(FF_PARAMETERS[key]),
        'settings': copy.deepcopy(FF_SETTINGS[key]),
        'parametrization': FF_SETTINGS[key][f'{key} parametrization'],
        'fit_basis_blocks': list(_VALUES[key]), 'auxiliary_blocks': [],
        'free': sum(entry['fixed'] is False for entry in params.values()),
        'fixed': sum(entry['fixed'] is not False for entry in params.values()),
        'active_count': len(params), 'optional_count': len(FF_OPTIONAL[key]),
        'source': 'jamai/ff_registry.py (authoritative supplied FF input.py excerpt)',
        'sha256': hashlib.sha256(canonical).hexdigest(), 'datasets': {},
        'note': 'Authoritative FF defaults; optional commented definitions are omitted until enabled.',
    }


def resolve_ff_parameters(key, overrides=None):
    """Merge parameter edits, validate enabled links, and order targets before dependents.

    Accept either a name-to-entry dictionary or UI rows. Missing rows keep their
    authoritative defaults; enabled=False removes a row entirely. Gaussian fields
    are carried through even when the UI edits only value, bounds, and relationship.
    """
    if key not in FF_OBJECTS:
        raise ValueError(f'Unknown fragmentation function {key!r}')
    if overrides is None:
        overrides = {}
    if isinstance(overrides, list):
        names = [row.get('name') for row in overrides if isinstance(row, dict)]
        if len(names) != len(overrides) or len(set(names)) != len(names):
            raise ValueError(f'{key}: parameter rows must have unique names')
        overrides = {row['name']: row for row in overrides}
    if not isinstance(overrides, dict):
        raise ValueError(f'{key}: parameter overrides must be a dictionary or row list')
    unknown = set(overrides) - set(FF_PARAMETERS[key])
    if unknown:
        raise ValueError(f'{key}: unknown parameters {sorted(unknown)}')
    params = {}
    for name, default in FF_PARAMETERS[key].items():
        edit = overrides.get(name, {})
        if not isinstance(edit, dict):
            raise ValueError(f'{key} {name}: parameter edit must be a dictionary')
        enabled = edit.get('enabled', name not in FF_OPTIONAL[key])
        if type(enabled) is not bool:
            raise ValueError(f'{key} {name}: enabled must be a boolean')
        if not enabled:
            continue
        entry = copy.deepcopy(default)
        for field in ('value', 'min', 'max', 'fixed', 'gaussian', 'mean', 'std'):
            if field in edit:
                entry[field] = edit[field]
        if 'tied_to' in edit and edit.get('fixed', True) is not False:
            entry['fixed'] = edit['tied_to']
        for field in ('value', 'min', 'max', 'mean', 'std'):
            if type(entry[field]) not in (int, float) or not math.isfinite(entry[field]):
                raise ValueError(f'{key} {name}: {field} must be finite')
        if entry['min'] > entry['max'] or not entry['min'] <= entry['value'] <= entry['max']:
            raise ValueError(f'{key} {name}: value must lie within ordered bounds')
        if type(entry['gaussian']) is not bool or entry['std'] <= 0:
            raise ValueError(f'{key} {name}: invalid Gaussian metadata')
        if not (type(entry['fixed']) is bool or isinstance(entry['fixed'], str)):
            raise ValueError(f'{key} {name}: fixed must be boolean or a parameter-name string')
        if entry['fixed'] is False and entry['min'] >= entry['max']:
            raise ValueError(f'{key} {name}: free parameters require distinct lower and upper bounds')
        block, parameter = name.split(' ')
        if block[:-1] in ('cb', 'bb'):
            required = f'{block[:-1][0]}{block[-1]} {parameter}'
            if entry['fixed'] != required:
                raise ValueError(f'{key} {name}: heavy-antiquark relationship must remain linked to {required}')
        params[name] = entry
    if not params:
        raise ValueError(f'{key}: at least one parameter definition must remain enabled')
    for flavor in ('c', 'b'):
        for shape in ('1', '2'):
            for parameter in ('N', 'a', 'b', 'c', 'd'):
                quark = f'{flavor}{shape} {parameter}'
                antiquark = f'{flavor}b{shape} {parameter}'
                if (quark in params) != (antiquark in params):
                    raise ValueError(f'{key}: enable or disable {quark} and {antiquark} together; '
                                     'heavy quark/antiquark FFs are constrained equal')
    ordered, visiting = {}, set()

    def visit(name):
        if name in ordered:
            return
        if name in visiting:
            raise ValueError(f'{key}: circular parameter link involving {name}')
        visiting.add(name)
        target = params[name]['fixed']
        if isinstance(target, str):
            if target not in params:
                raise ValueError(f'{key} {name}: linked parameter {target!r} is missing or disabled')
            visit(target)
        visiting.remove(name)
        ordered[name] = params[name]

    for name in params:
        visit(name)
    return ordered


def normalize_ff_parameters(key, rows):
    """Gateway validation adapter: return normalized UI rows, change flag, errors."""
    try:
        params = resolve_ff_parameters(key, rows)
        normalized = []
        for name, entry in params.items():
            row = {'name': name, **copy.deepcopy(entry), 'enabled': True}
            if isinstance(entry['fixed'], str):
                row['tied_to'] = entry['fixed']
            normalized.append(row)
        return normalized, params != resolve_ff_parameters(key), []
    except (ValueError, TypeError) as exc:
        return [], False, [str(exc)]
