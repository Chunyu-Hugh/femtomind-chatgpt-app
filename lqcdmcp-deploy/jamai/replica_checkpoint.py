"""Application-owned, atomic checkpoint records; no FitPack serialization changes."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

SCHEMA = 'femtomind.replica-checkpoint.v1'
RESTART_POINTS = {'initial_optimizer_base', 'accepted_optimizer_iterate'}


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def file_hash(path):
    value = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, sort_keys=True, separators=(',', ':'), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        # Persist the rename, not just its file contents, across node interruption.
        if os.name != 'nt':  # Windows cannot open a directory for fsync.
            folder = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def checkpoint_root(stage, replica):
    # A reproduction runs beside the original replicas and must not touch their
    # checkpoints: its array script points JAMAI_CHECKPOINT_ROOT at its own folder.
    root = os.environ.get('JAMAI_CHECKPOINT_ROOT') or (Path(stage) / 'checkpoints')
    return Path(root) / ('replica_%d' % int(replica))


def campaign_identity(stage):
    stage = Path(stage).resolve()
    campaign = stage.parent.parent.name if stage.parent.name == 'stages' else stage.name
    return {'campaign_id': campaign, 'stage_path': str(stage)}


def resolve(stage, replica, expected=None):
    """Validate one full record and immutable bootstrap payload, without NumPy."""
    root = checkpoint_root(stage, replica)
    doc = json.loads((root / 'latest.json').read_text())
    checksum = doc.pop('sha256', None)
    if checksum != digest(doc) or doc.get('schema') != SCHEMA:
        raise ValueError('Checkpoint record integrity failure')
    doc['sha256'] = checksum
    if expected is not None and checksum != expected:
        raise ValueError('Checkpoint changed after the confirmed resume plan')
    if doc.get('replica') != int(replica):
        raise ValueError('Checkpoint replica identity mismatch')
    if doc.get('campaign') != campaign_identity(stage):
        raise ValueError('Checkpoint campaign/stage identity mismatch')
    if doc.get('input_sha256') != file_hash(Path(stage) / 'input.py'):
        raise ValueError('Checkpoint scientific input changed')
    if doc.get('checkpoint_point') not in RESTART_POINTS:
        raise ValueError('Checkpoint is not a verified initial/accepted optimizer vector; legacy evaluation checkpoints cannot be resumed')
    order, values = doc.get('order', []), doc.get('values', [])
    if not order or len(order) != len(values) or len({tuple(x) for x in order}) != len(order):
        raise ValueError('Invalid checkpoint parameter identities')
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in values):
        raise ValueError('Invalid checkpoint parameter values')
    if (type(doc.get('evaluations')) is not int or doc['evaluations'] < 0 or
            type(doc.get('objective')) not in (int, float) or not math.isfinite(doc['objective'])):
        raise ValueError('Invalid checkpoint objective/progress')
    # Only payloads generated in this replica's checkpoint attempt directories.
    relative = Path(doc.get('bootstrap_file', ''))
    if (relative.is_absolute() or len(relative.parts) != 2 or
            not relative.parts[0].startswith('attempt-') or relative.parts[1] != 'bootstrap.npz'):
        raise ValueError('Invalid checkpoint bootstrap reference')
    payload = root / relative
    if file_hash(payload) != doc.get('bootstrap_sha256'):
        raise ValueError('Checkpoint bootstrap integrity failure')
    return doc, payload


def preview(stage, replicas):
    records, failures = {}, []
    for replica in replicas:
        try:
            doc, _ = resolve(stage, replica)
            records[str(replica)] = {'sha256': doc['sha256'], 'evaluations': doc['evaluations'],
                                     'objective': doc['objective'], 'input_sha256': doc['input_sha256'],
                                     'bootstrap_sha256': doc['bootstrap_sha256'],
                                     'checkpoint_point': doc['checkpoint_point'],
                                     'point_evaluations': doc.get('point_evaluations'),
                                     'accepted_steps': doc.get('accepted_steps')}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            failures.append({'replica': replica, 'error': str(exc)})
    return {'schema': 'femtomind.resume-plan.v1', 'records': records, 'failures': failures,
            'optimizer_state_restored': False}
