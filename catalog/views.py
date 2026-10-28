import copy
import functools
import io
import os
import re
import zipfile
from pathlib import Path
from urllib.parse import quote_plus

from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.coordinates.name_resolve import NameResolveError
from bokeh.embed import components
from bokeh.layouts import column, row
from bokeh.models import Checkbox, CustomJS, Legend, LegendItem, Select
from bokeh.resources import CDN
from django.conf import settings
from django.db import DatabaseError
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import render

from .forms import GROUPING_PARAMETER, PlotSelectionForm, validate_grouping
from sherpa.astro.io import read_arf, read_pha
from sherpa.astro.plot import DataPHAPlot
import sherpa.plot as sherpa_plot
from sherpa.stats import Chi2Gehrels

from .models import FileRecord, Observation, Source

DATA_FILE_ROOT = Path(
    getattr(
        settings,
        "DATA_FILE_ROOT",
        os.environ.get("TGDINGO_DATA_ROOT", "./data"),
    )
)
PREVIEW_IMAGE_ROOT = Path(
    getattr(
        settings,
        "PREVIEW_IMAGE_ROOT",
        os.environ.get("TGDINGO_PREVIEW_IMAGE_ROOT", "./data/png"),
    )
)
DOWNLOAD_DATA_OPTIONS = {
    "ARF, RMF, PHA2": ["arf", "rmf", "pha2"],
    "PHA Type 1": ["pha1"],
    "light curves": ["lc"],
    "summary plots": ["sum"],
    "obspar": ["obspar"],
    "Event files (evt2)": ["evt2"],
}
TG_PART = {1: "HEG", 2: "MEG", 3: "LEG"}
PLOT_ANALYSIS_MODES = ("wavelength", "channel", "energy")


def _reset_sherpa_bokeh_whisker_colors(figure):
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


def _collect_sherpa_bokeh_whisker_renderers(figure):
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


def _collect_plot_renderers(figure):
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

        if hasattr(node, "data_source") and getattr(node, "data_source", None) is not None:
            renderer_id = id(node)
            if renderer_id not in seen:
                seen.add(renderer_id)
                plot_renderers.append(node)

        for attr in ("renderers", "center", "above", "below", "left", "right", "children"):
            child = getattr(node, attr, None)
            if child is not None:
                _walk(child)

    _walk(figure)
    return plot_renderers


def _link_whisker_muting(line_renderers, whiskers):
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


def _make_y_unit_select(y_axis, y_sources, y_exposures, y_label):
    """Build the "Y unit" dropdown that rescales the y data in the browser.

    Options: "sec" (as plotted, a rate per second), "ks" (rate per kilosecond,
    values x1000) and "exposure" (values x exposure time, i.e. plain counts,
    with the time unit dropped from the axis label).

    ``y_sources`` are all ColumnDataSources holding y values (lines and error
    bars); ``y_exposures`` holds the exposure in seconds of the spectrum that
    each source belongs to. The original columns are cached in the browser so
    switching back and forth never accumulates rounding errors.
    """
    select = Select(title="Y unit:", value="sec", options=["sec", "ks", "exposure"])
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


def _parse_coordinate(value):
    """Parse a user-supplied coordinate string into an Astropy SkyCoord.

    Parameters
    ----------
    value : str or None
        Raw coordinate text supplied by the user.

    Returns
    -------
    astropy.coordinates.SkyCoord or None
        Parsed coordinate object, or ``None`` if the input is empty or invalid.

    Notes
    -----
    Supports decimal degrees and common sexagesimal formats used by the TGCat UI.
    """
    if value is None:
        return None

    text = re.sub(r"\s+", " ", value.strip())
    # common form to write coordinates is with a colon separator, e.g. "12:34:56.7 +12:34:56"
    text = re.sub(r":", " ", text)
    if not text:
        return None

    if "," in text:
        parts = [part.strip() for part in text.split(",") if part.strip()]
        if len(parts) == 2:
            try:
                return SkyCoord(parts[0], parts[1], unit=(u.deg, u.deg))
            except (TypeError, ValueError, u.UnitsError):
                pass

    tokens = text.split()
    if len(tokens) == 2:
        try:
            return SkyCoord(float(tokens[0]), float(tokens[1]), unit=u.deg)
        except (TypeError, ValueError):
            pass
        try:
            return SkyCoord(tokens[0], tokens[1], unit=(u.hourangle, u.deg))
        except (TypeError, ValueError, u.UnitsError):
            pass

    if len(tokens) >= 6:
        ra_text = " ".join(tokens[:3])
        dec_text = " ".join(tokens[3:6])
        try:
            return SkyCoord(ra_text, dec_text, unit=(u.hourangle, u.deg))
        except (TypeError, ValueError, u.UnitsError):
            pass

    return None


def _observation_detail_slug(obsid, obi=None, obsid_counts=None):
    """Return the canonical observation detail URL slug for a row."""
    if obsid is None:
        return ""
    if obsid_counts is not None and obsid_counts.get(obsid, 0) > 1:
        if obi is not None:
            return f"{obsid}-{obi}"
    return str(obsid)


def _obsid_has_multiple_obi(obsid):
    """Return whether a given ObsID has more than one distinct OBI value."""
    if obsid is None:
        return False
    obi_values = set(
        Observation.objects.filter(obsid=obsid)
        .filter(reject="N")
        .exclude(obi__isnull=True)
        .values_list("obi", flat=True)
    )
    return len(obi_values) > 1


def _observation_result_entries(observations, source=None):
    """Convert observation records into deduplicated result dictionaries.

    Parameters
    ----------
    observations : iterable
        Observation objects to convert into search result entries.
    source : Source, optional
        Matching source associated with the observations.

    Returns
    -------
    list of dict
        Result dictionaries for each valid observation.
    """
    results = []
    seen = set()
    obsid_counts = {}
    for observation in observations:
        obsid_counts[observation.obsid] = obsid_counts.get(observation.obsid, 0) + 1

    for observation in observations:
        source_for_observation = source
        if source_for_observation is None:
            source_for_observation = Source.objects.filter(
                srcid=observation.srcid
            ).first()
            assert source_for_observation is not None, (
                "Observation has no matching source."
            )
        key = (observation.obsid, observation.obi, observation.srcid)
        if key in seen:
            continue
        seen.add(key)

        observation_date_iso = observation.date_obs.strftime("%Y-%m-%d %H:%M:%S")

        result = {
            "name": (
                getattr(source_for_observation, "source_name", None)
                or getattr(source_for_observation, "simbad_ID", None)
                or getattr(source_for_observation, "ned_ID", None)
                or getattr(observation, "target", None)
                or str(observation.obsid)
            ),
            "observation_id": observation.obsid,
            "obi": observation.obi,
            "show_obi": obsid_counts.get(observation.obsid, 0) > 1,
            "detail_slug": _observation_detail_slug(
                observation.obsid, observation.obi, obsid_counts
            ),
            "tgcat_id": observation.id,
            "instrument": observation.instrument,
            "grating": observation.grating,
            "exposure": observation.exposure,
            "exposure_ks": round(observation.exposure / 1000.0),
            "observation_date_iso": observation_date_iso,
            "source_id": source_for_observation.srcid,
        }
        results.append(result)
    return results


def _source_observation_results(source):
    """Build result records for all accepted observations attached to a source.

    Parameters
    ----------
    source : Source
        Source object matched by a search query.

    Returns
    -------
    list of dict
        Search result dictionaries, one per accepted observation for the source.
    """
    matched_observations = list(
        Observation.objects.filter(srcid=source.srcid)
        .filter(reject="N")
        .order_by("obsid")
        .all()
    )

    return _observation_result_entries(matched_observations, source=source)


def _coordinate_search_results(query, radius_deg: float = 0.1):
    """Return accepted observations within a radius of a coordinate.

    Parameters
    ----------
    query : str or astropy.coordinates.SkyCoord
        Coordinate string or resolved coordinate to search against the catalog.
    radius_deg : float, default=0.1
        Angular search radius in degrees.

    Returns
    -------
    list of dict
        Result entries for matching source observations, using the matched source name
        and each accepted ObsID.
    """
    coord = query if isinstance(query, SkyCoord) else _parse_coordinate(query)
    if coord is None:
        return []

    radius = float(radius_deg or 0.1)
    sources = list(Source.objects.filter(reject="N").order_by("srcid").all())

    if not sources:
        return []

    catalog = SkyCoord([s.ra for s in sources], [s.decl for s in sources], unit=u.deg)
    d2d = coord.separation(catalog)
    matches = d2d < radius * u.deg

    results = []
    for match_index, is_match in enumerate(matches):
        if not is_match:
            continue
        source = sources[int(match_index)]
        if source is None:
            continue

        results.extend(_source_observation_results(source))

    return results


def _search_results(query: str, radius_deg: float = 0.1):
    """Search catalog sources and observations by name, coordinate, or obsid.

    Parameters
    ----------
    query : str
        Free-text search term submitted by the user.
    radius_deg : float, default=0.1
        Radius used for coordinate-based searches in degrees.

    Returns
    -------
    list of dict
        Deduplicated result rows suitable for the search template.
    """
    if not query:
        return []

    query = query.strip()
    if _parse_coordinate(query) is not None:
        return _coordinate_search_results(query, radius_deg=radius_deg)

    if re.fullmatch(r"\d+", query):
        matching_observations = list(
            Observation.objects.filter(obsid=int(query))
            .filter(reject="N")
            .order_by("id")
            .all()
        )
        return _observation_result_entries(matching_observations)

    results = []

    try:
        objects = (
            Source.objects.filter(source_name__icontains=query)
            | Source.objects.filter(simbad_ID__icontains=query)
            | Source.objects.filter(ned_ID__icontains=query)
        )
        for source in objects:
            results.extend(_source_observation_results(source))
    except (AttributeError, TypeError, DatabaseError):
        pass

    try:
        resolved_coord = SkyCoord.from_name(query)
        results.extend(
            _coordinate_search_results(resolved_coord, radius_deg=radius_deg)
        )
    except (ValueError, TypeError, NameResolveError):
        pass

    seen = set()
    deduplicated = []
    for result in results:
        key = (
            result.get("name"),
            result.get("observation_id"),
            result.get("obi"),
            result.get("instrument"),
            result.get("source_id"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(result)
    return deduplicated


def home(request):
    """Render the TGCat home page and execute the active search query.

    Parameters
    ----------
    request : HttpRequest
        Incoming request containing an optional ``q`` and ``radius``.

    Returns
    -------
    HttpResponse
        Home page with the search form and any matching catalog entries.
    """
    query = (request.GET.get("q") or "").strip()
    radius_raw = request.GET.get("radius")
    try:
        radius = float(radius_raw)
    except (TypeError, ValueError):
        radius = 0.1
    results = _search_results(query, radius_deg=radius)
    return render(
        request,
        "catalog/home.html",
        {"query": query, "radius": radius, "results": results},
    )


def help(request):
    """Render a static help page with example search queries.

    Parameters
    ----------
    request : HttpRequest
        Incoming help request.

    Returns
    -------
    HttpResponse
        Search examples page that demonstrates accepted query formats.
    """
    return render(request, "catalog/help.html")


def _normalized_obsid_list(values):
    """Parse a list of selected TGCat row IDs from the request."""
    selected = []
    for value in values:
        for token in str(value).split(","):
            token = token.strip()
            if not token:
                continue
            try:
                selected.append(int(token))
            except ValueError:
                continue
    return list(dict.fromkeys(selected))


def _format_observation_label(observation, source_name=None):
    """Format an observation label consistently across the page and plot."""
    sname = source_name or "Unknown"
    if _obsid_has_multiple_obi(observation.obsid):
        return f"ObsID: {observation.obsid} (OBI: {observation.obi}, {sname})"
    else:
        return f"ObsID: {observation.obsid} ({sname})"


def _selected_observation_display_name(observation_id):
    """Return the shared display label for a selected TGCat row."""
    observation = (
        Observation.objects.filter(id=observation_id).filter(reject="N").first()
    )
    if observation is None:
        return f"(ObsID: {observation_id}, Unknown)"
    source = Source.objects.filter(srcid=observation.srcid).first()
    return _format_observation_label(observation, getattr(source, "source_name", None))


def _file_record_path(observation_id, file_type, value, arc_name=None):
    """Return the resolved file path for a TGCat file record."""
    if not value:
        return None
    if not arc_name:
        arc_name = ""
    arc_name = str(arc_name or "").strip()
    candidate = Path(DATA_FILE_ROOT)
    if arc_name:
        candidate = candidate / arc_name
    candidate = candidate / file_type / f"tgcid_{observation_id}_{value}"
    resolved = candidate.resolve()
    if resolved.exists() and resolved.is_file():
        return resolved
    return None


@functools.lru_cache(maxsize=16)
def _setup_spectra(tgcat_id):
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
    path = _file_record_path(tgcat_id, "pha2", value, arc_name)
    if path is None:
        raise ValueError(f"PHA2 file is missing for observation {tgcat_id}.")

    phalist = read_pha(str(path))

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
            arf_path = _file_record_path(
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


def _render_spectral_plot_html(
    observation_ids, analysis="wavelength", grouping_method=None, grouping_value=None
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
            (observation, copy.deepcopy(pha)) for pha in _setup_spectra(observation_id)
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
        observation_labels[observation.id] = _format_observation_label(
            observation, getattr(source, "source_name", None)
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
        _reset_sherpa_bokeh_whisker_colors(figure)
        rendered_count = len(figure.renderers)
        dataset_whiskers = [
            whisker
            for whisker in _collect_sherpa_bokeh_whisker_renderers(figure)
            if whisker.id not in linked_whisker_ids
        ]
        linked_whisker_ids.update(whisker.id for whisker in dataset_whiskers)
        if dataset_whiskers:
            _link_whisker_muting(
                [r for r in renderers if hasattr(r, "muted")], dataset_whiskers
            )
        # Remember which spectrum's exposure belongs to each new data source
        # (lines: ``data_source``, error bars: ``Whisker.source``).
        for node in _collect_plot_renderers(figure) + _collect_sherpa_bokeh_whisker_renderers(figure):
            node_source = getattr(node, "data_source", None) or getattr(node, "source", None)
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
        whisker_renderers.extend(_collect_sherpa_bokeh_whisker_renderers(plotted_figure))

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
    y_unit_select = _make_y_unit_select(y_axis, y_sources, y_exposures, y_label)

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


def _build_download_zip(selected_obsids, selected_categories):
    """Create a zip archive of all matching TGCat files for the selected observations."""
    archive_buffer = io.BytesIO()
    selected_categories = set(selected_categories)
    allowed_types = {
        file_type.lower()
        for category in selected_categories
        for file_type in DOWNLOAD_DATA_OPTIONS.get(category, [])
    }

    # Individual data files in the database are already compressed
    # so compressing again here would take a lot of time with only a few % gain of space.
    # Default compression is ZIP_STORED, which is fast and does not recompress the files.
    with zipfile.ZipFile(archive_buffer, mode="w") as archive:
        for obsid in selected_obsids:
            observations = list(
                Observation.objects.filter(id=obsid)
                .filter(reject="N")
                .order_by("id")
                .all()
            )
            for observation in observations:
                file_rows = (
                    FileRecord.objects.filter(id=observation.id)
                    .filter(name="file")
                    .exclude(value__isnull=True)
                    .exclude(value="")
                    .order_by("arc", "type", "value")
                    .all()
                )
                for row in file_rows:
                    file_type = str(getattr(row, "type", "") or "").strip().lower()
                    if not file_type or file_type not in allowed_types:
                        continue

                    arc_name = str(getattr(row, "arc", "") or "").strip()
                    value_name = str(getattr(row, "value", "") or "").strip()
                    source_path = _file_record_path(
                        observation.id, file_type, value_name, arc_name
                    )
                    if source_path is None:
                        continue

                    archive_name = (
                        f"obs_{getattr(observation, 'obsid', 'unknown')}_"
                        f"tgid_{getattr(observation, 'id', 'unknown')}"
                        f"/{source_path.name}"
                    )
                    archive.write(source_path, arcname=archive_name)

    archive_buffer.seek(0)
    return archive_buffer


def download_selection(request):
    """Render the bulk-download selection page or return a ZIP archive for the chosen groups."""
    raw_obsids = request.GET.getlist("obsids") + request.POST.getlist("obsids")
    selected_obsids = _normalized_obsid_list(raw_obsids)

    selected_obs_labels = [
        _selected_observation_display_name(obsid) for obsid in selected_obsids
    ]

    if request.method == "POST":
        selected_categories = request.POST.getlist("categories") or []
        if not selected_obsids or not selected_categories:
            return render(
                request,
                "catalog/download_selection.html",
                {
                    "selected_obsids": selected_obsids,
                    "selected_obs_labels": selected_obs_labels,
                    "selected_categories": [],
                    "download_options": DOWNLOAD_DATA_OPTIONS,
                    "error": "Select at least one observation and one download option.",
                },
            )

        archive_buffer = _build_download_zip(selected_obsids, selected_categories)
        response = HttpResponse(
            archive_buffer.getvalue(), content_type="application/zip"
        )
        response["Content-Disposition"] = 'attachment; filename="tgcat_selection.zip"'
        return response

    selected_categories = request.GET.getlist("categories") or list(
        DOWNLOAD_DATA_OPTIONS
    )
    return render(
        request,
        "catalog/download_selection.html",
        {
            "selected_obsids": selected_obsids,
            "selected_obs_labels": selected_obs_labels,
            "selected_categories": selected_categories,
            "download_options": DOWNLOAD_DATA_OPTIONS,
        },
    )


def plot_selection(request):
    """Render the plotting page for the currently selected observation(s)."""
    raw_tgcat_ids = request.GET.getlist("tgcat_ids") + request.POST.getlist(
        "tgcat_ids"
    )
    selected_tgcat_ids = _normalized_obsid_list(raw_tgcat_ids)
    selected_obs_labels = [
        _selected_observation_display_name(tgcat_id) for tgcat_id in selected_tgcat_ids
    ]
    is_post = request.method == "POST"
    if is_post:
        plot_form = PlotSelectionForm(request.POST)
        form_is_valid = plot_form.is_valid()
        cleaned_data = plot_form.cleaned_data if form_is_valid else {}
    else:
        plot_form = PlotSelectionForm(initial={"analysis": "wavelength"})
        form_is_valid = True
        cleaned_data = {"analysis": "wavelength", "grouping_method": ""}
    analysis = cleaned_data.get("analysis", "wavelength")

    plot_html = None
    plot_error = None
    if selected_tgcat_ids and form_is_valid:
        try:
            grouping_method = cleaned_data.get("grouping_method") or None
            grouping_parameter = GROUPING_PARAMETER.get(grouping_method)
            plot_html = _render_spectral_plot_html(
                selected_tgcat_ids[0]
                if len(selected_tgcat_ids) == 1
                else selected_tgcat_ids,
                analysis,
                grouping_method,
                cleaned_data.get("group_value") if grouping_parameter else None,
            )
        except Exception as exc:  # pragma: no cover - runtime-only safeguard
            plot_error = str(exc)

    return render(
        request,
        "catalog/plot_selection.html",
        {
            "selected_tgcat_ids": selected_tgcat_ids,
            "selected_obs_labels": selected_obs_labels,
            "analysis": analysis,
            "plot_analysis_modes": PLOT_ANALYSIS_MODES,
            "plot_form": plot_form,
            "plot_html": plot_html,
            "plot_error": plot_error,
        },
    )


def _as_plain_text(value):
    """Convert binary or non-string values to plain text for template display.

    Parameters
    ----------
    value : object
        Value that may be ``None``, bytes, or another Python object.

    Returns
    -------
    str
        String-safe text representation for rendering in templates.
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _preview_image_entries_for_observation(observation):
    """Return preview-image metadata for an observation from the SQL file metadata.

    The database row identifies the logical preview family (for example,
    "summary_spf.ps"), while the actual PNGs in the preview directory are page-aware and
    can exist as "tgcid_<internal_id>_summary_spf.png",
    "tgcid_<internal_id>_summary_spf-1.png", etc. We therefore check a small, known set
    of likely numbered variants rather than scanning the whole preview directory.
    """
    if not PREVIEW_IMAGE_ROOT.exists() or not PREVIEW_IMAGE_ROOT.is_dir():
        raise Http404("Preview image directory is not configured or does not exist.")

    image_rows = []
    for row in (
        FileRecord.objects.filter(id=observation.id)
        .filter(name="file")
        .filter(type="sum")
        .exclude(value__isnull=True)
        .exclude(value="")
        .order_by("type", "value")
        .all()
    ):
        value = str(getattr(row, "value", "") or "").strip()
        if not value:
            continue

        candidate = Path(value)
        stem = candidate.stem
        if not stem or "_thumb" in stem.lower():
            continue

        base_prefix = f"tgcid_{observation.id}_{stem}"
        filenames = [base_prefix + ".png"]
        filenames.extend(f"{base_prefix}-{page}.png" for page in range(1, 4))

        for filename in filenames:
            if "_thumb" in filename.lower():
                continue

            target = (PREVIEW_IMAGE_ROOT / filename).resolve()
            if not target.exists() or not target.is_file():
                continue

            label = filename.removeprefix(f"tgcid_{observation.id}_").removesuffix(
                ".png"
            )
            image_rows.append({"filename": filename, "label": label})

    return image_rows


def preview_image(request, filename):
    """Serve a preview image from the configured PNG directory."""
    target = (PREVIEW_IMAGE_ROOT / filename).resolve()
    if not target.exists() or not target.is_file():
        raise Http404("Preview image not found.")

    return FileResponse(target.open("rb"), content_type="image/png")


def observation_detail(request, obsid_slug):
    """Render a single accepted observation with metadata and product links.

    Parameters
    ----------
    request : HttpRequest
        Incoming request for a specific observation.
    obsid_slug : str
        ObsID, optionally suffixed with ``-<obi>`` to disambiguate multiple rows.

    Returns
    -------
    HttpResponse
        Observation detail page.

    Raises
    ------
    Http404
        If no accepted observation with the supplied ID exists.
    """
    match = re.fullmatch(r"(\d+)(?:-(\d+))?", str(obsid_slug).strip())
    if match is None:
        raise Http404("Observation not found.")

    obsid = int(match.group(1))
    requested_obi = int(match.group(2)) if match.group(2) is not None else None

    queryset = (
        Observation.objects.filter(obsid=obsid)
        .filter(reject="N")
        .order_by("obi", "id")
    )
    observation = (
        queryset.first() if requested_obi is None else queryset.filter(obi=requested_obi).first()
    )
    if observation is None:
        raise Http404("Observation not found.")

    source = Source.objects.filter(srcid=observation.srcid).first()
    show_obi = _obsid_has_multiple_obi(observation.obsid)

    source_label = (
        getattr(source, "source_name", None)
        or getattr(source, "simbad_ID", None)
        or getattr(source, "ned_ID", None)
        or observation.target
        or "Unknown target"
    )
    source_url = None
    if (
        source
        and getattr(source, "simbad_ID", None)
        and str(source.simbad_ID) != "NO_AUTO_MATCH"
    ):
        source_url = (
            "https://simbad.cds.unistra.fr/simbad/sim-basic?Ident="
            + quote_plus(str(source.simbad_ID))
        )

    observation_date_iso = observation.date_obs.strftime("%Y-%m-%d %H:%M:%S")

    context = {
        "observation": observation,
        "source": source,
        "target_name": source_label,
        "source_url": source_url,
        "show_obi": show_obi,
        "coordinates": {
            "ra": getattr(observation, "ra_targ", None)
            if getattr(observation, "ra_targ", None) is not None
            else getattr(observation, "ra", None),
            "decl": getattr(observation, "dec_targ", None)
            if getattr(observation, "dec_targ", None) is not None
            else getattr(observation, "decl", None),
        },
        "exposure_ks": round(observation.exposure / 1000.0),
        "preview_images": _preview_image_entries_for_observation(observation),
        "observation_date_iso": observation_date_iso,
        "notes_text": _as_plain_text(getattr(observation, "comments", None)),
    }
    return render(request, "catalog/observation_detail.html", context)


def download_product(request, filename):
    """Stream a local TGCat product file back to the client as an attachment.

    Parameters
    ----------
    request : HttpRequest
        Incoming download request.
    filename : str
        Name of the product file beneath the configured data root.

    Returns
    -------
    FileResponse
        File download response for the requested artifact.

    Raises
    ------
    Http404
        If the requested file does not exist or is not a regular file.
    """
    target = Path(settings.DATA_FILE_ROOT) / filename
    if not target.exists() or not target.is_file():
        raise Http404("Requested data product was not found.")
    return FileResponse(target.open("rb"), as_attachment=True, filename=filename)
