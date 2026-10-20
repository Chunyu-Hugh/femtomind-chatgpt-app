#!/usr/bin/env python3
"""Archive the existing JAM24 arrays and statistics; never evaluates theory or fits.

Run in the existing scientific environment. All writes are confined to a new,
ignored directory under the application checkout. Source arrays are trusted
original NumPy pickle bundles; output numeric NPZ files need no pickle support.
"""
import argparse
import ast
from collections import Counter
import contextlib
import copy
import gzip
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import warnings

import numpy as np
from scipy import stats


SOURCE_FILES = ('replicas_cut.npy', 'thy_cut.npy', 'all_exp.npy', 'avg_thy.npy')


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def json_value(value):
    if isinstance(value, np.generic):
        return json_value(value.item())
    if isinstance(value, np.ndarray):
        return json_value(value.tolist())
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, float) and not np.isfinite(value):
        raise ValueError('Nonfinite scalar requires an explicit missing-value reason')
    return value


def write_json(path, value):
    Path(path).write_text(json.dumps(json_value(value), indent=2, ensure_ascii=True, allow_nan=False) + '\n', encoding='utf-8')


def original_zscore(source):
    """Load only the original four-line function, never execute notebook cells."""
    path = source / 'chi2_zscore_plot_paper.ipynb'
    notebook = json.loads(path.read_bytes())
    found = []
    for cell_index, cell in enumerate(notebook['cells']):
        if cell.get('cell_type') != 'code':
            continue
        text = ''.join(cell.get('source', []))
        if 'def get_zscore(' not in text:
            continue
        tree = ast.parse(text)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == 'get_zscore':
                function_text = ast.get_source_segment(text, node)
                scope = {'stats': stats}
                exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), scope)
                found.append((scope['get_zscore'], {'path': str(path), 'sha256': sha(path),
                    'cell_index_zero_based': cell_index, 'function': function_text,
                    'function_sha256': hashlib.sha256(function_text.encode()).hexdigest()}))
    if len(found) != 1:
        raise ValueError('Original Z-score function is missing or ambiguous')
    return found[0]


def normalization_values(replicas, reaction, dataset_id):
    """Resolve a same-reaction link to its independent column; never resample."""
    definitions = replicas['conf']['datasets'][reaction].get('norm', {})
    if dataset_id not in definitions:
        return None, None, 'absent'
    current, visited = dataset_id, set()
    while True:
        if current in visited:
            raise ValueError('Normalization link cycle')
        visited.add(current)
        definition = definitions[current]
        fixed = definition['fixed']
        if fixed is False:
            column = replicas['order'].index([2, reaction, current])
            return replicas['params'][:, column], current, 'independent' if current == dataset_id else 'linked'
        if fixed is True:
            return np.full(len(replicas['params']), definition['value']), current, 'fixed'
        if not isinstance(fixed, (int, np.integer)) or int(fixed) not in definitions:
            raise ValueError('Unsupported original normalization link')
        current = int(fixed)


def dataset_statistics(predictions, experimental, zscore, norm_values=None, dN=None,
                       norm_status='absent'):
    nrep, npoints = predictions['thy'].shape
    if not nrep or not npoints:
        raise ValueError('Cannot make a fit row from an empty ensemble or dataset')
    for name, array in predictions.items():
        if array.ndim != 2 or array.shape[0] != nrep or not np.all(np.isfinite(array)):
            raise ValueError('Invalid original replica array: ' + name)
    value, alpha = experimental['value'], experimental['alpha']
    if value.shape != (npoints,) or alpha.shape != (npoints,) or not np.all(alpha > 0):
        raise ValueError('Experimental points are not aligned')
    if not np.all(np.isfinite(value)) or not np.all(np.isfinite(alpha)):
        raise ValueError('Nonfinite experimental values or uncertainties')
    mean = {name: np.mean(array, axis=0) for name, array in predictions.items()}
    point_residual = (mean['thy'] - value) / alpha
    point_chi2 = float(np.sum(point_residual ** 2))
    cor_sum = float(np.sum(mean['rres'] ** 2))
    ncor = predictions['rres'].shape[1]
    norm = {'status': norm_status, 'value': None, 'uncertainty': None, 'uncertainty_definition': None,
            'experimental_relative_uncertainty': dN, 'chi2': None, 'missing_reason': None,
            'objective_penalty_counted': norm_status == 'independent'}
    if norm_values is None:
        norm['missing_reason'] = 'No fitted dataset normalization parameter in the original configuration.'
    else:
        if norm_values.shape != (nrep,) or not np.all(np.isfinite(norm_values)):
            raise ValueError('Normalization replicas are not aligned')
        norm['value'] = float(np.mean(norm_values))
        if norm_status != 'fixed':
            norm['uncertainty'] = float(np.std(norm_values, ddof=0))
            norm['uncertainty_definition'] = 'Replica population standard deviation (ddof=0), as in the original table notebook; not the prior width.'
        if dN is None or dN <= 0:
            norm['missing_reason'] = 'Experimental normalization penalty width is unavailable.'
        else:
            norm['chi2'] = float(((norm['value'] - 1) / dN) ** 2)
    norm_contribution = norm['chi2'] if norm['objective_penalty_counted'] else 0.0
    z = float(zscore(point_chi2, npoints))
    result = {'accepted_points': npoints, 'n_cor': ncor, 'chi2_data': point_chi2,
        'chi2_red': point_chi2 / npoints, 'z_score': z if np.isfinite(z) else None,
        'z_score_missing_reason': None if np.isfinite(z) else 'Original CDF/PPF convention reached a floating-point tail limit.',
        'chi2_cor': cor_sum if ncor else None,
        'chi2_cor_missing_reason': None if ncor else 'No explicit correlated-systematic nuisance residuals.',
        'normalization': norm,
        'chi2_objective_components': {'data': point_chi2, 'correlated': cor_sum, 'normalization': norm_contribution},
        'chi2_objective_like_at_ensemble_means': None if norm_contribution is None else point_chi2 + cor_sum + norm_contribution,
        'mean_replica_data_chi2_diagnostic': float(np.mean(np.sum(predictions['res'] ** 2, axis=1)))}
    return result, mean, point_residual


def aggregate(rows, zscore):
    npoints = sum(row['accepted_points'] for row in rows)
    chi2 = sum(row['chi2_data'] for row in rows)
    components = {}
    for kind in ('data', 'correlated', 'normalization'):
        parts = [row['chi2_objective_components'][kind] for row in rows]
        components[kind] = None if any(v is None for v in parts) else float(sum(parts))
    z = float(zscore(chi2, npoints))
    return {'datasets': len(rows), 'accepted_points': npoints,
        'original_points': sum(row['original_points'] for row in rows),
        'n_cor': sum(row['n_cor'] for row in rows), 'chi2_data': chi2,
        'chi2_red': chi2 / npoints, 'z_score': z if np.isfinite(z) else None,
        'chi2_cor': components['correlated'], 'chi2_norm_unique': components['normalization'],
        'chi2_norm_display_row_sum': sum(row['normalization']['chi2'] or 0.0 for row in rows),
        'chi2_objective_components': components,
        'chi2_objective_like_at_ensemble_means': None if any(v is None for v in components.values()) else sum(components.values())}


def comparison(calculated, reference):
    a, b = np.asarray(calculated), np.asarray(reference)
    if a.shape != b.shape:
        return {'exact': False, 'shape_mismatch': [list(a.shape), list(b.shape)]}
    difference = np.abs(a - b)
    return {'exact': bool(np.array_equal(a, b)), 'max_absolute_difference': float(np.max(difference)) if a.size else 0.0}


def appendix_comparison(row, mapping):
    """Compare to existing catalog print precision; references never feed statistics."""
    result = {'publication_row': mapping['publication_row'], 'source_url': mapping['source_url'], 'checks': {}}
    fields = {'chi2_red': ('chi2red_z_as_printed', 0), 'z_score': ('chi2red_z_as_printed', 1),
              'chi2_cor': ('chi2_cor_as_printed', 0), 'chi2_norm': ('chi2_norm_as_printed', 0)}
    for metric, (key, position) in fields.items():
        printed = mapping.get(key, '')
        numbers = re.findall(r'[+-]?\d+(?:\.\d+)?', printed)
        actual = row['normalization']['chi2'] if metric == 'chi2_norm' else row[metric]
        if len(numbers) <= position or actual is None:
            result['checks'][metric] = {'status': 'not_comparable', 'printed': printed}
        else:
            target = numbers[position]
            decimals = len(target.split('.')[1]) if '.' in target else 0
            rounded = f'{actual:.{decimals}f}'
            matches = float(rounded) == float(target)
            result['checks'][metric] = {'status': 'match' if matches else 'disagreement',
                'calculated': actual, 'rounded': rounded, 'printed': target}
    text = mapping.get('fitted_norm_as_printed', '')
    match = re.search(r'(\d+\.\d+)\((\d+)\)', text)
    if match and row['normalization']['value'] is not None:
        decimals = len(match[1].split('.')[1])
        expected_sd = float(f'{int(match[2]) / 10 ** decimals:.{decimals}f}')
        value, sd = row['normalization']['value'], row['normalization']['uncertainty']
        result['checks']['fitted_normalization'] = {'status': 'match' if float(f'{value:.{decimals}f}') == float(match[1]) else 'disagreement',
            'calculated': value, 'printed': match[1]}
        result['checks']['normalization_uncertainty'] = {'status': 'match' if sd is not None and float(f'{sd:.{decimals}f}') == expected_sd else 'disagreement',
            'calculated': sd, 'printed': expected_sd, 'definition': 'Posterior replica standard deviation, not prior width.'}
    printed_cor = mapping.get('n_cor_as_printed', '')
    if printed_cor.isdigit():
        result['checks']['n_cor'] = {'status': 'match' if int(printed_cor) == row['n_cor'] else 'disagreement', 'printed': int(printed_cor), 'calculated': row['n_cor']}
    return result


def native_experiments(replicas, theory, experimental, fitpack):
    import pandas as pd
    sys.path.insert(0, str(fitpack))
    os.environ['FITPACK'] = str(fitpack)
    from tools.config import conf
    from tools.residuals import _RESIDUALS
    from qcdlib.aux import AUX
    conf.clear()
    conf.update(copy.deepcopy(replicas['conf']))
    conf['bootstrap'] = False
    conf['aux'] = AUX()
    original_read = pd.read_excel
    books = {}
    def tagged_read(*args, **kwargs):
        frame = original_read(*args, **kwargs)
        books[str(Path(args[0]).resolve())] = {'original_points': len(frame), 'sha256': sha(args[0])}
        frame['archive_source_row'] = np.arange(len(frame))
        return frame
    pd.read_excel = tagged_read
    output, logs = {}, io.StringIO()
    try:
        with contextlib.redirect_stdout(logs), warnings.catch_warnings(record=True) as reader_warnings:
            warnings.simplefilter('always')
            for reaction, datasets in theory.items():
                reader = importlib.import_module('obslib.' + reaction + '.reader').READER()
                tabs = reader.load_data_sets(reaction, verb=False)
                residual = object.__new__(_RESIDUALS)
                residual.tabs, residual.reaction = tabs, reaction
                residual.percent_to_absolute()
                residual.get_alpha()
                residual.retrieve_norm_uncertainty()
                for tab in tabs.values():
                    if 'resampling' not in tab:
                        tab['resampling'] = np.full(len(tab['value']), conf.get('resample version', 0))
                residual.setup_covmat()
                for dataset_id, predictions in datasets.items():
                    tab = tabs[dataset_id]
                    for field in ('value', 'alpha'):
                        if not np.array_equal(tab[field], experimental[reaction][dataset_id][field]):
                            raise ValueError(f'Current native experimental {field} differs from original archive: {reaction}:{dataset_id}')
                    corr = [name for name in tab if '_c' in name and '%' not in name and 'norm' not in name]
                    if len(corr) != predictions['rres'].shape[1]:
                        raise ValueError(f'Native nuisance count differs from original rres: {reaction}:{dataset_id}')
                    definition = conf['datasets'][reaction].get('norm', {}).get(dataset_id)
                    output[(reaction, dataset_id)] = {'tab': tab, 'correlated_names': corr,
                        'dN': None if definition is None else definition.get('dN'),
                        'xlsx_path': conf['datasets'][reaction]['xlsx'][dataset_id]}
    finally:
        pd.read_excel = original_read
    notes = Counter((type(w.message).__name__, next((line.strip() for line in str(w.message).splitlines() if line.strip()), '')) for w in reader_warnings)
    return output, books, [{'type': kind, 'message': message, 'count': count} for (kind, message), count in notes.items()]


def save_numeric(path, arrays):
    cleaned = {}
    json_columns = []
    for name, value in arrays.items():
        if name == '__archive_json_columns__':
            raise ValueError('Reserved archive metadata member')
        value = np.asarray(value)
        if value.dtype == object:
            if all(isinstance(item, str) for item in value.flat):
                value = value.astype(str)
            else:
                # Native SIA untagged data use None. Preserve these metadata
                # cells, not the string "None", without pickling Python objects.
                cells = []
                for item in value.flat:
                    if isinstance(item, np.generic):
                        item = item.item()
                    if item is not None and not isinstance(item, (str, bool, int, float)):
                        raise ValueError('Unsupported object metadata cell: ' + name)
                    if isinstance(item, float) and not np.isfinite(item):
                        item = {'nonfinite_float': repr(item)}
                    cells.append(json.dumps(item, ensure_ascii=True, allow_nan=False))
                value = np.asarray(cells, dtype=str).reshape(value.shape)
                json_columns.append(name)
        cleaned[name] = value
    if json_columns:
        cleaned['__archive_json_columns__'] = np.asarray(json.dumps(json_columns))
    np.savez_compressed(path, **cleaned)
    with np.load(path, allow_pickle=False) as saved:
        if set(saved.files) != set(cleaned):
            raise ValueError('Archive member list changed')
        for key, value in cleaned.items():
            if saved[key].dtype.kind in 'fc':
                equal = np.array_equal(saved[key], value, equal_nan=True)
            else:
                equal = np.array_equal(saved[key], value)
            if not equal:
                raise ValueError('Archive round-trip changed: ' + key)


def load_numeric(path):
    """Read pickle-free arrays, restoring nullable SIA tag metadata explicitly."""
    with np.load(path, allow_pickle=False) as stored:
        arrays = {key: stored[key] for key in stored.files if key != '__archive_json_columns__'}
        columns = json.loads(str(stored['__archive_json_columns__'])) if '__archive_json_columns__' in stored else []
    for name in columns:
        source = arrays[name]
        result = np.empty(source.shape, dtype=object)
        for index in np.ndindex(source.shape):
            value = json.loads(str(source[index]))
            if isinstance(value, dict) and set(value) == {'nonfinite_float'}:
                value = float(value['nonfinite_float'])
            result[index] = value
        arrays[name] = result
    return arrays


def build(source, fitpack, app):
    source, fitpack, app = source.resolve(), fitpack.resolve(), app.resolve()
    if source == app or source in app.parents or app in source.parents:
        raise ValueError('Source and application archive must be separate directories')
    sys.path.insert(0, str(app / 'lqcdmcp-deploy'))
    from jamai.analysis_catalog import Catalog
    catalog = Catalog(app / 'analyses')
    catalog_page = catalog.get('JAM24.final')
    config = json.loads((app / 'analyses/JAM24/catalog/analysis_config.json').read_bytes())
    source_hashes = {name: sha(source / name) for name in SOURCE_FILES}
    loaded = {name: np.load(source / name, allow_pickle=True, encoding='latin1').item() for name in SOURCE_FILES}
    replicas, theory, experimental, old_mean = (loaded[name] for name in SOURCE_FILES)
    nrep, ncolumns = replicas['params'].shape
    if ncolumns != len(replicas['order']) or not np.all(np.isfinite(replicas['params'])):
        raise ValueError('Original parameter columns are invalid')
    dataset_keys = {(r, int(d)) for r, rows in theory.items() for d in rows}
    for archive in (experimental, old_mean):
        if {(r, int(d)) for r, rows in archive.items() for d in rows} != dataset_keys:
            raise ValueError('Original dataset keys disagree across archives')
    mappings = {(m['reaction'], int(m['official_dataset_id'])): m for m in config['source_verification']['dataset_normalization_audit']['paper_to_input_rows']}
    if set(mappings) != dataset_keys:
        raise ValueError('Catalog accepted dataset list differs from original numerical files')
    get_zscore, zscore_provenance = original_zscore(source)
    native, workbook_metadata, reader_warnings = native_experiments(replicas, theory, experimental, fitpack)
    digest = hashlib.sha256(json.dumps({'sources': source_hashes, 'catalog': catalog_page['catalog_ref'],
        'builder': os.environ.get('JAM24_ARCHIVE_BUILDER_SHA256')}, sort_keys=True).encode()).hexdigest()
    data_root = app / 'analyses/JAM24/results/data'
    data_root.mkdir(parents=True, exist_ok=True)
    ignore = data_root / '.gitignore'
    expected_ignore = '*\n!.gitignore\n'
    if ignore.exists() and ignore.read_text() != expected_ignore:
        raise ValueError('Unexpected archive ignore policy; refusing to overwrite')
    if not ignore.exists():
        ignore.write_text(expected_ignore)
    destination = data_root / digest[:16]
    destination.mkdir()  # Existing archives are immutable; never overwrite them.
    if data_root.resolve() not in destination.resolve().parents or source in destination.resolve().parents:
        raise ValueError('Unsafe archive destination')
    if subprocess.run(['git', 'check-ignore', '-q', str(destination / 'parameters.npz')], cwd=app).returncode != 0:
        raise ValueError('Large numeric archives must be Git-ignored before writing')
    for name in ('datasets', 'means', 'replicas'):
        (destination / name).mkdir()
    row_hashes = np.asarray([hashlib.sha256(row.tobytes()).hexdigest() for row in replicas['params']])
    save_numeric(destination / 'parameters.npz', {'values': replicas['params'], 'source_ordinal': np.arange(nrep), 'row_sha256': row_hashes})
    with (source / 'replicas_cut.npy').open('rb') as original, (destination / 'original_parameter_bundle.npy.gz').open('wb') as target:
        with gzip.GzipFile(fileobj=target, mode='wb', mtime=0) as packed:
            shutil.copyfileobj(original, packed)
    with gzip.open(destination / 'original_parameter_bundle.npy.gz', 'rb') as original:
        if hashlib.sha256(original.read()).hexdigest() != source_hashes['replicas_cut.npy']:
            raise ValueError('Original parameter bundle did not round-trip exactly')
    metadata = {'order': replicas['order'], 'source_configuration': replicas['conf'],
        'catalog_parameter_definitions': config['source_verification']['parameter_definitions'],
        'catalog_ref': catalog_page['catalog_ref'], 'count': nrep, 'columns': ncolumns,
        'ordering': 'Source ordinal 0..N-1 exactly preserved. No original fit-job identifier is asserted.',
        'source_configuration_key_types': 'JSON object keys are strings; source datasets xlsx/norm keys were integers. Exact original key types survive in original_parameter_bundle.npy.gz.',
        'sum_rule_note': 'Four PDF fixed:true normalizations are FitPack-determined, not independently fitted columns; all links and original conf are preserved for native recomputation.'}
    precut = source / 'replicas_cut_all.npy'
    if precut.exists():
        all_replicas = np.load(precut, allow_pickle=True).item()
        if all_replicas['order'] == replicas['order']:
            index = {}
            for i, row in enumerate(all_replicas['params']):
                index.setdefault(hashlib.sha256(row.tobytes()).hexdigest(), []).append(i)
            if all(len(index.get(k, [])) == 1 for k in row_hashes):
                save_numeric(destination / 'precut_order.npz', {'source_ordinal': np.arange(nrep), 'precut_ordinal': [index[k][0] for k in row_hashes]})
                metadata['precut_source'] = {'path': str(precut), 'sha256': sha(precut), 'rows': len(all_replicas['params']), 'method': 'Unique exact row-byte matching; no numeric tolerance or reordering.'}
    write_json(destination / 'parameter_metadata.json', metadata)
    rows, comparisons, alignment = [], [], []
    for reaction, datasets in theory.items():
        for dataset_id, prediction in datasets.items():
            key = f'{reaction}_{dataset_id}'
            info = native[(reaction, dataset_id)]
            tab, exp = info['tab'], experimental[reaction][dataset_id]
            if prediction['thy'].shape[0] != nrep:
                raise ValueError('Replica count mismatch: ' + key)
            values, root_id, status = normalization_values(replicas, reaction, dataset_id)
            row, mean, point_residual = dataset_statistics(prediction, exp, get_zscore, values, info['dN'], status)
            mapping = mappings[(reaction, dataset_id)]
            book = workbook_metadata[str((fitpack / 'database' / info['xlsx_path']).resolve())]
            catalog_dataset = next(d for d in config['source_verification']['datasets'][reaction]['entries'] if int(d['id']) == dataset_id)
            if book['original_points'] != mapping['n_before_cuts'] or book['sha256'] != catalog_dataset['current_reader_verification']['sha256']:
                raise ValueError('Current experimental workbook changed from the approved catalog: ' + key)
            row.update(reaction=reaction, dataset_id=str(dataset_id), key=f'{reaction}:{dataset_id}',
                experiment=mapping['dataset'], observable=mapping['observable_tex'],
                original_points=mapping['n_before_cuts'], xlsx_path=info['xlsx_path'],
                point_count_display=f"{row['accepted_points']}/{mapping['n_before_cuts']}",
                publication_row=mapping['publication_row'], normalization_root_id=root_id,
                published_symbols={k: mapping[k] for k in ('paper_unpenalized_asterisk', 'paper_shared_dagger', 'paper_covariance_star')})
            row['nuisance_coordinate_provenance'] = ('Original rres coordinate order retained; current native covariance eigenvector identities/signs are not asserted.'
                if any(name.startswith('new_') for name in info['correlated_names']) else
                'Original rres coordinate order retained; current native correlated-column count verified against it.')
            if row['accepted_points'] != mapping['n_after_cuts']:
                raise ValueError('Accepted point count differs from approved catalog')
            identity_error = float(np.max(np.abs(prediction['res'] - (exp['value'][None, :] - prediction['thy']) / exp['alpha'][None, :])))
            eps = np.finfo(np.float64).eps
            gamma8 = 8 * eps / (1 - 8 * eps)
            rounding_bound = gamma8 * ((np.abs(exp['value'][None, :]) + np.abs(prediction['thy']) +
                2 * np.abs(prediction['shift'])) / exp['alpha'][None, :] + np.abs(prediction['res']))
            residual_difference = np.abs(prediction['res'] - (exp['value'][None, :] - prediction['thy']) / exp['alpha'][None, :])
            if not np.all(residual_difference <= rounding_bound):
                raise ValueError('Original replica residual/point alignment differs beyond float64 reassociation error: ' + key)
            alignment.append({'key': row['key'], 'replicas': nrep, 'residual_identity_max_abs_difference': identity_error,
                'float64_eight_operation_forward_error_bound_max': float(np.max(rounding_bound))})
            row['normalization']['stored_nres_mean'] = mean['nres'].tolist()
            row['normalization']['stored_nres_mean_squared'] = float(np.sum(mean['nres'] ** 2))
            if values is not None:
                reconstructed = (values - 1) / info['dN']
                row['normalization']['original_nres_max_abs_difference'] = float(np.max(np.abs(reconstructed - prediction['nres'][:, 0])))
            numeric_tab = {name: np.asarray(value) for name, value in tab.items()}
            numeric_tab['correlated_column_names'] = np.asarray(info['correlated_names'], dtype=str)
            save_numeric(destination / 'datasets' / (key + '.npz'), numeric_tab)
            save_numeric(destination / 'means' / (key + '.npz'), dict(mean,
                prediction_std=np.std(prediction['thy'], axis=0, ddof=0),
                shift_std=np.std(prediction['shift'], axis=0, ddof=0),
                value=exp['value'], alpha=exp['alpha'], point_residual=point_residual,
                source_row=tab['archive_source_row']))
            save_numeric(destination / 'replicas' / (key + '.npz'), prediction)
            previous = old_mean[reaction][dataset_id]
            check = {name: comparison(mean[name], previous[name]) for name in ('thy', 'shift', 'rres', 'nres')}
            check.update(std=comparison(np.std(prediction['thy'], axis=0, ddof=0), previous['std']),
                res2=comparison(point_residual ** 2, previous['res2']),
                chi2_red=comparison(row['chi2_red'], previous['rchi2']),
                chi2_cor=comparison(float(np.sum(mean['rres'] ** 2)), previous['chi2corr']))
            comparisons.append({'key': row['key'], 'previous_avg_thy': check, 'appendix': appendix_comparison(row, mapping)})
            rows.append(row)
    reactions = {reaction: aggregate([r for r in rows if r['reaction'] == reaction], get_zscore) for reaction in theory}
    overall = aggregate(rows, get_zscore)
    if overall['accepted_points'] != 5853 or len(rows) != len(mappings):
        raise ValueError('Final catalog fit totals do not agree')
    summary = {'schema': 'femtomind.reference_results.1', 'analysis_id': 'JAM24.final',
        'catalog_ref': catalog_page['catalog_ref'], 'archive_id': digest[:16], 'replica_count': nrep,
        'datasets': rows, 'reactions': reactions, 'overall': overall,
        'configured_zero_retained_datasets': [{'reaction': rx, 'dataset_id': d['id'],
            'reason': 'Configured file retains zero points; absent from original thy_cut/all_exp, not a silently dropped dataset.'}
            for rx, data in config['source_verification']['datasets'].items() for d in data['entries']
            if (rx, int(d['id'])) not in dataset_keys and d['current_reader_verification']['retained_count'] == 0],
        'definitions': {'chi2_red': 'sum(((mean(fitted_prediction)-original_value)/alpha)**2)/N_accepted; correlated and normalization penalties are separate.',
            'fitted_prediction': 'Original thy_cut thy is FitPack prediction = raw theory / normalization + systematic shift. Do not divide by normalization or add shifts again.',
            'chi2_cor': 'sum(mean(rres,axis=0)**2), not mean(sum(rres**2)).',
            'chi2_norm': '(mean(fitted_norm)-1)**2/dN**2; linked-row values retained for display, but their objective contribution is counted at the independent root only.',
            'objective_like_at_ensemble_means': 'Data chi2 of mean fitted predictions + squared mean nuisance residuals + unique normalization penalties. This is not a refit at mean parameters or mean replica objective.',
            'z_score': zscore_provenance, 'normalization_uncertainty': 'np.std(original fitted normalization replicas,ddof=0); exact source notebook convention.',
            'missing': 'Null plus reason means unavailable/not applicable; it does not mean zero.'}}
    discrepancies = [{'key': c['key'], 'metric': metric, **value}
        for c in comparisons for metric, value in c['appendix']['checks'].items() if value['status'] == 'disagreement']
    validation = {'dataset_count': len(rows), 'accepted_points': overall['accepted_points'],
        'replicas': nrep, 'parameter_columns': ncolumns, 'native_value_alpha_exact': True,
        'original_source_sha256': source_hashes, 'dataset_comparisons': comparisons,
        'replica_point_alignment': alignment, 'appendix_disagreements': discrepancies,
        'reader_warnings': reader_warnings, 'workbooks': workbook_metadata,
        'mean_prediction_max_abs_difference': max(c['previous_avg_thy']['thy']['max_absolute_difference'] for c in comparisons)}
    write_json(destination / 'summary.json', summary)
    write_json(destination / 'validation.json', validation)
    table = ['# JAM24 original fitted-ensemble reference', '',
        'Computed from the mean fitted prediction; correlated and normalization penalties are separate. A dash means not applicable/unavailable.', '',
        '| dataset | observable | N_dat | N_cor | chi2_red (Z) | chi2_cor | chi2_norm | fitted norm. |',
        '|---|---|---:|---:|---:|---:|---:|---:|']
    for row in rows:
        normal = row['normalization']
        norm_text = '-'
        if normal['value'] is not None:
            printed = mappings[(row['reaction'], int(row['dataset_id']))].get('fitted_norm_as_printed', '')
            pattern = re.search(r'(\d+\.\d+)\((\d+)\)', printed)
            precision = len(pattern[1].split('.')[1]) if pattern else 3
            norm_text = f"{normal['value']:.{precision}f}"
            if normal['uncertainty'] is not None:
                norm_text += '(' + str(int(round(normal['uncertainty'] * 10 ** precision))) + ')'
        show = lambda value: '-' if value is None else f'{value:.2f}'
        table.append(f"| {row['key']} {row['experiment']} | {row['observable']} | {row['point_count_display']} | {row['n_cor'] or '-'} | {row['chi2_red']:.2f} ({show(row['z_score'])}) | {show(row['chi2_cor'])} | {show(normal['chi2'])} | {norm_text} |")
    (destination / 'appendix_preview.md').write_text('\n'.join(table) + '\n', encoding='utf-8')
    provenance = {'source_directory': str(source), 'source_files': {name: {'sha256': digest, 'bytes': (source / name).stat().st_size} for name, digest in source_hashes.items()},
        'catalog_ref': catalog_page['catalog_ref'], 'zscore_source': zscore_provenance,
        'fitpack_checkout': str(fitpack), 'fitpack_head': subprocess.check_output(['git','rev-parse','HEAD'],cwd=fitpack,text=True).strip(),
        'fitpack_tracked_status': subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=fitpack,text=True),
        'implementation_sha256': {rel: sha(fitpack / rel) for rel in ['tools/residuals.py','fitlib/maxlike.py','fitlib/parman.py','tools/reader.py'] + ['obslib/'+r+'/reader.py' for r in theory]},
        'numpy_version': np.__version__, 'scipy_version': importlib.import_module('scipy').__version__,
        'builder_sha256': os.environ.get('JAM24_ARCHIVE_BUILDER_SHA256'),
        'read_only_source': True, 'physics_evaluations': 0, 'fits': 0, 'replicas_excluded': 0}
    if {name: sha(source / name) for name in SOURCE_FILES} != source_hashes:
        raise ValueError('Original numerical sources changed during archival')
    write_json(destination / 'provenance.json', provenance)
    members = {str(p.relative_to(destination)): {'bytes': p.stat().st_size, 'sha256': sha(p)} for p in sorted(destination.rglob('*')) if p.is_file()}
    write_json(destination / 'manifest.json', {'archive_id': summary['archive_id'], 'catalog_ref': summary['catalog_ref'], 'files': members,
        'source_hashes_verified_after_build': True, 'all_npz_members_round_trip_exact': True})
    print(json.dumps({'archive': str(destination), 'replicas': nrep, 'datasets': len(rows),
        'accepted_points': overall['accepted_points'], 'overall': overall,
        'mean_prediction_max_abs_difference': validation['mean_prediction_max_abs_difference'],
        'appendix_disagreements': discrepancies, 'file_count': len(members)+1,
        'bytes_without_manifest': sum(x['bytes'] for x in members.values()),
        'wc': [r for r in rows if r['reaction'] == 'wc']}, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--fitpack', type=Path, required=True)
    parser.add_argument('--app', type=Path, required=True)
    args = parser.parse_args()
    build(args.source, args.fitpack, args.app)
