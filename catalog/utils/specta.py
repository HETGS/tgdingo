# Copyright (C) 2026  MIT - see LICENSE.md
import copy
import functools

from bokeh.embed import components
from bokeh.layouts import column, row
from bokeh.models import Checkbox, CustomJS, Legend, LegendItem
from bokeh.resources import CDN
from sherpa import plot as sherpa_plot
from sherpa.astro.io import read_arf, read_pha
from sherpa.astro.plot import DataPHAPlot
from sherpa.stats import Chi2Gehrels

from ..forms import GROUPING_PARAMETER, validate_grouping
from ..models import FileRecord, Observation, Source
from .bokeh_utils import (
    collect_plot_renderers,
    collect_sherpa_bokeh_whisker_renderers,
    link_whisker_muting,
    make_y_unit_select,
    reset_sherpa_bokeh_whisker_colors,
)
from .utils import file_record_path, format_observation_label

__all__ = ["TG_PART", "render_spectral_plot_html"]


TG_PART = {1: "HEG", 2: "MEG", 3: "LEG"}


@functools.lru_cache(maxsize=16)
def load_spectra(tgcat_id):
    """Load the response files associated with a PHA spectrum."""
    spectra = []

    files = (
        FileRecord.objects.filter(id=tgcat_id)
        .filter(name="file")
        .filter(type="pha2")
        .exclude(value__isnull=True)
        .exclude(value="")
        .exclude(value__icontains="bg")
        .order_by("arc", "value")
        .first()
    )
    if files is None:
        raise ValueError(f"No valid PHA2 file found for observation {tgcat_id}.")
    value = str(getattr(files, "value", "") or "").strip()
    arc_name = str(getattr(files, "arc", "") or "").strip()
    path = file_record_path(tgcat_id, "pha2", value, arc_name)
    if path is None:
        raise ValueError(f"PHA2 file is missing for observation {tgcat_id}.")

    phalist = read_pha(str(path))
    # We load a pha2 files, so the output is a list.
    assert type(phalist) is list

    for pha in phalist:
        tg_m = int(pha.header["TG_M"])
        tg_part = int(pha.header["TG_PART"])
        if abs(tg_m) > 1:
            continue
        if str(pha.header.get("INSTRUME", "")).upper().startswith("HRC"):
            response_orders = range(-1, -9, -1) if tg_m < 0 else range(1, 9)
        else:
            response_orders = (tg_m,)
        part_name = TG_PART[tg_part]

        for response_index, order in enumerate(response_orders, start=1):
            # For plotting alone, the RMF is not needed.
            # On the other hand, those are big files and we save a lot
            # of time by not loading them, so we only load the ARF here.
            arf_path = file_record_path(
                tgcat_id,
                "arf",
                f"{part_name.lower()}_{order}.arf.gz",
                arc_name,
            )
            if arf_path is None:
                raise ValueError(
                    f"ARF response is missing for {part_name} order {order} "
                    f"in TGCat row {tgcat_id}."
                )
            arf = read_arf(str(arf_path))
            pha.set_response(arf, None, id=response_index)

        spectra.append(pha)
    return spectra


def render_spectral_plot_html(
    observation_ids,
    analysis="wavelength",
    grouping_method=None,
    grouping_value=None,
    add_plus_minus_orders=False,
    add_heg_meg=False,
):
    """Render a Bokeh plot for selected TGCat observation rows.

    Notes
    -----
    We are still exploring if this approach is fast enough. Here is a thought
    for significant speed-up:
    Currently, the pha is read first, and then looping over it, the arf is added
    as needed for some of the spectra (e.g. not HETG order -3).
    Instead, one
    """
    grouping_method, grouping_value = validate_grouping(grouping_method, grouping_value)
    if isinstance(observation_ids, int):
        observation_ids = [observation_ids]
    observations = []
    spectra = []
    for observation_id in observation_ids:
        observation = (
            Observation.objects.filter(id=observation_id).filter(reject="N").first()
        )
        if observation is None:
            raise ValueError(f"Observation {observation_id} was not found.")
        observations.append(observation)
        spectra.extend(
            (observation, copy.deepcopy(pha)) for pha in load_spectra(observation_id)
        )

    if not spectra:
        raise ValueError(
            "No plottable spectra were found for the selected observations."
        )
    for _, pha in spectra:
        pha.set_analysis(analysis)
        if grouping_method in GROUPING_PARAMETER:
            getattr(pha, grouping_method)(grouping_value)

    sherpa_plot.set_backend("BokehBackend")
    dataplot = DataPHAPlot()
    legend_items = []
    rendered_count = 0
    # source id -> (ColumnDataSource, exposure in s) for all y data in the plot
    y_source_exposure = {}
    linked_whisker_ids = set()
    multi_observation_plot = len(observations) > 1
    observation_labels = {}
    for observation in observations:
        source = (
            Source.objects.filter(srcid=observation.srcid).first()
            if getattr(observation, "srcid", None) is not None
            else None
        )
        observation_labels[observation.id] = format_observation_label(
            observation, source.source_name
        )
    obs_titles = list(dict.fromkeys(observation_labels.values()))
    plot_title = obs_titles[0] if len(obs_titles) == 1 else ", ".join(obs_titles)

    chi2 = Chi2Gehrels()
    for i, (observation, pha) in enumerate(spectra):
        dataplot.prepare(pha, stat=chi2)
        obs_title = observation_labels[observation.id]
        part_name = TG_PART.get(int(pha.header["TG_PART"]), "Unknown")
        tg_m = int(pha.header["TG_M"])
        dataplot.title = plot_title
        dataplot.plot(
            label=(
                f"{obs_title} {part_name} {tg_m}"
                if multi_observation_plot
                else f"{part_name} {tg_m}"
            ),
            linestyle="-",
            marker=None,
            overplot=i != 0,
            xerrorbars=True,
            yerrorbars=True,
        )
        # In Sherpa 4.19, the labels set in the plot() call above are not automatically added to the legend,
        # so we manually add them here.
        # This can be removed when Sherpa supports this.
        figure_grid = sherpa_plot.backend.current_fig
        figure = figure_grid.children[0][0]
        renderers = figure.renderers[rendered_count:]
        # TEMPORARY WORKAROUND: Sherpa/Bokeh currently assigns a default color to
        # the `Whisker` glyphs, so the error bars do not match the plotted line.
        # Reset the whisker colors here until the Sherpa bug is fixed.
        reset_sherpa_bokeh_whisker_colors(figure)
        rendered_count = len(figure.renderers)
        dataset_whiskers = [
            whisker
            for whisker in collect_sherpa_bokeh_whisker_renderers(figure)
            if whisker.id not in linked_whisker_ids
        ]
        linked_whisker_ids.update(whisker.id for whisker in dataset_whiskers)
        if dataset_whiskers:
            link_whisker_muting(
                [r for r in renderers if hasattr(r, "muted")], dataset_whiskers
            )
        # Remember which spectrum's exposure belongs to each new data source
        # (lines: ``data_source``, error bars: ``Whisker.source``).
        for node in collect_plot_renderers(
            figure
        ) + collect_sherpa_bokeh_whisker_renderers(figure):
            node_source = getattr(node, "data_source", None) or getattr(
                node, "source", None
            )
            if node_source is not None and node_source.id not in y_source_exposure:
                y_source_exposure[node_source.id] = (
                    node_source,
                    getattr(pha, "exposure", None) or 1.0,
                )
        legend_items.append(
            LegendItem(
                label=(
                    f"{obs_title} {part_name} {tg_m}"
                    if multi_observation_plot
                    else f"{part_name} {tg_m}"
                ),
                renderers=renderers,
            )
        )
        # end manual legend workaround

    # Since the can't control the default Bokeh figure size from Sherpa,
    # we manually resize the figure here to a rectangular format that should work on most screens.
    figure_grid = sherpa_plot.backend.current_fig
    figure = figure_grid.children[0][0]
    whisker_renderers = []
    for plotted_figure, _, _ in figure_grid.children:
        whisker_renderers.extend(
            collect_sherpa_bokeh_whisker_renderers(plotted_figure)
        )

    whisker_checkbox = Checkbox(label="Show error bars", active=True)
    whisker_checkbox.js_on_change(
        "active",
        CustomJS(
            args={"whiskers": whisker_renderers},
            code="""
                for (const renderer of whiskers) {
                    if (!renderer) continue;
                    if (typeof renderer.visible !== 'undefined') {
                        renderer.visible = this.active;
                    }
                    if (renderer.glyph && typeof renderer.glyph.visible !== 'undefined') {
                        renderer.glyph.visible = this.active;
                    }
                }
            """,
        ),
    )
    try:
        y_axis = figure.yaxis[0]
        y_label = y_axis.axis_label or ""
    except (AttributeError, IndexError):
        y_axis, y_label = None, ""

    y_sources = [entry[0] for entry in y_source_exposure.values()]
    y_exposures = [entry[1] for entry in y_source_exposure.values()]
    y_unit_select = make_y_unit_select(y_axis, y_sources, y_exposures, y_label)

    figure_legend = Legend(items=legend_items) if legend_items else None
    figure.toolbar_location = "above"
    for rendered_figure, _, _ in figure_grid.children:
        rendered_figure.width = 960
        rendered_figure.height = 540

    widget_row = row(whisker_checkbox, y_unit_select)
    try:
        plot_layout = column(widget_row, figure)
        if figure_legend is not None:
            figure.add_layout(figure_legend, "right")
            figure.legend.click_policy = "mute"
    except (TypeError, ValueError):
        figure.add_layout(whisker_checkbox, "above")
        if figure_legend is not None:
            figure.add_layout(figure_legend, "right")
            figure.legend.click_policy = "mute"
        plot_layout = figure
    div, script = components(plot_layout)
    return f"{CDN.render_css()}{CDN.render_js()}{div}{script}"
