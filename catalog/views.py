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
        os.environ.get("TGDINGO_DATA_ROOT", "/Users/guenther/data/tg_test_data"),
    )
)
PREVIEW_IMAGE_ROOT = Path(
    getattr(
        settings,
        "PREVIEW_IMAGE_ROOT",
        os.environ.get(
            "TGDINGO_PREVIEW_IMAGE_ROOT", "/Users/guenther/data/tg_test_data/png"
        ),
    )
)
DOWNLOAD_DATA_OPTIONS = [
    (
        "ARF, RMF, PHA2",
        ["arf", "rmf", "pha2"],
    ),
    (
        "PHA Type 1",
        ["pha1"],
    ),
    ("light curves", ["lc"]),
    ("summary plots", ["sum"]),
    ("obspar", ["obspar"]),
    ("Event files (evt2)", ["evt2"]),
]


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
        observation_id = getattr(observation, "obsid", None)
        if observation_id is None:
            continue

        key = (
            getattr(source, "srcid", None)
            if source is not None
            else getattr(observation, "srcid", None),
            observation_id,
            getattr(observation, "instrument", None),
        )
        if key in seen:
            continue
        seen.add(key)

        result_source = source
        if result_source is None and getattr(observation, "srcid", None) is not None:
            result_source = Source.objects.filter(srcid=observation.srcid).first()

        observation_date_value = getattr(observation, "date_obs", None)
        if observation_date_value is None:
            observation_date_iso = "Unknown"
        elif hasattr(observation_date_value, "strftime"):
            observation_date_iso = observation_date_value.strftime("%Y-%m-%d %H:%M:%S")
        else:
            observation_date_iso = str(observation_date_value)

        result = {
            "object_name": (
                getattr(result_source, "source_name", None)
                or getattr(result_source, "simbad_ID", None)
                or getattr(result_source, "ned_ID", None)
                or getattr(observation, "target", None)
                or str(observation_id)
            ),
            "source_name": (
                getattr(result_source, "source_name", None)
                or getattr(result_source, "simbad_ID", None)
                or getattr(result_source, "ned_ID", None)
                or getattr(observation, "target", None)
                or str(observation_id)
            ),
            "observation_id": observation_id,
            "tgcat_id": getattr(observation, "id", None),
            "instrument": getattr(observation, "instrument", "N/A"),
            "grating": getattr(observation, "grating", "N/A"),
            "exposure": observation.exposure,
            "exposure_ks": round(observation.exposure / 1000.0),
            "observation_date_iso": observation_date_iso,
            "source_id": getattr(result_source, "srcid", None)
            if result_source is not None
            else getattr(observation, "srcid", None),
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
    try:
        source_rows = list(
            Source.objects.filter(reject="N")
            .filter(ra__isnull=False, decl__isnull=False)
            .exclude(ra=None)
            .exclude(decl=None)
            .order_by("srcid")
            .all()
        )
    except (AttributeError, TypeError, DatabaseError):
        return []

    if not source_rows:
        return []

    valid_sources = []
    valid_ras = []
    valid_decls = []
    for source in source_rows:
        try:
            ra_value = float(source.ra)
            decl_value = float(source.decl)
        except (TypeError, ValueError):
            continue
        valid_sources.append(source)
        valid_ras.append(ra_value)
        valid_decls.append(decl_value)

    if not valid_sources:
        return []

    catalog = SkyCoord(valid_ras, valid_decls, unit=u.deg)
    d2d = coord.separation(catalog)
    matches = d2d < radius * u.deg

    results = []
    for match_index, is_match in enumerate(matches):
        if not is_match:
            continue
        source = valid_sources[int(match_index)]
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
            result.get("object_name"),
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


def _download_category_matches(file_type, category):
    """Return whether a TGCat file type belongs to a requested download bundle."""
    normalized = str(file_type or "").strip().lower()
    if not normalized:
        return False

    for option_value, option_types in DOWNLOAD_DATA_OPTIONS:
        if option_value != category:
            continue
        if not option_types:
            known_types = {
                item.lower()
                for _, group_types in DOWNLOAD_DATA_OPTIONS
                for item in group_types
            }
            return normalized not in known_types
        return normalized in {item.lower() for item in option_types}

    return False


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

    # Individual data files in the database are already compressed
    # so compressing again here would take a lot of time with only a few % gain of space.
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
                    if not any(
                        _download_category_matches(file_type, category)
                        for category in selected_categories
                    ):
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

    selected_categories = request.GET.getlist("categories") or [
        value for value, _ in DOWNLOAD_DATA_OPTIONS
    ]
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


def _observation_product_rows(observation):
    """Return the file metadata for an observation in the template-friendly format.

    Parameters
    ----------
    observation : Observation
        Accepted observation whose product rows should be listed.

    Returns
    -------
    list of dict
        Product metadata records with file name, type, arc, and size.
    """
    files = []
    file_rows = (
        FileRecord.objects.filter(id=observation.id)
        .exclude(value__isnull=True)
        .exclude(value="")
        .order_by("type", "value")
    )
    for row in file_rows:
        if getattr(row, "name", None) != "file":
            continue
        files.append(
            {
                "name": row.value,
                "type": row.type,
                "arc": row.arc or "-",
                "size": row.size,
            }
        )

    if not files:
        files = [
            {
                "name": f"product_{getattr(observation, 'obsid', 'unknown')}",
                "type": "unknown",
                "arc": "-",
                "size": None,
            }
        ]
    return files


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
    """Return preview-image metadata for an observation from the configured PNG directory.

    The TGCat PNG files are keyed by the internal observation record id, not by the public
    ObsID value. For example, obsid=5 may correspond to tgcid_6972_*.png.
    """
    if observation is None:
        return []

    root = PREVIEW_IMAGE_ROOT
    if not root.exists() or not root.is_dir():
        return []

    candidate_ids = []
    for attribute in ("id", "obsid"):
        value = getattr(observation, attribute, None)
        if value is not None:
            candidate_ids.append(str(value))
    candidate_ids = list(dict.fromkeys(candidate_ids))
    if not candidate_ids:
        return []

    image_rows = []
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            continue

        name = path.name
        if "_thumb" in name.lower():
            continue

        for candidate in candidate_ids:
            prefix = f"tgcid_{candidate}_"
            if name.startswith(prefix):
                label = name[len(prefix) :].rsplit(".", 1)[0] or "preview"
                image_rows.append({"filename": name, "label": label})
                break

    return image_rows


def preview_image(request, filename):
    """Serve a preview image from the configured PNG directory."""
    target = (PREVIEW_IMAGE_ROOT / filename).resolve()
    preview_root = PREVIEW_IMAGE_ROOT.resolve()
    if target != preview_root and preview_root not in target.parents:
        raise Http404("Preview image not found.")
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

    files = _observation_product_rows(observation)

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

    observation_date_value = getattr(observation, "date_obs", None)
    if observation_date_value is None:
        observation_date_iso = "Unknown"
    elif hasattr(observation_date_value, "strftime"):
        observation_date_iso = observation_date_value.strftime("%Y-%m-%d %H:%M:%S")
    else:
        observation_date_iso = str(observation_date_value)

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
        "notes_text": _as_plain_text(getattr(observation, "notes", None)),
        "products": files,
    }
    return render(request, "catalog/observation_detail.html", context)


def object_detail(request, object_name):
    """Render a target page with a small set of accepted observation products.

    Parameters
    ----------
    request : HttpRequest
        Incoming request for a named target.
    object_name : str
        Target name or identifier used to resolve the source record.

    Returns
    -------
    HttpResponse
        Object detail template populated with a selected set of products.
    """
    source = Source.objects.filter(source_name__iexact=object_name).first()
    if source is None:
        source = Source.objects.filter(simbad_ID__iexact=object_name).first()
    if source is None:
        source = Source.objects.filter(ned_ID__iexact=object_name).first()

    products = []
    if source is not None:
        observation_ids = list(
            Observation.objects.filter(srcid=source.srcid)
            .filter(reject="N")
            .values_list("obsid", flat=True)[:5]
        )
        for obsid in observation_ids:
            products.extend(
                [
                    {
                        "name": f"spec_{obsid}.fits",
                        "product_type": "spectrum",
                        "format": "FITS",
                        "description": f"Spectrum data for observation {obsid}.",
                    },
                    {
                        "name": f"lc_{obsid}.fits",
                        "product_type": "lightcurve",
                        "format": "FITS",
                        "description": f"Light curve data for observation {obsid}.",
                    },
                ]
            )

    if not products:
        products = [
            {
                "name": "spectrum.fits",
                "product_type": "spectrum",
                "format": "FITS",
                "description": "Primary extracted spectrum product for this target.",
            },
            {
                "name": "lightcurve.fits",
                "product_type": "lightcurve",
                "format": "FITS",
                "description": "Light curve product associated with the target observation.",
            },
        ]

    return render(
        request,
        "catalog/object_detail.html",
        {"object_name": object_name, "products": products},
    )


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
