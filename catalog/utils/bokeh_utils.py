from bokeh.models import CustomJS, Select

__all__ = ["collect_plot_renderers",
           "collect_sherpa_bokeh_whisker_renderers",
           "link_whisker_muting",
           "make_y_unit_select",
           "reset_sherpa_bokeh_whisker_colors"]


def make_y_unit_select(y_axis, y_sources, y_exposures, y_label):
    """Build the "Y unit" dropdown that rescales the y data in the browser.

    Options: "sec" (as plotted, a rate per second), "ks" (rate per kilosecond,
    values x1000) and "exposure" (values x exposure time, i.e. plain counts,
    with the time unit dropped from the axis label).

    ``y_sources`` are all ColumnDataSources holding y values (lines and error
    bars); ``y_exposures`` holds the exposure in seconds of the spectrum that
    each source belongs to. The original columns are cached in the browser so
    switching back and forth never accumulates rounding errors.
    """
    select = Select(title="time:", value="sec", options=["sec", "ks", "exposure"])
    select.js_on_change(
        "value",
        CustomJS(
            args={
                "axis": y_axis,
                "sources": y_sources,
                "exposures": [float(exposure) for exposure in y_exposures],
                "base_y_label": y_label,
            },
            code="""
                const unit = cb_obj.value;
                if (unit === 'ks') {
                    axis.axis_label = base_y_label.replace(/sec/g, 'ks');
                } else if (unit === 'exposure') {
                    axis.axis_label = base_y_label.replace(/\\/sec/g, '');
                } else {
                    axis.axis_label = base_y_label;
                }

                // Columns arrive in the browser as typed arrays, not Arrays.
                sources.forEach((source, i) => {
                    const scale = (unit === 'ks') ? 1000
                        : (unit === 'exposure') ? exposures[i] : 1;
                    if (!source.__tg_orig_y) source.__tg_orig_y = {};
                    const orig = source.__tg_orig_y;
                    const new_data = Object.assign({}, source.data);
                    for (const key of ['y', 'lower', 'upper']) {
                        const col = source.data[key];
                        if (col == null || typeof col.length !== 'number') continue;
                        if (!(key in orig)) orig[key] = Float64Array.from(col);
                        new_data[key] = orig[key].map((v) => v * scale);
                    }
                    source.data = new_data;
                });
            """,
        ),
    )
    return select


def reset_sherpa_bokeh_whisker_colors(figure):
    """Reset Bokeh whiskers to the color of the matching plotted line.

    TEMPORARY WORKAROUND: Sherpa currently creates Bokeh ``Whisker`` objects
    with their default styling instead of inheriting the plotted line color.
    In the live Bokeh figure, the whiskers live in the figure layout while the
    line renderers are in the main renderer list. Match each whisker to the line
    with the same series order so overplotted datasets keep their own colors.
    Remove this once the upstream Sherpa bug is fixed.
    """

    def _apply_whisker_color(whisker, color):
        whisker.line_color = color
        if hasattr(whisker, "line_alpha"):
            whisker.line_alpha = getattr(whisker, "line_alpha", 1)

        alpha = getattr(whisker, "line_alpha", 1)
        for head in (
            getattr(whisker, "upper_head", None),
            getattr(whisker, "lower_head", None),
        ):
            if head is None:
                continue
            if hasattr(head, "line_color"):
                head.line_color = color
            if hasattr(head, "fill_color"):
                head.fill_color = color
            if hasattr(head, "line_alpha"):
                head.line_alpha = alpha
            if hasattr(head, "fill_alpha"):
                head.fill_alpha = alpha

    line_colors = []
    for renderer in getattr(figure, "renderers", ()):
        glyph = getattr(renderer, "glyph", None)
        if glyph is None:
            continue
        color = getattr(glyph, "line_color", None) or getattr(glyph, "fill_color", None)
        if color:
            line_colors.append(color)

    whiskers = []
    for container in (
        getattr(figure, "center", ()),
        getattr(figure, "above", ()),
        getattr(figure, "below", ()),
        getattr(figure, "left", ()),
        getattr(figure, "right", ()),
    ):
        for item in container:
            if type(item).__name__ == "Whisker":
                whiskers.append(item)
                continue
            glyph = getattr(item, "glyph", None)
            if glyph is not None and type(glyph).__name__ == "Whisker":
                whiskers.append(glyph)

    if not whiskers or not line_colors:
        return

    for index, whisker in enumerate(whiskers):
        color = line_colors[index] if index < len(line_colors) else line_colors[-1]
        _apply_whisker_color(whisker, color)


def collect_sherpa_bokeh_whisker_renderers(figure):
    """Collect the Bokeh whisker renderers from a Sherpa-generated figure.

    The whiskers can live in the figure layout containers rather than the main
    renderer list, especially when multiple datasets are overplotted and when
    Bokeh nests the glyphs in layout children. This helper is intentionally kept
    separate so the interactive checkbox can target all whiskers in one go.
    """
    whiskers = []

    def _walk(node):
        if node is None:
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)
            return

        if type(node).__name__ == "Whisker":
            whiskers.append(node)
            return

        glyph = getattr(node, "glyph", None)
        if glyph is not None and type(glyph).__name__ == "Whisker":
            whiskers.append(node)

        for attr in ("center", "above", "below", "left", "right", "children"):
            child = getattr(node, attr, None)
            if child is not None:
                _walk(child)

    _walk(figure)
    return whiskers


def collect_plot_renderers(figure):
    """Collect the renderers used for plotted data, including nested figure layout children.

    The Sherpa-generated Bokeh figure can include renderers directly on the figure
    object or nested under layout containers, so we gather both forms before wiring
    custom JavaScript callbacks.
    """
    plot_renderers = []
    seen = set()

    def _walk(node):
        if node is None:
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)
            return

        if (
            hasattr(node, "data_source")
            and getattr(node, "data_source", None) is not None
        ):
            renderer_id = id(node)
            if renderer_id not in seen:
                seen.add(renderer_id)
                plot_renderers.append(node)

        for attr in (
            "renderers",
            "center",
            "above",
            "below",
            "left",
            "right",
            "children",
        ):
            child = getattr(node, attr, None)
            if child is not None:
                _walk(child)

    _walk(figure)
    return plot_renderers


def link_whisker_muting(line_renderers, whiskers):
    """Make error bars follow their line when it is muted via the legend.

    Bokeh's ``Whisker`` has no ``muted`` state and cannot be part of a
    ``LegendItem``, so clicking the legend (``click_policy="mute"``) only fades
    the line. This callback fades the whiskers of the same dataset to the same
    alpha Bokeh uses for muted lines (``muted_alpha`` default, 0.2) and restores
    their original alpha when the line is un-muted.
    """
    callback = CustomJS(
        args={"whiskers": whiskers},
        code="""
            const muted = cb_obj.muted;
            for (const whisker of whiskers) {
                for (const obj of [whisker, whisker.upper_head, whisker.lower_head]) {
                    if (!obj) continue;
                    if (obj.__tg_alpha === undefined) obj.__tg_alpha = obj.line_alpha;
                    obj.line_alpha = muted ? 0.2 : obj.__tg_alpha;
                }
                // Head (tee) property changes alone do not trigger a redraw.
                whisker.change.emit();
            }
        """,
    )
    for line_renderer in line_renderers:
        line_renderer.js_on_change("muted", callback)
