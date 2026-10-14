from __future__ import annotations

import shutil
from pathlib import Path

from .stage_resolver import ResolvedStage


def copy_stage_input_template(template_path: str | Path, output_path: str | Path) -> str:
    # Start from a known-good legacy FitPack input card, then append JAMAI's
    # generated overrides instead of trying to synthesize a full input.py from
    # scratch.
    src = Path(template_path).expanduser().resolve()
    dst = Path(output_path).expanduser().resolve()

    if not src.exists():
        raise FileNotFoundError(f'Input template not found: {src}')

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return str(dst)


def _enabled_cuts(stage: ResolvedStage) -> dict[str, list[str]]:
    # {'idis': [{'variable': 'Q2', 'op': '>', 'value': 1.69, 'enabled': True}]}
    # -> {'idis': ['Q2>1.69']}, the filter strings FitPack readers apply.
    # Only reactions in the stage, only enabled cuts, only plain comparisons.
    out: dict[str, list[str]] = {}
    for reaction in stage.reactions:
        cuts = (stage.cuts or {}).get(reaction) or []
        filters = []
        for cut in cuts:
            if not isinstance(cut, dict) or not cut.get('enabled', True):
                continue
            variable = str(cut.get('variable') or '').strip()
            op = str(cut.get('op') or '').strip()
            if not variable.isidentifier() or op not in ('>', '>=', '<', '<='):
                raise ValueError(f'Invalid cut for {reaction}: {cut!r}')
            filters.append(f'{variable}{op}{float(cut.get("value")):g}')
        if filters:
            out[reaction] = filters
    return out


def _render_stage_override_block(stage: ResolvedStage) -> str:
    # FitPack input files are Python programs. The safest bridge is therefore a
    # Python snippet appended to the copied template. When FitPack executes the
    # input card, these assignments/filtering operations mutate `conf` for this
    # stage.
    parameter_overrides: dict[str, dict] = {}
    norm_overrides: dict[str, dict[str, dict]] = {}
    for sector in stage.params.get('sectors', []) or []:
        sector_key = str(sector.get('key') or '').strip()
        for parameter in sector.get('parameters', []) or []:
            if sector_key == 'norm':
                # Normalization parameters are stored under datasets/reactions,
                # not in `conf['params']['pdf']`, so split them into a separate
                # override table keyed by reaction and dataset id.
                parameter_id = str(parameter.get('id') or '')
                _, reaction, dataset_id = (parameter_id.split('_', 2) + ['', '', ''])[:3]
                if reaction and dataset_id:
                    norm_overrides.setdefault(reaction, {})[dataset_id] = {
                        'value': parameter.get('value'),
                        'min': parameter.get('min'),
                        'max': parameter.get('max'),
                        'fixed': bool(parameter.get('fixed', False)),
                    }
                continue
            parameter_name = str(parameter.get('name') or '').strip()
            if not parameter_name:
                continue
            # Regular PDF parameter overrides are keyed by the parameter name as
            # it appears inside the FitPack input card.
            parameter_overrides[parameter_name] = {
                'value': parameter.get('value'),
                'min': parameter.get('min'),
                'max': parameter.get('max'),
                # FitPack accepts a parameter name in ``fixed`` to tie the
                # numerical value to that parameter; preserve it instead of
                # coercing every non-empty value to True.
                'fixed': parameter.get('fixed', False),
            }

    lines = [
        '',
        '# --- JAMAI stage overrides ---',
        # The lines below are written as source code into the generated input.py.
        # Keep them compact because this block is meant to be read by FitPack,
        # not imported as helper functions.
        f'_ALLOWED_REACTIONS = {stage.reactions!r}',
        f'_STAGE_DATASETS = {stage.datasets!r}',
        f'_ACTIVE_FIT_OBJECTS = {stage.effective_fit_objects!r}',
        f'_PARAMETER_OVERRIDES = {parameter_overrides!r}',
        f'_NORM_OVERRIDES = {norm_overrides!r}',
        "_DATASET_CONF = conf.setdefault('datasets', {})",
        "_FILTER_STAGE_MAPPING = lambda _mapping, _allowed_ids: [_mapping.pop(_dataset_id, None) for _dataset_id in list(_mapping.keys()) if _dataset_id not in _allowed_ids] if isinstance(_mapping, dict) else None",
        "_FREEZE_OBJECT_PARAMS = lambda _params: [(_entry.__setitem__('fixed', True) if isinstance(_entry, dict) and (_entry.get('fixed') is False or 'fixed' not in _entry) else None) for _entry in _params.values()] if isinstance(_params, dict) else None",
        "for _reaction in list(_DATASET_CONF.keys()): _DATASET_CONF.pop(_reaction, None) if _reaction not in _ALLOWED_REACTIONS else None",
        "for _reaction in _ALLOWED_REACTIONS: _dataset_entry = _DATASET_CONF.get(_reaction, {}); _dataset_ids = _STAGE_DATASETS.get(_reaction, []); _allowed_ids = set(_dataset_ids) if isinstance(_dataset_ids, (list, tuple, set)) else set(); _FILTER_STAGE_MAPPING(_dataset_entry.get('xlsx') if isinstance(_dataset_entry, dict) else None, _allowed_ids); _FILTER_STAGE_MAPPING(_dataset_entry.get('norm') if isinstance(_dataset_entry, dict) else None, _allowed_ids)",
        "for _object_name, _object_params in list(conf.get('params', {}).items()): _FREEZE_OBJECT_PARAMS(_object_params) if _object_name not in _ACTIVE_FIT_OBJECTS else None",
        "_PDF_PARAMS = conf.get('params', {}).get('pdf', {}) if isinstance(conf.get('params', {}), dict) else {}",
        "for _param_name, _override in _PARAMETER_OVERRIDES.items(): _entry = _PDF_PARAMS.get(_param_name); _entry.update({'value': _override.get('value'), 'min': _override.get('min'), 'max': _override.get('max'), 'fixed': _override.get('fixed')}) if isinstance(_entry, dict) else None",
        "for _reaction, _dataset_overrides in _NORM_OVERRIDES.items(): _norm_entries = (_DATASET_CONF.get(_reaction, {}) if isinstance(_DATASET_CONF.get(_reaction, {}), dict) else {}).get('norm', {}); [(_norm_entries.get(int(_dataset_id), {}).update({'value': _override.get('value'), 'min': _override.get('min'), 'max': _override.get('max'), 'fixed': _override.get('fixed')}) if isinstance(_norm_entries.get(int(_dataset_id)), dict) else None) for _dataset_id, _override in _dataset_overrides.items()]",
        # FemtoMind gateway additions. Upstream only removed datasets from the
        # template, so a dataset the template does not list was silently
        # dropped; selected ids are added here with the repository's data path.
        f'_STAGE_CUTS = {_enabled_cuts(stage)!r}',
        "for _reaction in _ALLOWED_REACTIONS: _dataset_entry = _DATASET_CONF.setdefault(_reaction, {}); _xlsx = _dataset_entry.setdefault('xlsx', {}); [_xlsx.setdefault(int(_dataset_id), '%s/expdata/%d.xlsx' % (_reaction, int(_dataset_id))) for _dataset_id in _STAGE_DATASETS.get(_reaction, [])]",
        # Upstream carried the cuts in the manifest but never wrote them.
        "for _reaction, _filters in _STAGE_CUTS.items(): _DATASET_CONF.setdefault(_reaction, {})['filters'] = list(_filters)",
        f"conf['order'] = {stage.qcd.get('order', 'NLO')!r}",
        f"conf['Q20'] = {stage.qcd.get('Q20', 1.6129)!r}",
        f"conf['ftol'] = {stage.qcd.get('ftol', 1.0e-6)!r}",
        f"conf['tmc'] = {stage.qcd.get('tmc', 'AOT')!r}",
        f"conf['ht'] = {bool(stage.qcd.get('higherTwist', False))!r}",
        f"conf['dglap mode'] = {stage.qcd.get('dglapMode', 'truncated')!r}",
        f"conf['alphaSmode'] = {stage.qcd.get('asMode', 'backward')!r}",
        *([f"conf['grid path'] = {stage.qcd.get('grid_path')!r}"] if stage.qcd.get('grid_path') else []),
        f"conf['bootstrap'] = {bool(stage.job.get('bootstrap', False))!r}",
        "conf['resample version'] = 0",
        f"conf['jamai parameter initialization'] = {stage.params.get('initializationMode') or stage.params.get('initialization', 'default_prior')!r}",
        f"conf['jamai parameter random seed'] = {stage.params.get('randomSeed', None)!r}",
        f"conf['jamai per replica seed'] = {bool(stage.params.get('perReplicaSeed', True))!r}",
        f"conf['jamai base seed'] = {stage.params.get('baseSeed', 120000)!r}",
        f"conf['jamai parameter sectors'] = {stage.params.get('sectors', [])!r}",
        "conf['jamai bootstrap seed offset'] = 100000",
        '',
    ]
    return '\n'.join(lines)


def materialize_stage_input(template_path: str | Path, output_path: str | Path, stage: ResolvedStage) -> str:
    # Copy template first, append overrides second, and return the final input.py
    # path that Slurm/JAM can execute.
    output_file = Path(copy_stage_input_template(template_path, output_path))
    with output_file.open('a', encoding='utf-8') as f:
        f.write(_render_stage_override_block(stage))
    return str(output_file)


def read_stage_input_preview(output_path: str | Path, max_lines: int = 24) -> str:
    # The appended override block is at the end of the file, so the preview
    # returns the tail rather than the first lines of the legacy template.
    lines = Path(output_path).read_text(encoding='utf-8').splitlines()
    if len(lines) <= max_lines:
        return '\n'.join(lines)
    return '\n'.join(lines[-max_lines:])
