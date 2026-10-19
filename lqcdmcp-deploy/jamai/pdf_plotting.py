"""Proton PDF display helpers; FitPack remains the source of every xPDF.

Transform each replica before taking the existing equal-tailed intervals.
Pion PDF and fragmentation-function plots do not use this module.
"""

PROTON_FLAVORS = ('u', 'd', 'g', 's', 'ub', 'db', 'sb',
                 'uv', 'dv', 'd/u', 'db+ub', 'db-ub', 'sp', 'sm', 'Rs', 'up', 'dp')
PDF_LABELS = {
    'u': r'$xu$', 'd': r'$xd$', 'g': r'$xg$', 's': r'$xs$',
    'ub': r'$x\bar{u}$', 'db': r'$x\bar{d}$', 'sb': r'$x\bar{s}$',
    'd/u': r'$d/u$', 'db+ub': r'$x(\bar{d}+\bar{u})$',
    'db-ub': r'$x(\bar{d}-\bar{u})$', 'sp': r'$xs^{+}$',
    'sm': r'$xs^{-}$', 'Rs': r'$R_s$', 'up': r'$xu^{+}$', 'dp': r'$xd^{+}$',
    'uv': r'$xu_v$', 'dv': r'$xd_v$',
}
PDF_STYLE = {
    'font.family': 'serif', 'font.serif': ['Computer Modern Roman'],
    'text.usetex': True, 'text.latex.preamble': r'\renewcommand{\rmdefault}{ptm}',
    'axes.unicode_minus': False,
}


def _pdf_flavor_label(flavor):
    """The single required label style: LaTeX bold math and short bars."""
    return r'\boldmath' + PDF_LABELS[flavor]


def _pdf_regular_math(text):
    """Reset TeX weight locally, using the same regular math face for all numbers."""
    return r'{\normalfont\mdseries\unboldmath$' + text + '$}'


def _pdf_y_ticks(ax, limits):
    """At most two decimals; both signs use the same explicit regular math face."""
    import numpy as np
    from matplotlib.ticker import MaxNLocator, FixedLocator, FuncFormatter
    locator = MaxNLocator(nbins=4, steps=[1, 2, 5, 10], prune='both', min_n_ticks=3)
    ticks = locator.tick_values(*limits)
    fine = not np.allclose(ticks * 100, np.round(ticks * 100), rtol=0, atol=1e-7)
    magnitude = max(abs(limits[0]), abs(limits[1]), np.finfo(float).tiny)
    exponent = int(np.floor(np.log10(magnitude))) if fine or magnitude >= 1e4 else 0
    scale = 10.0 ** exponent
    scaled = ticks / scale
    precision = next((n for n in (0, 1, 2) if np.allclose(
        scaled * 10**n, np.round(scaled * 10**n), rtol=0, atol=1e-7)), 2)
    def format_tick(value, position):
        value /= scale
        if abs(value) < 0.5 * 10**(-precision): value = 0.0
        text = ('%.*f' % (precision, value))
        return _pdf_regular_math(text)
    class ConciseFormatter(FuncFormatter):
        def get_offset(self):
            return r'$\times10^{%d}$' % exponent if exponent else ''
    ax.yaxis.set_major_locator(FixedLocator(ticks))
    ax.yaxis.set_major_formatter(ConciseFormatter(format_tick))


def _pdf_typography():
    """Require the validated Times-text/CM-math LaTeX renderer."""
    import shutil
    from matplotlib import pyplot as plt, rc_context
    missing = [tool for tool in ('latex', 'dvipng') if not shutil.which(tool)]
    if missing:
        raise RuntimeError('Proton PDF plots require real LaTeX with Times text and Computer Modern math; '
                           'missing executable(s): ' + ', '.join(missing) +
                           '. Use a plotting runtime with these tools and the required TeX fonts. '
                           'No alternate text renderer is permitted.')
    # Classic latex cannot load the Windows TNR TTF directly. ptm supplies
    # Times text while leaving Computer Modern math and true boldmath intact.
    style = dict(PDF_STYLE)
    probe = None
    try:
        with rc_context(style):
            probe = plt.figure(figsize=(2, 1))
            probe.text(0.05, 0.5, r'\boldmath$xu,\ d/u,\ xs^{+},\ R_s$')
            probe.text(0.05, 0.1, r'$Q^2=10\,\mathrm{GeV}^2$; 95\% C.I.')
            probe.canvas.draw()
        return style, 'Times (TeX ptm), Computer Modern math'
    except (RuntimeError, OSError) as exc:
        raise RuntimeError('Required proton PDF LaTeX rendering failed. Check latex, dvipng, '
                           'and the Times/Computer Modern TeX fonts in the plotting runtime. '
                           'No alternate text renderer is permitted.') from exc
    finally:
        if probe is not None: plt.close(probe)


def _tex_literal(text):
    """Escape literal campaign names and non-math annotations in TeX mode."""
    replacements = {'\\': r'\textbackslash{}', '&': r'\&', '%': r'\%', '$': r'\$',
                    '#': r'\#', '_': r'\_', '{': r'\{', '}': r'\}',
                    '~': r'\textasciitilde{}', '^': r'\textasciicircum{}'}
    return ''.join(replacements.get(char, char) for char in str(text))


def validate_pdf_display(x_min=0.01, x_max=1.0, band_color='red', band_alpha=0.95):
    import math
    try:
        if any(isinstance(v, bool) for v in (x_min, x_max, band_alpha)):
            raise ValueError()
        x_min, x_max, band_alpha = map(float, (x_min, x_max, band_alpha))
        if not all(map(math.isfinite, (x_min, x_max, band_alpha))):
            raise ValueError()
        if not 0 < x_min < x_max <= 1 or not 0 <= band_alpha <= 1:
            raise ValueError()
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError('Require 0 < x_min < x_max <= 1 and 0 <= band_alpha <= 1 (finite numbers).') from exc
    if not isinstance(band_color, str) or not band_color.strip() or len(band_color) > 64:
        raise ValueError('Band color must be a Matplotlib named color or hexadecimal color.')
    return dict(x_min=x_min, x_max=x_max, band_color=band_color.strip(), band_alpha=band_alpha)


def proton_x_grid(x_min, x_max):
    import numpy as np
    # Mellin inversion is evaluated in the open physical domain. x=1 remains
    # the displayed boundary, not an invented PDF value or a 0/0 ratio.
    xs = np.geomspace(x_min, x_max, 241)
    return xs[xs < 1.0]


def evaluate_proton_curves(pdf, xs, q2, flavors, display_breaks=None):
    """Native u/d are full quarks; their valence parts are u-ub and d-db.

    All base values already include x. Thus sums retain x and ratios cancel
    it. Numerically unresolved denominators produce explicit gaps, never zero.
    """
    import numpy as np
    needed = {
        'uv': ('u', 'ub'), 'dv': ('d', 'db'), 'd/u': ('d', 'u'), 'db+ub': ('db', 'ub'), 'db-ub': ('db', 'ub'),
        'sp': ('s', 'sb'), 'sm': ('s', 'sb'), 'Rs': ('s', 'sb', 'ub', 'db'),
        'up': ('u', 'ub'), 'dp': ('d', 'db'),
    }
    base = {}
    for flavor in flavors:
        if flavor not in PROTON_FLAVORS:
            raise ValueError('Unsupported proton PDF flavor: ' + flavor)
        for key in needed.get(flavor, (flavor,)):
            if key not in base:
                base[key] = np.asarray([pdf.get_xF(float(x), q2, key) for x in xs], dtype=float)
                if base[key].shape != np.shape(xs) or not np.isfinite(base[key]).all():
                    raise ValueError('Invalid native FitPack xPDF for ' + key)

    def ratio(numerator, denominator, scale):
        # Pointwise cancellation tolerance, independent of the display range.
        # A small but well-conditioned high-x PDF is not a reason to cut data.
        threshold = np.maximum(64 * np.finfo(float).eps * scale, np.finfo(float).tiny)
        good = np.isfinite(denominator) & (np.abs(denominator) > threshold)
        result = np.full_like(denominator, np.nan)
        with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
            np.divide(numerator, denominator, out=result, where=good)
        result[~np.isfinite(result)] = np.nan
        return result

    if display_breaks is not None:
        # Connectivity metadata only: do not change ratio samples or intervals.
        for flavor in flavors:
            if flavor == 'd/u':
                denominator = base['u']
                breaks = np.zeros(len(xs), dtype=bool)
                breaks[1:] = ((np.signbit(denominator[1:]) != np.signbit(denominator[:-1]))
                              | (denominator[1:] == 0) | (denominator[:-1] == 0))
                display_breaks[flavor] = breaks

    curves = {}
    for flavor in flavors:
        if flavor == 'd/u': curves[flavor] = ratio(base['d'], base['u'], np.abs(base['u']))
        elif flavor == 'Rs':
            curves[flavor] = ratio(base['s'] + base['sb'], base['ub'] + base['db'],
                                   np.abs(base['ub']) + np.abs(base['db']))
        elif flavor in ('db+ub', 'sp', 'up', 'dp'):
            a, b = needed[flavor]; curves[flavor] = base[a] + base[b]
        elif flavor in ('uv', 'dv', 'db-ub', 'sm'):
            a, b = needed[flavor]; curves[flavor] = base[a] - base[b]
        else: curves[flavor] = base[flavor]
        if flavor not in ('d/u', 'Rs') and not np.isfinite(curves[flavor]).all():
            raise ValueError('Nonfinite derived xPDF for ' + flavor)
    return curves


def proton_bands(curves, interval, interval_function):
    """Use the same replicas at each retained x; never use nanpercentile.

    If any replica has an unresolved ratio, omit that x for the whole band
    and report it. This avoids silently changing the ensemble at each point.
    """
    import numpy as np
    bands = {}
    for flavor, values in curves.items():
        arr = np.asarray(values, dtype=float)
        if arr.ndim != 2 or arr.shape[0] < 2:
            raise ValueError('At least two valid fitted replicas are required for a proton PDF band.')
        good = np.isfinite(arr).all(axis=0)
        if not good.any():
            raise ValueError('No numerically resolved points for ' + flavor)
        low, high = np.full(arr.shape[1], np.nan), np.full(arr.shape[1], np.nan)
        low[good], high[good] = interval_function(arr[:, good], interval)
        mean = np.full(arr.shape[1], np.nan); mean[good] = arr[:, good].mean(axis=0)
        bands[flavor] = dict(low=low, high=high, mean=mean, valid=good)
    return bands


def proton_display_limits(xs, band, flavor, x_min, x_max):
    """Estimate display bounds only; never modify the band or its interval.

    Isolated excursions are replaced by local medians in this private limit
    estimate. Rs uses a robust body summary in the visible x<=0.4 region,
    when available. These are display heuristics, not replica percentiles.
    """
    import numpy as np
    visible = band['valid'] & (xs >= x_min) & (xs <= x_max)
    reference = visible.copy()
    if flavor == 'Rs':
        body = visible & (xs >= 0.001) & (xs <= 0.4)
        if body.sum() >= 8: reference = body
    low = np.array(band['low'][reference], copy=True)
    high = np.array(band['high'][reference], copy=True)
    if not low.size:
        raise ValueError('No visible band points for ' + flavor)
    suppressed = 0
    if low.size >= 8:
        half = min(10, max(2, int(np.ceil(low.size * 0.025))))
        all_edges = np.r_[low, high]
        q10, q90 = np.percentile(all_edges, [10, 90])
        scale = max(q90 - q10, float(np.median(np.abs(all_edges))) * 0.05, np.finfo(float).tiny)
        for edge in (low, high):
            smooth = np.array([np.median(edge[max(0, i-half):min(edge.size, i+half+1)])
                               for i in range(edge.size)])
            extreme = np.abs(edge - smooth) > 3 * scale
            suppressed += int(extreme.sum())
            edge[extreme] = smooth[extreme]
    if flavor == 'Rs' and low.size >= 8:
        # The upper quartile describes the main envelope without its rising
        # denominator-driven tail. A broad elevated body still raises the range.
        lo, hi = float(np.percentile(low, 10)), float(np.percentile(high, 75))
    else:
        lo, hi = float(low.min()), float(high.max())
    if flavor not in ('d/u', 'Rs'):
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    elif lo >= 0: lo = 0.0
    span = hi - lo
    pad = 0.12 * (span if span > 0 else max(abs(hi), 1e-6))
    lower, upper = lo - pad, hi + pad
    if flavor == 'Rs' and lo >= 0:
        lower = 0.0
        # Pleasant ratio ticks, with an adaptive ceiling (not a fixed 1.5 cap).
        step = 0.5 * 10 ** np.floor(np.log10(max(upper, 1e-12)))
        upper = float(np.ceil(upper / step) * step)
    cropped = visible & ((band['low'] < lower) | (band['high'] > upper))
    return (lower, upper), {
        'ylim_method': 'robust_Rs_body' if flavor == 'Rs' and low.size >= 8 else 'local_spike_resistant',
        'ylim_reference_range': [float(xs[reference].min()), float(xs[reference].max())],
        'ylim_suppressed_edge_samples': suppressed,
        'display_cropped_points': int(cropped.sum()),
    }


def _pdf_display_paths(band, limits):
    """Break drawing paths at gaps/poles, leaving every stored band value intact."""
    import numpy as np
    low, high, mean = [np.array(band[key], dtype=float, copy=True) for key in ('low', 'high', 'mean')]
    lo, hi = limits
    valid = np.asarray(band['valid']) & np.isfinite(low) & np.isfinite(high)
    visible = valid & (high >= lo) & (low <= hi)
    low[~visible] = np.nan
    high[~visible] = np.nan
    # A dashed mean must end before a clipped excursion, rather than drawing
    # a near-vertical segment from below the frame to above it (or back).
    mean[~valid | ~np.isfinite(mean) | (mean < lo) | (mean > hi)] = np.nan
    return low, high, mean


def draw_proton_figure(xs, bands, flavors, q2, campaign_name, interval_label,
                       x_min=0.01, x_max=1.0, band_color='red', band_alpha=0.95,
                       mean_overlay=False):
    from matplotlib import rc_context
    style, font_family = _pdf_typography()
    with rc_context(style):
        fig, layout = _draw_proton_figure(xs, bands, flavors, q2, campaign_name, interval_label,
                                         x_min, x_max, band_color, band_alpha, mean_overlay)
        # Materialize tick text while the figure's scoped typography is active.
        fig.canvas.draw()
    for item in layout:
        item.update(text_rendering='latex', font_family=font_family)
    # TeX also consults rcParams at export time (often at a different DPI).
    # Keep this figure's save scoped, without altering another plot's fonts.
    save = fig.savefig
    def save_with_style(*args, **kwargs):
        with rc_context(style):
            return save(*args, **kwargs)
    fig.savefig = save_with_style
    return fig, layout


def _draw_proton_figure(xs, bands, flavors, q2, campaign_name, interval_label,
                        x_min, x_max, band_color, band_alpha, mean_overlay):
    """One two-column figure. The band mapping separates evaluation from art."""
    import numpy as np
    from matplotlib import pyplot as plt
    from matplotlib.patches import Patch
    from matplotlib.transforms import ScaledTranslation
    from matplotlib.ticker import AutoMinorLocator, LogLocator, FixedLocator, FixedFormatter, NullFormatter
    rows = (len(flavors) + 1) // 2
    fig, axes = plt.subplots(rows, 2, figsize=(14.0, 4.0 * rows + 1.0), squeeze=False)
    fig.subplots_adjust(left=0.10, right=0.97, bottom=0.85 / fig.get_figheight(),
                        top=1 - 0.55 / fig.get_figheight(), hspace=0, wspace=0.16)
    last_tick = 0.3 if x_max <= 0.5 else 0.6
    x_ticks = [1e-4, 1e-3, 0.01, 0.1, last_tick]
    x_labels = [r'$10^{-4}$', r'$10^{-3}$', r'$0.01$', r'$0.1$', '$%g$' % last_tick]
    layout = []
    for i, flavor in enumerate(flavors):
        ax = axes.flat[i]; band = bands[flavor]
        limits, display_info = proton_display_limits(xs, band, flavor, x_min, x_max)
        if flavor == 'Rs':
            # Preserve the published Rs display, including its optional mean.
            ax.fill_between(xs, band['low'], band['high'], facecolor=band_color,
                            edgecolor=band_color, alpha=band_alpha, linewidth=1.0)
            if mean_overlay:
                ax.plot(xs, band['mean'], color=band_color, lw=1.5, ls='--')
        else:
            # Rendering copies only: never connect an off-screen pole across the
            # visible plot. NaNs also preserve every unresolved-point gap.
            low, high, mean = _pdf_display_paths(band, limits)
            # A crossing in any replica's denominator splits the entire display
            # ensemble between the sampled points, without dropping either sample.
            starts = [0] + list(np.flatnonzero(band.get('break_before', np.zeros(len(xs), dtype=bool))))
            for start, stop in zip(starts, starts[1:] + [len(xs)]):
                if stop - start < 2:
                    continue
                ratio = flavor in ('Rs', 'd/u')
                ax.fill_between(xs[start:stop], low[start:stop], high[start:stop], facecolor=band_color,
                                edgecolor='none' if ratio else band_color, alpha=band_alpha,
                                linewidth=0 if ratio else 1.0)
                if mean_overlay:
                    ax.plot(xs[start:stop], mean[start:stop], color=band_color, lw=1.5, ls='--')
        ax.set_ylim(*limits)
        if limits[0] <= 0 <= limits[1] or flavor in ('sm', 'db-ub'):
            # Above the opaque band so s-minus retains a visible zero guide.
            ax.axhline(0, color='black', lw=1.0, ls='--', alpha=0.4, zorder=3)
        ax.set_xscale('log'); ax.set_xlim(x_min, x_max)
        ax.xaxis.set_major_locator(FixedLocator(x_ticks))
        ax.xaxis.set_major_formatter(FixedFormatter(x_labels))
        ax.xaxis.set_minor_locator(LogLocator(base=10, subs=np.arange(2, 10), numticks=100))
        ax.xaxis.set_minor_formatter(NullFormatter())
        _pdf_y_ticks(ax, limits)
        ax.yaxis.set_minor_locator(AutoMinorLocator(2))
        bottom = i + 2 >= len(flavors)
        ax.tick_params(which='both', direction='in', top=True, right=True,
                       labelbottom=bottom, labelleft=True, labelright=False, labelsize=23)
        ax.tick_params(which='major', length=8, width=1.4, pad=10, zorder=10)
        ax.tick_params(axis='y', which='major', pad=8)
        ax.tick_params(which='minor', length=4, width=1.0, pad=10, zorder=10)
        ax.yaxis.get_offset_text().set_visible(True)
        if bottom:
            ax.set_xlabel(r'\boldmath$x$', fontsize=35)
        # Keep endpoint labels inside their column at the shared border.
        for value, text in zip(ax.get_xticks(), ax.get_xticklabels()):
            if np.isclose(value, x_min, rtol=1e-10, atol=0):
                text.set_ha('left')
                text.set_transform(text.get_transform() + ScaledTranslation(3/72, 0, fig.dpi_scale_trans))
            if np.isclose(value, x_max, rtol=1e-10, atol=0):
                text.set_ha('right')
                text.set_transform(text.get_transform() + ScaledTranslation(-3/72, 0, fig.dpi_scale_trans))
        ax.spines['top'].set_linewidth(2)
        ax.spines['right'].set_linewidth(2)
        ax.spines['bottom'].set_linewidth(2)
        ax.spines['left'].set_linewidth(2)
        ax.set_rasterized(True)
        # Measure bold math, then score candidate rectangles against the band.
        label = _pdf_flavor_label(flavor)
        text = ax.text(0.08, 0.86, label, transform=ax.transAxes, fontsize=45,
                       va='center', clip_on=True,
                       bbox=dict(facecolor='white', edgecolor='none', alpha=0.75, pad=1))
        renderer = fig.canvas.get_renderer()
        extent = text.get_window_extent(renderer)
        text_width = extent.width / ax.bbox.width + 0.02
        text_halfheight = extent.height / ax.bbox.height / 2 + 0.02
        candidates = [(0.08, 0.86, 'left'), (0.92, 0.86, 'right'),
                      (0.08, 0.17, 'left'), (0.92, 0.17, 'right')]
        xp = (np.log(xs) - np.log(x_min)) / np.log(x_max / x_min)
        yl, yh = ax.get_ylim(); height = yh - yl
        scores = []
        for tx, ty, align in candidates:
            xlo = tx if align == 'left' else tx - text_width
            region = band['valid'] & (xp >= xlo - 0.01) & (xp <= xlo + text_width + 0.01)
            hits = region & (band['high'] >= yl + (ty - text_halfheight) * height) & (band['low'] <= yl + (ty + text_halfheight) * height)
            scores.append(int(hits.sum()))
        tx, ty, align = candidates[int(np.argmin(scores))]
        text.set_position((tx, ty)); text.set_ha(align)
        layout.append(dict(flavor=flavor, bottom_labels=bottom, label_position=[tx, ty],
                           ylim=list(ax.get_ylim()), omitted_points=int((~band['valid']).sum()), **display_info))
    for i in range(len(flavors), rows * 2): axes.flat[i].set_visible(False)
    # Treat the two annotations as one centered block. Test full rendered band
    # polygons, rather than sampled points, so a narrow edge cannot be missed.
    from matplotlib.transforms import Bbox
    annotation_texts = [_pdf_regular_math(r'Q^{2} = %g\,\mathrm{GeV}^{2}' % q2),
                        _pdf_regular_math(str(interval_label).split('%', 1)[0].strip() + r'\%\;\mathrm{C.I.}')]
    placements = []
    gap = 4 * fig.dpi / 72
    margin = 6 * fig.dpi / 72
    for i in range(len(flavors)):
        ax = axes.flat[i]
        artists = [ax.text(0.5, 0.5, value, transform=ax.transAxes, fontsize=30,
                           fontweight='normal', va='center', ha='center', clip_on=True)
                   for value in annotation_texts]
        extents = [text.get_window_extent(renderer) for text in artists]
        widths = [box.width for box in extents]
        heights = [box.height for box in extents]
        block_width = max(widths) + 2 * margin
        block_height = sum(heights) + gap + 2 * margin
        frame = ax.bbox
        band_paths = [collection.get_transform().transform_path(path)
                      for collection in ax.collections for path in collection.get_paths()]
        line_paths = [line.get_transform().transform_path(line.get_path()) for line in ax.lines]
        flavor_box = ax.texts[0].get_window_extent(renderer).padded(margin)
        if block_width < frame.width and block_height < frame.height:
            for cx in np.linspace(frame.x0 + block_width / 2, frame.x1 - block_width / 2, 25):
                for cy in np.linspace(frame.y0 + block_height / 2, frame.y1 - block_height / 2, 25):
                    centers = [cy + (heights[1] + gap) / 2,
                               cy - (heights[0] + gap) / 2]
                    boxes = [Bbox.from_bounds(cx - width / 2, y - height / 2, width, height).padded(margin)
                             for width, height, y in zip(widths, heights, centers)]
                    if any(box.overlaps(flavor_box) or
                           any(path.intersects_bbox(box, filled=True) for path in band_paths) or
                           any(path.intersects_bbox(box, filled=False) for path in line_paths)
                           for box in boxes):
                        continue
                    tx, ty = ax.transAxes.inverted().transform((cx, cy))
                    score = (tx - 0.35) ** 2 + (ty - 0.68) ** 2 + i * 0.02
                    placements.append((score, i, cx, centers))
        for artist in artists:
            artist.remove()
    if not placements:
        plt.close(fig)
        raise ValueError('No clear space for the PDF annotations at the approved font size; select a different display range.')
    _, i, cx, centers = min(placements, key=lambda item: item[0])
    ax = axes.flat[i]
    for annotation, cy in zip(annotation_texts, centers):
        tx, ty = ax.transAxes.inverted().transform((cx, cy))
        ax.text(tx, ty, annotation, transform=ax.transAxes, fontsize=30,
                fontweight='normal', va='center', ha='center', clip_on=True)
        layout[i].setdefault('annotations', []).append({'text': annotation, 'position': [tx, ty],
                                                       'fontweight': 'normal'})
    legend_name = _tex_literal(campaign_name)
    # Literal campaign text, even if its name contains Matplotlib math syntax.
    handle = Patch(facecolor=band_color, edgecolor=band_color, alpha=band_alpha,
                   label=legend_name)
    legend = fig.legend(handles=[handle], loc='upper right', bbox_to_anchor=(0.98, 1),
                        frameon=False, prop={'weight': 'bold', 'size': 20})
    for text in legend.get_texts(): text.set_parse_math(False)
    return fig, layout
