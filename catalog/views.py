import io
import os
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
    for observation in observations:
        source_for_observation = source
        if source_for_observation is None:
            source_for_observation = Source.objects.filter(
                srcid=observation.srcid
            ).first()
            assert source_for_observation is not None, (
                "Observation has no matching source."
            )
        key = (observation.obsid, observation.srcid)
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
            "tgcat_id": getattr(observation, "id", None),
            "instrument": getattr(observation, "instrument", "N/A"),
            "grating": getattr(observation, "grating", "N/A"),
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


def _selected_observation_display_name(observation_id):
    """Render the user-facing selection label based on the TGCat observation row."""
    observation = (
        Observation.objects.filter(id=observation_id).filter(reject="N").first()
    )
    # Should never happen , but better save than sorry.
    if observation is None:
        return f"(ObsID: {observation_id}, Unknown)"
    source = Source.objects.filter(srcid=observation.srcid).first()
    if source is None:
        return f"(ObsID: {observation.obsid}, Unknown)"
    return f"(ObsID: {observation.obsid}, {source.source_name})"


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
                    if not arc_name or not value_name:
                        continue

                    source_path = (
                        DATA_FILE_ROOT
                        / arc_name
                        / file_type
                        / f"tgcid_{observation.id}_{value_name}"
                    ).resolve()
                    if not source_path.exists() or not source_path.is_file():
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


def observation_detail(request, obsid):
    """Render a single accepted observation with metadata and product links.

    Parameters
    ----------
    request : HttpRequest
        Incoming request for a specific observation.
    obsid : int
        ObsID to fetch.

    Returns
    -------
    HttpResponse
        Observation detail page.

    Raises
    ------
    Http404
        If no accepted observation with the supplied ID exists.
    """
    observation = (
        Observation.objects.filter(obsid=obsid)
        .filter(reject="N")
        .order_by("id")
        .first()
    )
    if observation is None:
        raise Http404("Observation not found.")

    source = Source.objects.filter(srcid=observation.srcid).first()

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
