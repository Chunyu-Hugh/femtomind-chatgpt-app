from __future__ import annotations


# JAM reactions imply which non-perturbative objects must be present in a stage.
# For example, DIS and Drell-Yan only need PDFs, while SIDIS also needs
# fragmentation functions. The stage resolver uses this table to reject
# impossible campaign manifests before a job is submitted.
REACTION_OBJECT_MAP: dict[str, tuple[str, ...]] = {
    'idis': ('pdf',),
    'dy': ('pdf',),
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


def get_required_objects(reactions: list[str]) -> list[str]:
    # Convert reactions into the union of all FitPack object families the stage
    # must either fit or freeze.
    required = []
    for reaction in normalize_reactions(reactions):
        if reaction not in REACTION_OBJECT_MAP:
            raise ValueError(f"Unsupported reaction '{reaction}'")
        for obj in REACTION_OBJECT_MAP[reaction]:
            if obj not in required:
                required.append(obj)
    return required
