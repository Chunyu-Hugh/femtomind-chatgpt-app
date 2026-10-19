from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from .dataset_catalog import CONFIGURATION_DATASETS, CONFIGURATION_ONLY_REACTIONS, configuration_dataset_block
from .object_registry import (
    DY_DATASET_DEFAULTS, PROTON_REACTION_DATASETS, cut_filters, dis_version_for, library_cuts, load_object_libraries,
)
from .stage_resolver import ResolvedStage
from .ff_registry import FF_OBJECTS, FF_PARAMETERS, ff_library, resolve_ff_parameters


def render_dataset_configuration_fragment(selections: dict, q2cut=None) -> str:
    """Render dataset assignments for inspection, not a runnable FF fit card."""
    blocks = {reaction: configuration_dataset_block(reaction, dataset_ids, q2cut)
              for reaction, dataset_ids in selections.items()}
    lines = [
        '# Configuration only: SIDIS/SIA FF fitting is not implemented.',
        "conf.setdefault('datasets', {})",
    ]
    lines.extend(f"conf['datasets'][{reaction!r}] = {block!r}"
                 for reaction, block in blocks.items())
    return '\n'.join(lines) + '\n'


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


def _enabled_cuts(stage: ResolvedStage) -> dict:
    # {'idis': [{'variable': 'Q2', 'op': '>', 'value': 1.69, 'enabled': True}]}
    # -> {'idis': ['Q2>1.69']}, the filter strings FitPack readers apply.
    # Only reactions in the stage, only enabled cuts, only plain comparisons.
    out = {}
    for reaction in stage.reactions:
        if reaction in ('sidis', 'sia'):
            continue  # Exact FF dataset filters are installed from the canonical catalog.
        if reaction in (stage.cuts or {}):
            filters = cut_filters(stage.cuts[reaction])
            if isinstance(filters, dict):
                missing = set(map(str, stage.datasets.get(reaction, []))) - filters.keys()
                if missing:
                    raise ValueError(f'{reaction}: missing cuts for datasets {sorted(missing)}')
            if reaction in PROTON_REACTION_DATASETS:
                approved = cut_filters(library_cuts(
                    PROTON_REACTION_DATASETS[reaction].get('filters', [])))
                supplied = filters.values() if isinstance(filters, dict) else [filters]
                if any(value != approved for value in supplied):
                    raise ValueError(f'{reaction}: cuts must match the approved reaction configuration')
                if not approved:
                    continue
            if not filters:
                continue
            out[reaction] = ({int(key): value for key, value in filters.items()
                              if int(key) in stage.datasets.get(reaction, [])}
                             if isinstance(filters, dict) else filters)
    return out


def _render_stage_override_block(stage: ResolvedStage) -> str:
    # FitPack input files are Python programs. The safest bridge is therefore a
    # Python snippet appended to the copied template. When FitPack executes the
    # input card, these assignments/filtering operations mutate `conf` for this
    # stage.
    positivity = {obj: stage.qcd.get(f'{obj}Positivity', True)
                  for obj in ('pdf', 'ffpion', 'ffkaon', 'ffhadron')}
    if any(type(value) is not bool for value in positivity.values()):
        raise ValueError('Positivity settings must be true or false')
    for reaction in stage.reactions:
        if reaction in PROTON_REACTION_DATASETS:
            unknown = set(stage.datasets.get(reaction, [])) - PROTON_REACTION_DATASETS[reaction]['xlsx'].keys()
            if unknown:
                raise ValueError(f'{reaction}: unsupported datasets {sorted(unknown)}')
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
                    if reaction in ('sidis', 'sia'):
                        selected = stage.datasets.get(reaction, []) if reaction in stage.reactions else []
                        expected = CONFIGURATION_DATASETS[reaction]['norm'].get(int(dataset_id))
                        supplied = {field: parameter.get(field) for field in ('value', 'min', 'max', 'fixed')}
                        if (int(dataset_id) not in selected or expected is None or supplied != expected
                                or type(supplied['fixed']) is not type(expected['fixed'])):
                            raise ValueError(f'{reaction}:{dataset_id}: preserve the selected authoritative normalization definition')
                    if reaction in PROTON_REACTION_DATASETS:
                        selected = stage.datasets.get(reaction, []) if reaction in stage.reactions else []
                        allowed = PROTON_REACTION_DATASETS[reaction]['norm']
                        if int(dataset_id) not in selected or int(dataset_id) not in allowed:
                            raise ValueError(f'{reaction}: normalization {dataset_id} is not an explicit selected entry')
                        tied = parameter.get('fixed', False)
                        if type(tied) is int and (tied not in selected or tied not in allowed):
                            raise ValueError(f'{reaction}: normalization {dataset_id} has an invalid equal-to dataset')
                    norm_overrides.setdefault(reaction, {})[dataset_id] = {
                        'value': parameter.get('value'),
                        'min': parameter.get('min'),
                        'max': parameter.get('max'),
                        # True/False, or the integer id of the dataset whose
                        # normalization this one equals (FitPack's convention).
                        'fixed': parameter.get('fixed', False),
                    }
                continue
            if sector_key in FF_OBJECTS:
                continue  # Complete enabled FF definitions are installed as a snapshot below.
            parameter_name = str(parameter.get('name') or '').strip()
            if not parameter_name:
                continue
            if sector_key == 'pdf-pion' and parameter_name.partition(' ')[0].endswith('2'):
                raise ValueError(f'Second-shape pion parameter is not allowed: {parameter_name}')
            # Parameter names are only unique within their FitPack sector.
            parameter_overrides.setdefault(sector_key, {})[parameter_name] = {
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
        "assert all(_key in _ACTIVE_FIT_OBJECTS for _key in _PARAMETER_OVERRIDES), 'Cannot override inactive object'",
        "for _object_name, _overrides in _PARAMETER_OVERRIDES.items(): [conf['params'][_object_name][_param_name].update(_override) for _param_name, _override in _overrides.items()]",
        "for _object_name, _object_params in list(conf.get('params', {}).items()): _FREEZE_OBJECT_PARAMS(_object_params) if _object_name not in _ACTIVE_FIT_OBJECTS else None",
        "for _reaction, _dataset_overrides in _NORM_OVERRIDES.items(): _norm_entries = _DATASET_CONF.setdefault(_reaction, {}).setdefault('norm', {}); [_norm_entries.setdefault(int(_dataset_id), {}).update(_override) for _dataset_id, _override in _dataset_overrides.items()]",
        # FemtoMind gateway additions. Upstream only removed datasets from the
        # template, so a dataset the template does not list was silently
        # dropped; selected ids are added here with the repository's data path.
        f'_STAGE_CUTS = {_enabled_cuts(stage)!r}',
        "for _reaction in _ALLOWED_REACTIONS: _dataset_entry = _DATASET_CONF.setdefault(_reaction, {}); _xlsx = _dataset_entry.setdefault('xlsx', {}); [_xlsx.setdefault(int(_dataset_id), '%s/expdata/%d.xlsx' % (_reaction, int(_dataset_id))) for _dataset_id in _STAGE_DATASETS.get(_reaction, [])]",
        # Upstream carried the cuts in the manifest but never wrote them.
        "for _reaction, _filters in _STAGE_CUTS.items(): _DATASET_CONF.setdefault(_reaction, {})['filters'] = _filters",
        "# Temporary app compatibility policy; remove when pion PDFs support IDIS v1.",
        "conf.pop('dis_version', None)",
        f"conf['dis version'] = {dis_version_for(stage.effective_fit_objects)!r}",
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
        # Explicit campaign choices override template defaults. Legacy manifests
        # without these fields retain the previous all-enabled behavior.
        *[f"conf[{(obj + ' positivity')!r}] = {value!r}" for obj, value in positivity.items()],
        f"conf['jamai parameter initialization'] = {stage.params.get('initializationMode') or stage.params.get('initialization', 'default_prior')!r}",
        f"conf['jamai parameter random seed'] = {stage.params.get('randomSeed', None)!r}",
        f"conf['jamai per replica seed'] = {bool(stage.params.get('perReplicaSeed', True))!r}",
        f"conf['jamai base seed'] = {stage.params.get('baseSeed', 120000)!r}",
        f"conf['jamai parameter sectors'] = {stage.params.get('sectors', [])!r}",
        "conf['jamai bootstrap seed offset'] = 100000",
        '',
    ]
    return '\n'.join(lines)


def materialize_stage_input(template_path: str | Path, output_path: str | Path, stage: ResolvedStage,
                            fitpack_root: str | Path | None = None) -> str:
    ff_blocks = {}
    for reaction in ('sidis', 'sia'):
        if reaction not in stage.reactions:
            continue
        if not stage.datasets.get(reaction):
            raise ValueError(f'{reaction}: select at least one dataset')
        cutoff = None
        if reaction == 'sidis':
            cuts = stage.cuts.get('sidis', stage.cuts.get('idis', []))
            qcuts = [cut for cut in cuts if cut.get('enabled', True)
                     and cut.get('variable') == 'Q2' and cut.get('op', '>') == '>']
            if len(qcuts) != 1 or sum(bool(cut.get('enabled', True)) for cut in cuts) != 1:
                raise ValueError('SIDIS requires exactly its configured Q2 > cutoff, without other cuts')
            cutoff = qcuts[0]['value']
        ff_blocks[reaction] = configuration_dataset_block(reaction, stage.datasets.get(reaction, []), cutoff)
        if reaction == 'sia' and 'sia' in stage.cuts:
            supplied = stage.cuts['sia']
            if isinstance(supplied, dict):
                supplied = {int(idx): value for idx, value in supplied.items()}
            if supplied != ff_blocks['sia']['filters']:
                raise ValueError('SIA cuts must match the selected authoritative dataset definitions')
    ff_objects = [key for key in FF_OBJECTS if key in stage.fit_objects or key in stage.frozen_objects]
    ff_params = {}
    for key in ff_objects:
        raw = stage.params.get('ff_parameters', {}).get(key)
        if key not in stage.effective_fit_objects:
            raw = None
        if raw is None:
            sectors = [sector for sector in stage.params.get('sectors', []) if sector.get('key') == key]
            edits = sectors[0].get('parameters', []) if sectors and key in stage.effective_fit_objects else None
        else:
            if not isinstance(raw, dict):
                raise ValueError(f'{key}: complete FF parameters must be a dictionary')
            unknown = set(raw) - set(FF_PARAMETERS[key])
            if unknown:
                raise ValueError(f'{key}: unknown parameters {sorted(unknown)}')
            # A snapshot is exhaustive: absent active and optional definitions stay absent.
            edits = {name: ({**raw[name], 'enabled': True} if name in raw else {'enabled': False})
                     for name in FF_PARAMETERS[key]}
        ff_params[key] = resolve_ff_parameters(key, edits)
    # Validate sources before copying, then compose the single final input.
    # Replace only the five approved reaction blocks before dataset pruning.
    # This also removes historical Jet filters and preserves omitted WC filters.
    fragments = [f"\nconf.setdefault('datasets', {{}})[{reaction!r}] = {PROTON_REACTION_DATASETS[reaction]!r}"
                 for reaction in stage.reactions if reaction in PROTON_REACTION_DATASETS]
    if 'dy' in stage.reactions:
        fragments.append(f"\nconf.setdefault('datasets', {{}})['dy'] = {DY_DATASET_DEFAULTS!r}")
    for reaction, block in ff_blocks.items():
        fragments.append(f"\nconf.setdefault('datasets', {{}})[{reaction!r}] = {block!r}")
    for key, params in ff_params.items():
        fragments.append(f"\nconf.setdefault('params', {{}})[{key!r}] = {params!r}")
        fragments.extend(f"conf[{name!r}] = {value!r}" for name, value in ff_library(key)['settings'].items())
    if ff_objects:
        # The proton template is only a settings scaffold for FF-only SIA.
        # Remove unused template distributions instead of initializing an unrelated PDF.
        keep = set(stage.fit_objects) | set(stage.frozen_objects)
        fragments.append(f"\n_FF_STAGE_OBJECTS = {sorted(keep)!r}")
        fragments.append("for _key in ('pdf', 'pdf-pion', 'ffpion', 'ffkaon', 'ffhadron'): conf.get('params', {}).pop(_key, None) if _key not in _FF_STAGE_OBJECTS else None")
    overrides = _render_stage_override_block(stage)
    for key, source in stage.params.get('library_sources', {}).items():
        if key in FF_OBJECTS:
            if source.get('sha256') and source['sha256'] != ff_library(key)['sha256']:
                raise ValueError(f'{key} library changed since validation; validate the card again')
            continue
        if source.get('source') and source.get('sha256'):
            actual = hashlib.sha256(Path(source['source']).read_bytes()).hexdigest()
            if actual != source['sha256']:
                raise ValueError(f'{key} library changed since validation; validate the card again')
    additional = [key for key in ('pdf-pion', 'p->pi,n')
                  if key in stage.fit_objects or key in stage.frozen_objects]
    if additional:
        root = fitpack_root or Path(template_path).resolve().parent.parent
        source = stage.params.get('library_sources', {}).get('pdf-pion', {})
        libraries = load_object_libraries(root, additional, pion_reference=source.get('source'))
        for key, library in libraries.items():
            fragments.extend([
                f'\n# {key} library: {library["source"]} (sha256 {library["sha256"]})',
                f"conf['params'][{key!r}] = {library['params']!r}",
            ])
            fragments.extend(f'conf[{name!r}] = {value!r}'
                             for name, value in library['settings'].items())
            fragments.extend(f"conf['datasets'][{reaction!r}] = {dataset!r}"
                             for reaction, dataset in library['datasets'].items()
                             if reaction in stage.reactions)
        if 'pdf-pion' in libraries and 'pdf' not in stage.effective_fit_objects:
            fragments.append(f"conf['params']['pdf'] = {libraries['pdf-pion']['fixed_companions']['pdf']!r}")
        if 'ln' in stage.reactions:
            fragments.append(f"conf['path2idistab'] = {str(Path(root) / 'grids' / 'grids-idis' / 'distab')!r}")
    output_file = Path(copy_stage_input_template(template_path, output_path))
    with output_file.open('a', encoding='utf-8') as f:
        f.write('\n'.join(fragments))
        f.write(overrides)
    return str(output_file)


def read_stage_input_preview(output_path: str | Path, max_lines: int = 24) -> str:
    # The appended override block is at the end of the file, so the preview
    # returns the tail rather than the first lines of the legacy template.
    lines = Path(output_path).read_text(encoding='utf-8').splitlines()
    if len(lines) <= max_lines:
        return '\n'.join(lines)
    return '\n'.join(lines[-max_lines:])
