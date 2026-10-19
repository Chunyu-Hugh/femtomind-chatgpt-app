"""Evaluate existing FitPack charged FFs without changing their physics.

FitPack's ``ffhadron`` (sum2) object already includes pion and kaon FFs.
The residual is obtained by subtraction at the same z and Q2, per replica.
"""

FF_FLAVORS = ('g', 'u', 'd', 's', 'ub', 'db', 'sb', 'c', 'b', 'up', 'dp', 'sp')
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
    requested = list(flavors)
    combinations = {'up': ('u', 'ub'), 'dp': ('d', 'db'), 'sp': ('s', 'sb')}
    flavors = list(dict.fromkeys(base for flavor in requested for base in combinations.get(flavor, (flavor,))))
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
    return {key: {flavor: ([a + b for a, b in zip(rows[combinations[flavor][0]], rows[combinations[flavor][1]])] if flavor in combinations else rows[flavor]) for flavor in requested} for key, rows in curves.items()}


FF_HADRONS = {
    'pion': ('ffpion', r'\pi^{+}', 'red'),
    'kaon': ('ffkaon', r'K^{+}', '#1f77b4'),
    'hadron': ('ffunidentified', r'h^{+}', '#2ca02c'),
    'rhadron': ('ffhadron', r'\delta h^{+}', 'darkorange'),
}
FF_FLAVOR_MATH = {
    'g': 'g', 'u': 'u', 'd': 'd', 's': 's',
    'ub': r'\bar{u}', 'db': r'\bar{d}', 'sb': r'\bar{s}',
    'c': 'c', 'b': 'b', 'up': r'u^{+}', 'dp': r'd^{+}', 'sp': r's^{+}',
}
FF_PREVIEW_DPI = 150


def ff_available_hadrons(objects):
    """Only actual component objects permit residual/total sum2 plots."""
    keys = set(objects)
    available = [hadron for hadron, key in (('pion', 'ffpion'), ('kaon', 'ffkaon')) if key in keys]
    if {'ffpion', 'ffkaon', 'ffhadron'} <= keys:
        available.extend(['hadron', 'rhadron'])
    return available


def validate_ff_display(z_min=0.02, z_max=0.9, modes='combined', hadrons='',
                        band_color='red', band_alpha=0.9):
    import math
    if any(isinstance(value, bool) for value in (z_min, z_max, band_alpha)):
        raise ValueError('FF display values must be finite numbers')
    try:
        z_min, z_max, band_alpha = map(float, (z_min, z_max, band_alpha))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('FF display values must be finite numbers') from exc
    if not all(map(math.isfinite, (z_min, z_max, band_alpha))) or not 0 < z_min < z_max < 1:
        raise ValueError('Require 0 < z minimum < z maximum < 1; endpoints cannot be evaluated')
    if not 0 <= band_alpha <= 1 or not isinstance(band_color, str) or not band_color.strip() or len(band_color) > 64:
        raise ValueError('Require opacity from 0 to 1 and a named or hexadecimal band color')
    def choices(raw, allowed, optional=False):
        if not isinstance(raw, str):
            raise ValueError('FF modes and hadrons must be comma-separated selections')
        result = list(dict.fromkeys(part.strip() for part in raw.split(',') if part.strip()))
        if (not result and not optional) or any(value not in allowed for value in result):
            raise ValueError('Select from: ' + ', '.join(allowed))
        return result
    return dict(z_min=z_min, z_max=z_max, modes=choices(modes, ('combined', 'individual')),
                hadrons=choices(hadrons, FF_HADRONS, optional=True),
                band_color=band_color.strip(), band_alpha=band_alpha)


def ff_bands(curves, hadrons, flavors, interval, interval_function, configured=False):
    """Retain complete replica ensembles; never take nanpercentiles or floor values."""
    import numpy as np
    bands = {}
    for hadron in hadrons:
        key = FF_HADRONS[hadron][0]
        if key not in curves:
            raise ValueError('Unavailable hadron category: ' + hadron)
        bands[hadron] = {}
        for flavor in flavors:
            values = np.asarray(curves[key][flavor], dtype=float)
            if values.ndim != 2 or not np.isfinite(values).all() or values.shape[0] < (1 if configured else 2):
                raise ValueError('At least two finite fitted replicas are required for FF bands')
            low, high = (values[0].copy(), values[0].copy()) if configured else interval_function(values, interval)
            bands[hadron][flavor] = dict(low=low, high=high, mean=values.mean(axis=0))
    return bands


def _ff_literal(text):
    replacements = {'\\': r'\textbackslash{}', '&': r'\&', '%': r'\%', '$': r'\$',
                    '#': r'\#', '_': r'\_', '{': r'\{', '}': r'\}',
                    '~': r'\textasciitilde{}', '^': r'\textasciicircum{}'}
    return ''.join(replacements.get(char, char) for char in str(text))


def _ff_regular(text):
    return r'{\normalfont\mdseries\unboldmath$' + text + '$}'


def _ff_place(fig, axes, artists, occupied, preferred=(0.35, 0.23), excluded=()):
    """Place measured text/legend rectangles in free log-plot space."""
    import numpy as np
    renderer = fig.canvas.get_renderer()
    candidates = []
    anchor_offsets = {}
    for i, ax in enumerate(axes):
        if i in excluded:
            continue
        artist = artists[i]
        if hasattr(artist, 'set_position'):
            artist.set_position((0.5, 0.5))
        else:
            artist.set_bbox_to_anchor((.5, .5), transform=ax.transAxes)
        box = artist.get_window_extent(renderer)
        width, height = box.width / ax.bbox.width, box.height / ax.bbox.height
        anchor = ax.transAxes.transform((.5, .5))
        offset = ((box.x0 + box.width / 2 - anchor[0]) / ax.bbox.width,
                  (box.y0 + box.height / 2 - anchor[1]) / ax.bbox.height)
        anchor_offsets[i] = offset
        paths = [collection.get_transform().transform_path(path)
                 for collection in ax.collections for path in collection.get_paths()]
        lines = [line.get_transform().transform_path(line.get_path()) for line in ax.lines]
        for tx in np.linspace(width / 2 + .025, 1 - width / 2 - .025, 15):
            for ty in np.linspace(height / 2 + .025, 1 - height / 2 - .025, 15):
                if width >= .95 or height >= .95:
                    continue
                if hasattr(artist, 'set_position'):
                    artist.set_position((tx, ty))
                else:
                    artist.set_bbox_to_anchor((tx-offset[0], ty-offset[1]), transform=ax.transAxes)
                box = artist.get_window_extent(renderer).padded(3 * fig.dpi / 72)
                if not ax.bbox.contains(box.x0, box.y0) or not ax.bbox.contains(box.x1, box.y1):
                    continue
                if any(box.overlaps(old) for old in occupied[i]):
                    continue
                if any(path.intersects_bbox(box, filled=True) for path in paths) or any(
                        path.intersects_bbox(box, filled=False) for path in lines):
                    continue
                candidates.append(((tx-preferred[0])**2 + (ty-preferred[1])**2 + .01*i, i, tx, ty))
    if not candidates:
        for artist in artists:
            artist.remove()
        raise ValueError('No clear in-panel space for FF text at the approved font sizes; select fewer flavors or a different z range')
    _, i, tx, ty = min(candidates)
    for j, artist in enumerate(artists):
        if j != i:
            artist.remove()
    artist = artists[i]
    if hasattr(artist, 'set_position'):
        artist.set_position((tx, ty))
    else:
        offset = anchor_offsets[i]
        artist.set_bbox_to_anchor((tx-offset[0], ty-offset[1]), transform=axes[i].transAxes)
    occupied[i].append(artist.get_window_extent(renderer).padded(3 * fig.dpi / 72))
    return i, artist


def draw_ff_figures(zs, bands, flavors, hadrons, modes, q2, campaign_name, interval,
                    *, typography, band_color='red', band_alpha=.9, configured=False):
    """Three-column FF art, independent of evaluation and future campaign overlays."""
    import numpy as np
    from matplotlib import pyplot as plt, rc_context
    from matplotlib.patches import Patch
    from matplotlib.ticker import FixedLocator, FixedFormatter, MultipleLocator, LogLocator, NullFormatter, MaxNLocator, AutoMinorLocator
    style, font_family = typography
    if style.get('text.usetex') is not True:
        raise ValueError('FF figures require the approved real-LaTeX typography')
    figures = []
    tasks = [('combined', list(hadrons))] if 'combined' in modes else []
    if 'individual' in modes:
        tasks.extend(('individual', [hadron]) for hadron in hadrons)
    for mode, selected in tasks:
        with rc_context(style):
            rows = (len(flavors) + 2) // 3
            fig, grid = plt.subplots(rows, 3, figsize=(18, rows * 4 + 1.0), squeeze=False)
            fig.subplots_adjust(left=.075, right=.985, bottom=1.0/fig.get_figheight(),
                                top=1-.15/fig.get_figheight(), hspace=0, wspace=0)
            axes = list(grid.flat)[:len(flavors)]
            for ax in list(grid.flat)[len(flavors):]:
                ax.set_visible(False)
            layout, limits_info = [], []
            for i, (ax, flavor) in enumerate(zip(axes, flavors)):
                positive = np.concatenate([bands[h][flavor]['high'][bands[h][flavor]['high'] > 0] for h in selected])
                # Display bounds only: retain the complete numerical bands.
                ax.set_yscale('log', nonpositive='clip'); ax.set_ylim(bottom=.0005, top=3)
                ax.set_xlim(float(zs[0]), float(zs[-1]))
                for hadron in sorted(selected, key=lambda h: 0 if h == 'hadron' else 1):
                    row = bands[hadron][flavor]
                    color = FF_HADRONS[hadron][2] if mode == 'combined' else band_color
                    valid = np.isfinite(row['low']) & np.isfinite(row['high']) & (row['high'] > 0)
                    if configured:
                        # Explicit deterministic preview, never a fitted-replica curve or interval.
                        ax.plot(zs, np.ma.masked_less_equal(row['mean'], 0), color=color, lw=1.8)
                    else:
                        ax.fill_between(zs, row['low'], row['high'], where=valid,
                                        facecolor=color, edgecolor=color, linewidth=.7, alpha=band_alpha)
                    partial = int(np.count_nonzero(valid & (row['low'] <= 0)))
                    omitted = int(np.count_nonzero(~valid))
                    limits_info.append(dict(hadron=hadron, flavor=flavor, nonpositive_lower=partial,
                                            nonpositive_upper=omitted, display_only=True))
                bottom = i // 3 == rows - 1
                zticks = [x for x in [.2,.4,.6,.8] if zs[0] <= x <= zs[-1]]
                if len(zticks) < 2:
                    zticks = [x for x in MaxNLocator(nbins=4).tick_values(zs[0],zs[-1]) if zs[0] <= x <= zs[-1]]
                    ax.xaxis.set_minor_locator(AutoMinorLocator(2))
                else:
                    ax.xaxis.set_minor_locator(MultipleLocator(.1))
                ax.xaxis.set_major_locator(FixedLocator(zticks))
                ax.xaxis.set_major_formatter(FixedFormatter([_ff_regular('%g' % x) for x in zticks]))
                yticks = [.001, .01, .1, 1]
                ax.yaxis.set_major_locator(FixedLocator(yticks))
                ax.yaxis.set_major_formatter(FixedFormatter([_ff_regular('%g' % y) for y in yticks]))
                ax.yaxis.set_minor_locator(LogLocator(base=10, subs=np.arange(2, 10)))
                ax.yaxis.set_minor_formatter(NullFormatter())
                ax.tick_params(which='both', direction='in', top=True, right=True,
                               labelleft=i % 3 == 0, labelright=False, labelbottom=bottom, labelsize=25)
                ax.tick_params(which='major', length=8, pad=9)
                ax.tick_params(which='minor', length=4)
                for spine in ax.spines.values(): spine.set_linewidth(2)
                ax.set_rasterized(True)
                if bottom: ax.set_xlabel(r'\boldmath$z$', fontsize=40)
                superscript = '^{%s}' % FF_HADRONS[selected[0]][1] if mode == 'individual' else ''
                label = r'\boldmath$zD_{%s}%s$' % (FF_FLAVOR_MATH[flavor], superscript)
                flavor_text = ax.text(.8, .8, label, transform=ax.transAxes, fontsize=45,
                                      ha='center', va='center', clip_on=True)
                _ff_place(fig, [ax], [flavor_text], {0:[]}, preferred=(.8,.8))
                if not positive.size:
                    ax.text(.5, .45, 'No positive FF interval', transform=ax.transAxes,
                            ha='center', fontsize=18, fontweight='normal')
                layout.append(dict(flavor=flavor, bottom_labels=bottom, y_numeric_labels=i%3==0,
                                   ylim=list(ax.get_ylim())))
            renderer = fig.canvas.get_renderer()
            occupied = {i:[text.get_window_extent(renderer).padded(4*fig.dpi/72) for text in ax.texts]
                        for i, ax in enumerate(axes)}
            annotation = _ff_regular(r'Q^{2} = %g\,\mathrm{GeV}^{2}' % q2) + '\n' + (
                _ff_regular(r'%g\%%\;\mathrm{C.I.}' % interval) if not configured else 'Configured values')
            texts = [ax.text(.5, .5, annotation, transform=ax.transAxes, fontsize=30,
                             fontweight='normal', ha='center', va='center', linespacing=1.15) for ax in axes]
            annotation_panel, _ = _ff_place(fig, axes, texts, occupied)
            if mode == 'combined':
                handles = [Patch(facecolor=FF_HADRONS[h][2], edgecolor=FF_HADRONS[h][2],
                                 alpha=band_alpha, label=r'\boldmath$%s$' % FF_HADRONS[h][1]) for h in selected]
            else:
                handles = [Patch(facecolor=band_color, edgecolor=band_color, alpha=band_alpha,
                                 label=_ff_literal(campaign_name))]
            legend_axes = list(axes)
            if len(axes) == 1:
                # Keep single-flavor requests usable without sharing its text panel.
                spare = list(grid.flat)[1]
                spare.set_visible(True)
                spare.set_axis_off()
                legend_axes.append(spare)
                occupied[1] = []
            legends = [ax.legend(handles=handles, loc='lower left', bbox_to_anchor=(0,0),
                                  frameon=False, ncol=2 if len(handles)>2 else 1,
                                  fontsize=28, handlelength=1, columnspacing=.3, handletextpad=.4)
                       for ax in legend_axes]
            legend_panel, _ = _ff_place(fig, legend_axes, legends, occupied,
                                       preferred=(0,0), excluded=(annotation_panel,))
            fig.canvas.draw()
            save = fig.savefig
            def styled_save(*args, _save=save, **kwargs):
                with rc_context(style): return _save(*args, **kwargs)
            fig.savefig = styled_save
            figures.append((fig, dict(mode=mode, hadrons=selected, layout=layout,
                                      annotation_panel=annotation_panel, legend_panel=legend_panel,
                                      log_limitations=limits_info, text_rendering='latex',
                                      font_family=font_family, dpi=FF_PREVIEW_DPI, q2=q2,
                                      z_min=float(zs[0]), z_max=float(zs[-1]),
                                      credible_interval=interval, configured=configured)))
    return figures
