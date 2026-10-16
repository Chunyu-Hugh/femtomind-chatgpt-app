from __future__ import annotations


# JAM reactions imply which non-perturbative objects must be present in a stage.
# For example, DIS and Drell-Yan only need PDFs, while SIDIS also needs
# fragmentation functions. The stage resolver uses this table to reject
# impossible campaign manifests before a job is submitted.
REACTION_OBJECT_MAP: dict[str, tuple[str, ...]] = {
    'idis': ('pdf',),
    'dy': ('pdf',),
    'dy-pion': ('pdf-pion',),
    'ln': ('pdf-pion', 'p->pi,n', 'pdf'),
    'zrap': ('pdf',),
    'wasym': ('pdf',),
    'wzrv': ('pdf',),
    'jet': ('pdf',),
    'wc': ('pdf',),
    'pidis': ('ppdf',),
    'sia': ('ffpion', 'ffkaon', 'ffhadron'),
    'sidis': ('pdf', 'ffpion', 'ffkaon', 'ffhadron'),
}


def normalize_reactions(reactions: list[str]) -> list[str]:
    # Normalize user/YAML input without reordering unique reactions.
    normalized = []
    for reaction in reactions:
        value = reaction.strip().lower()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def get_required_objects(reactions: list[str], datasets=None) -> list[str]:
    # Convert reactions into the union of all FitPack object families the stage
    # must either fit or freeze.
    required = []
    for reaction in normalize_reactions(reactions):
        if reaction not in REACTION_OBJECT_MAP:
            raise ValueError(f"Unsupported reaction '{reaction}'")
        objects = REACTION_OBJECT_MAP[reaction]
        if reaction in ('sidis', 'sia') and datasets is not None:
            selection = datasets.get(reaction, [])
            if isinstance(selection, dict):
                selection = selection.get('selected', [])
            if selection:
                # These authoritative catalogs use 1xxx=pion, 2xxx=kaon;
                # 3xxx SIDIS / 4xxx SIA are unidentified charged hadrons.
                objects = ['pdf'] if reaction == 'sidis' else []
                for dataset_id in selection:
                    prefix = str(dataset_id)[0]
                    fragment = ('ffpion',) if prefix == '1' else ('ffkaon',) if prefix == '2' else (
                        'ffpion', 'ffkaon', 'ffhadron')
                    objects.extend(fragment)
        for obj in objects:
            if obj not in required:
                required.append(obj)
    return required


def resolve_object_roles(fit_objects, reactions, frozen_objects=(), datasets=None):
    fitted = list(dict.fromkeys(fit_objects))
    frozen = list(dict.fromkeys(frozen_objects))
    overlap = set(fitted) & set(frozen)
    if overlap:
        raise ValueError(f'Objects cannot be both fitted and frozen: {sorted(overlap)}')
    required = get_required_objects(reactions, datasets)
    if 'pdf-pion' in fitted and 'ln' in normalize_reactions(reactions):
        if 'p->pi,n' not in fitted and 'p->pi,n' not in frozen:
            fitted.append('p->pi,n')
        if 'pdf' not in fitted and 'pdf' not in frozen:
            frozen.append('pdf')
    if any(reaction in ('sidis', 'sia') for reaction in normalize_reactions(reactions)):
        unused = [key for key in fitted if key in ('ffpion', 'ffkaon', 'ffhadron') and key not in required]
        if unused:
            raise ValueError(f'Selected datasets do not constrain these fitted FF groups: {unused}')
        # Required distributions that are not selected for fitting use explicit,
        # frozen library defaults. An SIA-only campaign never needs a PDF.
        frozen.extend(key for key in required if key not in fitted and key not in frozen)
    missing = [key for key in required if key not in fitted and key not in frozen]
    if missing:
        raise ValueError(f'Missing required fit coverage for objects: {missing}')
    return fitted, frozen
