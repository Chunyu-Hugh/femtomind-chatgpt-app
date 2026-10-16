"""Evaluate existing FitPack charged FFs without changing their physics.

FitPack's ``ffhadron`` (sum2) object already includes pion and kaon FFs.
The residual is obtained by subtraction at the same z and Q2, per replica.
"""

FF_FLAVORS = ('g', 'u', 'd', 'ub', 'db', 's', 'sb', 'c', 'b')
FF_PLOT_OBJECTS = ('ffpion', 'ffkaon', 'ffhadron', 'ffunidentified')
FF_PLOT_LABELS = {
    'ffpion': 'Charged pion FFs', 'ffkaon': 'Charged kaon FFs',
    'ffhadron': 'Residual charged hadron FFs',
    'ffunidentified': 'Unidentified charged hadron FFs',
}


def evaluate_ff_curves(conf, zs, q2, flavors):
    """Return zD curves in the native positive charged-hadron convention.

    No extra factor of two is applied: charge sums belong to the reaction
    calculation. Charm/anticharm and bottom/antibottom are not separate choices.
    """
    import math
    if not flavors or any(flavor not in FF_FLAVORS for flavor in flavors):
        raise ValueError('Unsupported FF flavor selection')
    if not math.isfinite(float(q2)) or float(q2) <= 0:
        raise ValueError('FF Q2 must be positive and finite')
    zs = [float(z) for z in zs]
    if not zs or any(not math.isfinite(z) or not 0 < z < 1 for z in zs):
        raise ValueError('FF z must be finite and strictly between zero and one')
    curves = {}
    for key in ('ffpion', 'ffkaon'):
        if key in conf:
            curves[key] = {flavor: [float(conf[key].get_xF(z, q2, flavor)) for z in zs]
                           for flavor in flavors}
    if 'ffhadron' in conf:
        if not all(key in curves for key in ('ffpion', 'ffkaon')):
            raise ValueError('FitPack sum2 requires pion and kaon FF objects')
        total = {flavor: [float(conf['ffhadron'].get_xF(z, q2, flavor)) for z in zs]
                 for flavor in flavors}
        curves['ffhadron'] = {
            flavor: [value - pion - kaon for value, pion, kaon in
                     zip(total[flavor], curves['ffpion'][flavor], curves['ffkaon'][flavor])]
            for flavor in flavors}
        # Sum the components inside this replica before computing any interval.
        curves['ffunidentified'] = {
            flavor: [pion + kaon + residual for pion, kaon, residual in
                     zip(curves['ffpion'][flavor], curves['ffkaon'][flavor], curves['ffhadron'][flavor])]
            for flavor in flavors}
    if not curves:
        raise ValueError('This configuration has no FF objects')
    if any(not math.isfinite(value) for values in curves.values()
           for row in values.values() for value in row):
        raise ValueError('FitPack returned a nonfinite FF curve')
    return curves
