#!/usr/bin/env python3
"""Independent read-only verification of an existing JAM24 reference archive."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import numpy as np


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def verify(root):
    root = root.resolve()
    manifest = json.loads((root / 'manifest.json').read_bytes())
    summary = json.loads((root / 'summary.json').read_bytes())
    provenance = json.loads((root / 'provenance.json').read_bytes())
    metadata = json.loads((root / 'parameter_metadata.json').read_bytes())
    source = Path(provenance['source_directory'])
    for name, record in manifest['files'].items():
        path = root / name
        if root not in path.resolve().parents or path.is_symlink():
            raise ValueError('Unexpected archive member path')
        if path.stat().st_size != record['bytes'] or sha(path) != record['sha256']:
            raise ValueError('Archive member checksum mismatch: ' + name)
    for name, record in provenance['source_files'].items():
        if sha(source / name) != record['sha256']:
            raise ValueError('Original source changed: ' + name)
    original = np.load(source / 'replicas_cut.npy', allow_pickle=True).item()
    with np.load(root / 'parameters.npz', allow_pickle=False) as archived:
        if not np.array_equal(archived['values'], original['params']) or metadata['order'] != original['order']:
            raise ValueError('Parameter values/order changed')
        if not np.array_equal(archived['source_ordinal'], np.arange(len(original['params']))):
            raise ValueError('Replica order changed')
    with gzip.open(root / 'original_parameter_bundle.npy.gz', 'rb') as packed:
        if hashlib.sha256(packed.read()).hexdigest() != provenance['source_files']['replicas_cut.npy']['sha256']:
            raise ValueError('Original parameter copy changed')
    theory = np.load(source / 'thy_cut.npy', allow_pickle=True).item()
    experimental = np.load(source / 'all_exp.npy', allow_pickle=True).item()
    averaged = np.load(source / 'avg_thy.npy', allow_pickle=True).item()
    datasets, means_exact, replica_members, nullable_tag_files = 0, 0, 0, 0
    nonfinite_native_columns = []
    for reaction, rows in theory.items():
        for dataset_id, predictions in rows.items():
            key = f'{reaction}_{dataset_id}'
            with np.load(root / 'replicas' / (key + '.npz'), allow_pickle=False) as archived:
                if set(archived.files) != set(predictions):
                    raise ValueError('Replica metric member set changed: ' + key)
                for name, array in predictions.items():
                    if archived[name].dtype != array.dtype or not np.array_equal(archived[name], array):
                        raise ValueError('Replica arrays changed: ' + key + '/' + name)
                    replica_members += 1
            with np.load(root / 'means' / (key + '.npz'), allow_pickle=False) as mean:
                for field in ('value', 'alpha'):
                    if not np.array_equal(mean[field], experimental[reaction][dataset_id][field]):
                        raise ValueError('Experimental arrays changed: ' + key + '/' + field)
                if not np.array_equal(mean['thy'], averaged[reaction][dataset_id]['thy']):
                    raise ValueError('Mean differs from original avg_thy: ' + key)
                means_exact += 1
            with np.load(root / 'datasets' / (key + '.npz'), allow_pickle=False) as data:
                if '__archive_json_columns__' in data.files:
                    nullable_tag_files += 1
                for field in data.files:
                    array = data[field]
                    if array.dtype.kind in 'fc' and np.any(~np.isfinite(array)):
                        nonfinite_native_columns.append({'dataset': reaction + ':' + str(dataset_id), 'column': field,
                            'count': int(np.sum(~np.isfinite(array)))})
            datasets += 1
    if datasets != len(summary['datasets']) or sum(row['accepted_points'] for row in summary['datasets']) != 5853:
        raise ValueError('Point/dataset totals differ')
    if (root / 'precut_order.npz').exists():
        precut = np.load(metadata['precut_source']['path'], allow_pickle=True).item()
        with np.load(root / 'precut_order.npz', allow_pickle=False) as order:
            if not np.array_equal(precut['params'][order['precut_ordinal']], original['params']):
                raise ValueError('Precut row identity mapping differs')
    result = {'status': 'PASS', 'archive': str(root), 'manifest_sha256': sha(root / 'manifest.json'),
        'file_count': len(manifest['files']) + 1, 'total_bytes': sum(p.stat().st_size for p in root.rglob('*') if p.is_file()),
        'replicas': len(original['params']), 'datasets': datasets, 'accepted_points': 5853,
        'exact_replica_metric_arrays': replica_members, 'exact_dataset_mean_arrays': means_exact,
        'original_source_hashes_unchanged': True, 'parameter_values_and_order_exact': True,
        'nullable_metadata_files': nullable_tag_files, 'nonfinite_native_metadata_columns': nonfinite_native_columns}
    print(json.dumps(result))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    verify(parser.parse_args().archive)
