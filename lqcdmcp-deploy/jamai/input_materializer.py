from __future__ import annotations

import hashlib
import shutil
from pprint import pformat
from pathlib import Path

from .dataset_catalog import CONFIGURATION_DATASETS, CONFIGURATION_ONLY_REACTIONS, configuration_dataset_block, sidis_dis_cut_values
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


def _literal(value):
    return pformat(value, width=100, sort_dicts=False)


def _render_stage_override_block(stage: ResolvedStage, prior_reference=None) -> str:
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
                        if int(dataset_id) not in selected or expected is None:
                            raise ValueError(f'{reaction}:{dataset_id}: no selected normalization parameter')
                        configuration_dataset_block(reaction, selected, normalizations={dataset_id: supplied})
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
        # Keep the generated source readable without changing literal values.
        f'_ALLOWED_REACTIONS = {stage.reactions!r}',
        f'_STAGE_DATASETS = {_literal(stage.datasets)}',
        f'_ACTIVE_FIT_OBJECTS = {stage.effective_fit_objects!r}',
        f'_PARAMETER_OVERRIDES = {_literal(parameter_overrides)}',
        f'_NORM_OVERRIDES = {_literal(norm_overrides)}',
        "_DATASET_CONF = conf.setdefault('datasets', {})",
        "def _FILTER_STAGE_MAPPING(_mapping, _allowed_ids):\n    if isinstance(_mapping, dict):\n        for _dataset_id in list(_mapping):\n            if _dataset_id not in _allowed_ids:\n                _mapping.pop(_dataset_id, None)",
        "def _FREEZE_OBJECT_PARAMS(_params):\n    if isinstance(_params, dict):\n        for _entry in _params.values():\n            if isinstance(_entry, dict) and (_entry.get('fixed') is False or 'fixed' not in _entry):\n                _entry['fixed'] = True",
        "for _reaction in list(_DATASET_CONF):\n    if _reaction not in _ALLOWED_REACTIONS:\n        _DATASET_CONF.pop(_reaction, None)",
        "for _reaction in _ALLOWED_REACTIONS:\n    _dataset_entry = _DATASET_CONF.get(_reaction, {})\n    _dataset_ids = _STAGE_DATASETS.get(_reaction, [])\n    _allowed_ids = set(_dataset_ids) if isinstance(_dataset_ids, (list, tuple, set)) else set()\n    if isinstance(_dataset_entry, dict):\n        _FILTER_STAGE_MAPPING(_dataset_entry.get('xlsx'), _allowed_ids)\n        _FILTER_STAGE_MAPPING(_dataset_entry.get('norm'), _allowed_ids)",
        "assert all(_key in _ACTIVE_FIT_OBJECTS for _key in _PARAMETER_OVERRIDES), 'Cannot override inactive object'",
        "for _object_name, _overrides in _PARAMETER_OVERRIDES.items():\n    for _param_name, _override in _overrides.items():\n        conf['params'][_object_name][_param_name].update(_override)",
        "for _object_name, _object_params in list(conf.get('params', {}).items()):\n    if _object_name not in _ACTIVE_FIT_OBJECTS:\n        _FREEZE_OBJECT_PARAMS(_object_params)",
        "for _reaction, _dataset_overrides in _NORM_OVERRIDES.items():\n    _norm_entries = _DATASET_CONF.setdefault(_reaction, {}).setdefault('norm', {})\n    for _dataset_id, _override in _dataset_overrides.items():\n        _norm_entries.setdefault(int(_dataset_id), {}).update(_override)",
        # FemtoMind gateway additions. Upstream only removed datasets from the
        # template, so a dataset the template does not list was silently
        # dropped; selected ids are added here with the repository's data path.
        f'_STAGE_CUTS = {_enabled_cuts(stage)!r}',
        "for _reaction in _ALLOWED_REACTIONS:\n    _dataset_entry = _DATASET_CONF.setdefault(_reaction, {})\n    _xlsx = _dataset_entry.setdefault('xlsx', {})\n    for _dataset_id in _STAGE_DATASETS.get(_reaction, []):\n        _xlsx.setdefault(int(_dataset_id), '%s/expdata/%d.xlsx' % (_reaction, int(_dataset_id)))",
        # Upstream carried the cuts in the manifest but never wrote them.
        "for _reaction, _filters in _STAGE_CUTS.items():\n    _DATASET_CONF.setdefault(_reaction, {})['filters'] = _filters",
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
        f"conf['beta version'] = {stage.qcd.get('betaVersion', 1)!r}",
        f"conf['sia version'] = {stage.qcd.get('siaVersion', 1)!r}",
        "for _obsolete in ('sea_type', 'strange off', 's_veto', 'grid-8 path'):\n    conf.pop(_obsolete, None)",
        f"conf['flat par'] = {(stage.params.get('initializationMode') or stage.params.get('initialization')) == 'random_within_bounds'!r}",
        *([f"conf['grid path'] = {stage.qcd.get('grid_path')!r}"] if stage.qcd.get('grid_path') else []),
        f"conf['bootstrap'] = {bool(stage.job.get('bootstrap', False))!r}",
        "conf['resample version'] = 0",
        # Explicit campaign choices override template defaults. Legacy manifests
        # without these fields retain the previous all-enabled behavior.
        *[f"conf[{(obj + ' positivity')!r}] = {value!r}" for obj, value in positivity.items()],
        *([f"conf['jamai prior replicas'] = {_literal(stage.params['prior_replicas'])}"] if stage.params.get('prior_replicas') and prior_reference is None else
          _prior_loader(prior_reference) if prior_reference else ["conf.pop('jamai prior replicas', None)"]),
        f"conf['jamai parameter initialization'] = {stage.params.get('initializationMode') or stage.params.get('initialization', 'default_prior')!r}",
        f"conf['jamai parameter random seed'] = {stage.params.get('randomSeed', None)!r}",
        f"conf['jamai per replica seed'] = {bool(stage.params.get('perReplicaSeed', True))!r}",
        f"conf['jamai base seed'] = {stage.params.get('baseSeed', 120000)!r}",
        f"conf['jamai parameter sectors'] = {_literal(stage.params.get('sectors', []))}",
        "conf['jamai bootstrap seed offset'] = 100000",
        '',
    ]
    return '\n'.join(lines)


def _prior_loader(reference):
    # Standalone stdlib loader: cards also work when FitPack copies them into a
    # replica directory, and analysis readers need no new application imports.
    return [f'_JAMAI_PRIOR_REFERENCE = {_literal(reference)}',
            'import json as _jamai_json', 'import hashlib as _jamai_hashlib',
            'from pathlib import Path as _JamaiPath',
            "_jamai_raw = _JamaiPath(_JAMAI_PRIOR_REFERENCE['path']).read_bytes()",
            "if _jamai_hashlib.sha256(_jamai_raw).hexdigest() != _JAMAI_PRIOR_REFERENCE['file_sha256']:",
            "    raise ValueError('Prior snapshot file integrity failure')",
            '_JAMAI_PRIOR_SNAPSHOT = _jamai_json.loads(_jamai_raw)',
            '_jamai_payload = dict(_JAMAI_PRIOR_SNAPSHOT)',
            "_jamai_payload.pop('sha256', None)",
            '_jamai_encoded = _jamai_json.dumps(',
            '    _jamai_payload, sort_keys=True, ensure_ascii=False,',
            "    allow_nan=False, separators=(',', ':'),",
            ").encode('utf-8')",
            '_jamai_digest = _jamai_hashlib.sha256(_jamai_encoded).hexdigest()',
            "if (_jamai_digest != _JAMAI_PRIOR_REFERENCE['snapshot_sha256']",
            "        or _jamai_digest != _JAMAI_PRIOR_SNAPSHOT.get('sha256')):",
            "    raise ValueError('Prior snapshot integrity failure')",
            "conf['jamai prior replicas'] = _JAMAI_PRIOR_SNAPSHOT"]


def materialize_stage_input(template_path: str | Path, output_path: str | Path, stage: ResolvedStage,
                            fitpack_root: str | Path | None = None) -> str:
    for key in ('betaVersion', 'siaVersion'):
        value = stage.qcd.get(key, 1)
        if type(value) is not int or value not in (0, 1):
            raise ValueError(f'{key} must be integer 0 or 1')
    ff_blocks = {}
    for reaction in ('sidis', 'sia'):
        if reaction not in stage.reactions:
            continue
        if not stage.datasets.get(reaction):
            raise ValueError(f'{reaction}: select at least one dataset')
        cutoff, w2cut = None, 10.0
        if reaction == 'sidis':
            cuts = stage.cuts.get('idis', stage.cuts.get('sidis', []))
            cutoff, w2cut = sidis_dis_cut_values(cuts)
        overrides = {}
        for sector in stage.params.get('sectors', []):
            if sector.get('key') != 'norm': continue
            for parameter in sector.get('parameters', []):
                prefix = 'norm_' + reaction + '_'
                if str(parameter.get('id', '')).startswith(prefix):
                    overrides[str(parameter['id'])[len(prefix):]] = {k: parameter[k] for k in ('value', 'min', 'max', 'fixed')}
        ff_blocks[reaction] = configuration_dataset_block(reaction, stage.datasets.get(reaction, []), cutoff,
            w2cut=w2cut, normalizations=overrides, filters=stage.cuts.get('sia') if reaction == 'sia' else None)
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
    fragments = [f"\nconf.setdefault('datasets', {{}})[{reaction!r}] = {_literal(PROTON_REACTION_DATASETS[reaction])}"
                 for reaction in stage.reactions if reaction in PROTON_REACTION_DATASETS]
    if 'dy' in stage.reactions:
        fragments.append(f"\nconf.setdefault('datasets', {{}})['dy'] = {_literal(DY_DATASET_DEFAULTS)}")
    for reaction, block in ff_blocks.items():
        fragments.append(f"\nconf.setdefault('datasets', {{}})[{reaction!r}] = {_literal(block)}")
    for key, params in ff_params.items():
        fragments.append(f"\nconf.setdefault('params', {{}})[{key!r}] = {_literal(params)}")
        fragments.extend(f"conf[{name!r}] = {value!r}" for name, value in ff_library(key)['settings'].items())
    if ff_objects:
        # The proton template is only a settings scaffold for FF-only SIA.
        # Remove unused template distributions instead of initializing an unrelated PDF.
        keep = set(stage.fit_objects) | set(stage.frozen_objects)
        fragments.append(f"\n_FF_STAGE_OBJECTS = {sorted(keep)!r}")
        fragments.append("for _key in ('pdf', 'pdf-pion', 'ffpion', 'ffkaon', 'ffhadron'):\n    if _key not in _FF_STAGE_OBJECTS:\n        conf.get('params', {}).pop(_key, None)")
    reference = None
    if stage.params.get('prior_replicas'):
        from .prior_replicas import store_snapshot
        reference = store_snapshot(stage.params['prior_replicas'], Path(output_path).parent / 'prior_replicas')
    overrides = _render_stage_override_block(stage, reference)
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
                f"conf['params'][{key!r}] = {_literal(library['params'])}",
            ])
            fragments.extend(f'conf[{name!r}] = {value!r}'
                             for name, value in library['settings'].items())
            fragments.extend(f"conf['datasets'][{reaction!r}] = {_literal(dataset)}"
                             for reaction, dataset in library['datasets'].items()
                             if reaction in stage.reactions)
        if 'pdf-pion' in libraries and 'pdf' not in stage.effective_fit_objects:
            fragments.append(f"conf['params']['pdf'] = {_literal(libraries['pdf-pion']['fixed_companions']['pdf'])}")
        if 'ln' in stage.reactions:
            fragments.append(f"conf['path2idistab'] = {str(Path(root) / 'grids' / 'grids-idis' / 'distab')!r}")
    output_file = Path(copy_stage_input_template(template_path, output_path))
    # Native tools.config.load_config executes each input line separately.
    # Keep multiline human-readable settings in a verified source file, and
    # use only complete single-line statements in the small legacy entry card.
    from .prior_replicas import publish_immutable
    source = ('\n'.join(fragments) + overrides).encode('utf-8')
    source_sha = hashlib.sha256(source).hexdigest()
    settings = publish_immutable(output_file.parent / 'stage_configuration', source_sha + '.py', source)
    with output_file.open('a', encoding='utf-8') as f:
        f.write('\n# Readable generated settings; original template above is retained.\n')
        f.write(f'_JAMAI_SETTINGS_PATH = {str(settings)!r}\n')
        f.write(f'_JAMAI_SETTINGS_SHA256 = {source_sha!r}\n')
        f.write('import hashlib as _jamai_hashlib\n')
        f.write('from pathlib import Path as _JamaiSettingsPath\n')
        f.write('_jamai_settings = _JamaiSettingsPath(_JAMAI_SETTINGS_PATH).read_bytes()\n')
        f.write("if _jamai_hashlib.sha256(_jamai_settings).hexdigest() != _JAMAI_SETTINGS_SHA256: raise ValueError('Campaign settings integrity failure')\n")
        f.write("exec(compile(_jamai_settings, _JAMAI_SETTINGS_PATH, 'exec'), globals())\n")
        # Existing setting/seed/provenance readers inspect scalar assignments
        # in this card. Keep all of them visible, not just bootstrap controls.
        import ast
        settings_text = source.decode('utf-8')
        for node in ast.parse(settings_text).body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Subscript)
                    and isinstance(node.targets[0].value, ast.Name) and node.targets[0].value.id == 'conf'
                    and isinstance(node.targets[0].slice, ast.Constant)):
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    continue
                if type(value) in (str, int, float, bool, type(None)):
                    f.write(ast.get_source_segment(settings_text, node) + '\n')
    return str(output_file)


def read_stage_input_preview(output_path: str | Path, max_lines: int = 24) -> str:
    # The appended override block is at the end of the file, so the preview
    # returns the tail rather than the first lines of the legacy template.
    lines = Path(output_path).read_text(encoding='utf-8').splitlines()
    if len(lines) <= max_lines:
        return '\n'.join(lines)
    return '\n'.join(lines[-max_lines:])
