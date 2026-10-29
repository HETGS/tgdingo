import io
import re
import zipfile
from pathlib import Path
from urllib.parse import quote_plus

from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.coordinates.name_resolve import NameResolveError
from django.conf import settings
from django.db import DatabaseError
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import render

PREVIEW_IMAGE_ROOT = settings.PREVIEW_IMAGE_ROOT

from .forms import GROUPING_PARAMETER, PlotSelectionForm
from .models import FileRecord, Observation, Source
from .utils.specta import render_spectral_plot_html
from .utils.utils import (
    file_record_path,
    obsid_has_multiple_obi,
    selected_observation_display_name,
)

DOWNLOAD_DATA_OPTIONS = {
    "ARF, RMF, PHA2": [["arf", "rmf", "pha2"], True],
    "PHA Type 1": [["pha1"], False],
    "light curves": [["lc"], False],
    "summary plots": [["sum"], False],
    "obspar": [["obspar"], False],
    "Event files (evt2)": [["evt2"], False],
}

PLOT_ANALYSIS_MODES = ("wavelength", "channel", "energy")
IMAGE_LABELS = {
    "summary_flux_overview": "Overview spectrum",
    "summary_im-b": "Image and orientation",
    "summary_im-a": "Image around 0th order",
    "summary_lc": "Light curve",
    "summary_imsp": "Extraction regions",
    "summary_im_osum": "Order sorting",
    "summary_spf": "Grating spectrum (page 1)",
    "summary_spf-1": "Grating spectrum (page 2)",
    "summary_spc": "Grating spectrum (page 1)",
    "summary_spc-1": "Grating spectrum (page 2)",
    "summary_spc-2": "Grating spectrum (page 3)",
    "summary_spc-3": "Grating spectrum (page 4)",
}


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
    if obsid_counts is not None and obsid_counts.get(obsid, 0) > 1 and obi is not None:
        return f"{obsid}-{obi}"
    return str(obsid)


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
            "name": source_for_observation.source_name,
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


def _build_download_zip(selected_obsids, selected_categories):
    """Create a zip archive of all matching TGCat files for the selected observations."""
    archive_buffer = io.BytesIO()
    selected_categories = set(selected_categories)
    allowed_types = {
        file_type.lower()
        for category in selected_categories
        for file_type in DOWNLOAD_DATA_OPTIONS.get(category, [[], False])[0]
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
                    source_path = file_record_path(
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
    raw_obsids = (
        request.GET.getlist("obsids")
        + request.POST.getlist("obsids")
        + request.GET.getlist("tgcat_ids")
        + request.POST.getlist("tgcat_ids")
    )
    selected_obsids = _normalized_obsid_list(raw_obsids)

    selected_obs_labels = [
        selected_observation_display_name(obsid) for obsid in selected_obsids
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

    default_selected_categories = [
        category
        for category, (_, is_selected_by_default) in DOWNLOAD_DATA_OPTIONS.items()
        if is_selected_by_default
    ]
    selected_categories = (
        request.GET.getlist("categories") or default_selected_categories
    )
    return render(
        request,
        "catalog/download_selection.html",
        {
            "selected_obsids": selected_obsids,
            "selected_obs_labels": selected_obs_labels,
            "selected_categories": selected_categories,
            "download_options": DOWNLOAD_DATA_OPTIONS,
            "plot_tgcat_ids": selected_obsids,
        },
    )


def plot_selection(request):
    """Render the plotting page for the currently selected observation(s)."""
    raw_tgcat_ids = request.GET.getlist("tgcat_ids") + request.POST.getlist(
        "tgcat_ids"
    )
    selected_tgcat_ids = _normalized_obsid_list(raw_tgcat_ids)
    selected_obs_labels = [
        selected_observation_display_name(tgcat_id) for tgcat_id in selected_tgcat_ids
    ]
    is_post = request.method == "POST"
    if is_post:
        plot_form = PlotSelectionForm(request.POST)
        form_is_valid = plot_form.is_valid()
        cleaned_data = plot_form.cleaned_data if form_is_valid else {}
    else:
        plot_form = PlotSelectionForm(initial={"analysis": "wavelength"})
        form_is_valid = True
        cleaned_data = {
            "analysis": "wavelength",
            "grouping_method": "",
            "add_plus_minus_orders": False,
            "add_heg_meg": False,
        }
    analysis = cleaned_data.get("analysis", "wavelength")

    plot_html = None
    plot_error = None
    if selected_tgcat_ids and form_is_valid:
        try:
            grouping_method = cleaned_data.get("grouping_method") or None
            grouping_parameter = GROUPING_PARAMETER.get(grouping_method)
            add_plus_minus_orders = bool(
                cleaned_data.get("add_plus_minus_orders", False)
            )
            add_heg_meg = bool(cleaned_data.get("add_heg_meg", False))
            plot_html = render_spectral_plot_html(
                selected_tgcat_ids[0]
                if len(selected_tgcat_ids) == 1
                else selected_tgcat_ids,
                analysis,
                grouping_method,
                cleaned_data.get("group_value") if grouping_parameter else None,
                add_plus_minus_orders=add_plus_minus_orders,
                add_heg_meg=add_heg_meg,
            )
        except Exception as exc:  # pragma: no cover - runtime-only safeguard
            plot_error = str(exc)

    return render(
        request,
        "catalog/plot_selection.html",
        {
            "selected_tgcat_ids": selected_tgcat_ids,
            "plot_tgcat_ids": selected_tgcat_ids,
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

            name = filename.removeprefix(f"tgcid_{observation.id}_").removesuffix(
                ".png"
            )
            thumbnail = f"tgcid_{observation.id}_{name}_thumb.png"
            label = IMAGE_LABELS.get(name, name)
            image_rows.append(
                {"filename": filename, "label": label, "thumbnail": thumbnail}
            )

    return image_rows


def _preview_images_by_source(obsid, requested_obi=None):
    """Group preview images by source, optionally by OBI when the ObsID has multiple OBIs."""
    queryset = (
        Observation.objects.filter(obsid=obsid).filter(reject="N").order_by("obi", "id")
    )
    if requested_obi is not None:
        queryset = queryset.filter(obi=requested_obi)

    show_obi = obsid_has_multiple_obi(obsid)
    rows = list(queryset.all())

    groups = []
    for observation in rows:
        source = Source.objects.filter(srcid=observation.srcid).first()
        source_label = source.source_name

        images = _preview_image_entries_for_observation(observation)
        if not images:
            continue

        obi = observation.obi if show_obi and observation.obi is not None else None
        if obi is None:
            groups.append({"source_label": source_label, "obi": None, "images": images})
            continue

        existing = next(
            (
                group
                for group in groups
                if group["source_label"] == source_label and group["obi"] == obi
            ),
            None,
        )
        if existing is not None:
            existing["images"].extend(images)
        else:
            groups.append({"source_label": source_label, "obi": obi, "images": images})

    return groups


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

    applicable_tgcat_ids = list(queryset.values_list("id", flat=True))
    source = Source.objects.filter(srcid=observation.srcid).first()
    show_obi = obsid_has_multiple_obi(observation.obsid)

    source_label = source.source_name
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
        "observation_date_iso": observation_date_iso,
        "notes_text": _as_plain_text(getattr(observation, "comments", None)),
        "preview_images": _preview_images_by_source(obsid, requested_obi),
        "plot_tgcat_ids": applicable_tgcat_ids,
        "download_obsids": applicable_tgcat_ids,
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
