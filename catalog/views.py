import re
from pathlib import Path
from urllib.parse import quote_plus

from astropy import units as u
from astropy.coordinates import SkyCoord
from django.conf import settings
from django.http import FileResponse, Http404
from django.shortcuts import render

from .models import FileRecord, Observation, Source


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

    try:
        if "," in text:
            parts = [part.strip() for part in text.split(",") if part.strip()]
            if len(parts) == 2:
                return SkyCoord(parts[0], parts[1], unit=(u.deg, u.deg))

        tokens = text.split()
        if len(tokens) == 2:
            try:
                return SkyCoord(float(tokens[0]), float(tokens[1]), unit=u.deg)
            except Exception:
                pass
            try:
                return SkyCoord(tokens[0], tokens[1], unit=(u.hourangle, u.deg))
            except Exception:
                pass

        if len(tokens) >= 6:
            ra_text = " ".join(tokens[:3])
            dec_text = " ".join(tokens[3:6])
            try:
                return SkyCoord(ra_text, dec_text, unit=(u.hourangle, u.deg))
            except Exception:
                pass
    except Exception:
        return None

    return None


def _source_observation_results(source, summary_formatter):
    """Build result records for all accepted observations attached to a source.

    Parameters
    ----------
    source : Source
        Source object matched by a search query.
    summary_formatter : callable
        Function that accepts ``(source, observation)`` and returns a summary string.

    Returns
    -------
    list of dict
        Search result dictionaries, one per accepted observation for the source.
    """
    try:
        matched_observations = list(
            Observation.objects.filter(srcid=source.srcid)
            .filter(reject="N")
            .order_by("obsid")
            .all()
        )
    except Exception:
        matched_observations = []

    results = []
    seen = set()
    for observation in matched_observations:
        observation_id = getattr(observation, "obsid", None)
        if observation_id is None:
            continue
        key = (
            getattr(source, "srcid", None),
            observation_id,
            getattr(observation, "instrument", None),
        )
        if key in seen:
            continue
        seen.add(key)

        results.append(
            {
                "object_name": getattr(source, "source_name", None)
                or getattr(source, "simbad_ID", None)
                or getattr(source, "ned_ID", None)
                or str(getattr(source, "srcid", "Unknown target")),
                "observation_id": observation_id,
                "instrument": getattr(observation, "instrument", "N/A"),
                "source_id": getattr(source, "srcid", None),
                "summary": summary_formatter(source, observation),
            }
        )
    return results


def _coordinate_search_results(query: str, radius_deg: float = 0.1):
    """Return accepted observations within a radius of a coordinate.

    Parameters
    ----------
    query : str
        Coordinate string to search against the catalog.
    radius_deg : float, default=0.1
        Angular search radius in degrees.

    Returns
    -------
    list of dict
        Result entries for matching source observations, using the matched source name
        and each accepted observation ID.
    """
    coord = _parse_coordinate(query)
    if coord is None:
        return []

    radius = float(radius_deg or 0.1)
    try:
        source_rows = (
            Source.objects.filter(reject="N")
            .filter(ra__isnull=False, decl__isnull=False)
            .exclude(ra=None)
            .exclude(decl=None)
            .order_by("srcid")
            .all()
        )
    except Exception:
        return []

    if not source_rows:
        return []

    try:
        catalog = SkyCoord(
            [float(source.ra) for source in source_rows],
            [float(source.decl) for source in source_rows],
            unit=u.deg,
        )
    except Exception:
        return []

    try:
        d2d = coord.separation(catalog)
        matches = d2d < radius * u.deg
    except Exception:
        return []

    results = []
    seen = set()
    for match_index, is_match in enumerate(matches):
        if not is_match:
            continue
        source = source_rows[int(match_index)]
        if source is None:
            continue

        results.extend(
            _source_observation_results(
                source,
                lambda src, observation: (
                    f"Target {getattr(src, 'source_name', getattr(src, 'simbad_ID', getattr(src, 'ned_ID', 'Unknown target')))} is within {radius:g}° of the searched coordinates."
                ),
            )
        )

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

    results = []

    try:
        objects = (
            Source.objects.filter(source_name__icontains=query)
            | Source.objects.filter(simbad_ID__icontains=query)
            | Source.objects.filter(ned_ID__icontains=query)
        )
        for source in objects:
            results.extend(
                _source_observation_results(
                    source,
                    lambda src, observation: (
                        f"Catalog entry for {getattr(src, 'source_name', None) or getattr(src, 'simbad_ID', None) or getattr(src, 'ned_ID', None)}."
                        if getattr(src, "source_name", None)
                        or getattr(src, "simbad_ID", None)
                        or getattr(src, "ned_ID", None)
                        else "Catalog entry for the selected target."
                    ),
                )
            )
    except Exception:
        pass

    if query.isdigit():
        try:
            matching_observations = list(
                Observation.objects.filter(obsid=int(query))
                .filter(reject="N")
                .order_by("id")
                .all()
            )
            for observation in matching_observations:
                source = None
                if observation.srcid is not None:
                    try:
                        source = Source.objects.filter(srcid=observation.srcid).first()
                    except Exception:
                        source = None
                target_name = (
                    getattr(source, "source_name", None)
                    or getattr(source, "simbad_ID", None)
                    or getattr(source, "ned_ID", None)
                    or observation.target
                    or str(observation.obsid)
                )
                results.append(
                    {
                        "object_name": target_name,
                        "observation_id": observation.obsid,
                        "instrument": getattr(observation, "instrument", "N/A"),
                        "source_id": getattr(observation, "srcid", None),
                        "summary": f"Observation {observation.obsid} matches the requested observation ID.",
                    }
                )
        except Exception:
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
    """Render the landing page for the TGCat catalog frontend.

    Parameters
    ----------
    request : HttpRequest
        Incoming HTTP request.

    Returns
    -------
    HttpResponse
        Rendered home page with default empty search values.
    """
    return render(request, "catalog/home.html", {"query": "", "radius": 0.1})


def search(request):
    """Handle the catalog search page and prepare the result list for rendering.

    Parameters
    ----------
    request : HttpRequest
        Incoming search request with query and optional radius parameters.

    Returns
    -------
    HttpResponse
        Search results page rendered with the user query, radius, and matches.
    """
    query = (request.GET.get("q") or "").strip()
    radius_raw = request.GET.get("radius")
    try:
        radius = float(radius_raw) if radius_raw not in (None, "") else 0.1
    except (TypeError, ValueError):
        radius = 0.1
    results = _search_results(query, radius_deg=radius)
    return render(
        request,
        "catalog/search.html",
        {"query": query, "radius": radius, "results": results},
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


def observation_detail(request, obsid):
    """Render a single accepted observation with metadata and product links.

    Parameters
    ----------
    request : HttpRequest
        Incoming request for a specific observation.
    obsid : int
        Observation identifier to fetch.

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

    source = None
    if observation.srcid is not None:
        try:
            source = Source.objects.filter(srcid=observation.srcid).first()
        except Exception:
            source = None

    files = []
    try:
        file_rows = (
            FileRecord.objects.filter(id=observation.id)
            .exclude(value__isnull=True)
            .exclude(value="")
            .order_by("type", "value")
        )
        for row in file_rows:
            if row.name != "file":
                continue
            files.append(
                {
                    "name": row.value,
                    "type": row.type,
                    "arc": row.arc or "-",
                    "size": row.size,
                }
            )
    except Exception:
        files = []

    if not files:
        files = [
            {"name": f"product_{obsid}", "type": "unknown", "arc": "-", "size": None}
        ]

    source_label = (
        getattr(source, "source_name", None)
        or getattr(source, "simbad_ID", None)
        or getattr(source, "ned_ID", None)
        or observation.target
        or "Unknown target"
    )
    source_url = None
    if source and getattr(source, "simbad_ID", None):
        if str(source.simbad_ID) != "NO_AUTO_MATCH":
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
            "ra": observation.ra_targ
            if observation.ra_targ is not None
            else observation.ra,
            "decl": observation.dec_targ
            if observation.dec_targ is not None
            else observation.decl,
        },
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
    try:
        source = Source.objects.filter(source_name__iexact=object_name).first()
        if source is None:
            source = Source.objects.filter(simbad_ID__iexact=object_name).first()
        if source is None:
            source = Source.objects.filter(ned_ID__iexact=object_name).first()
    except Exception:
        source = None

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
