from pathlib import Path
from urllib.parse import quote_plus

from django.conf import settings
from django.http import FileResponse, Http404
from django.shortcuts import render

from .models import FileRecord, Observation, Source


def _search_results(query: str):
    if not query:
        return []

    query = query.strip()
    results = []

    try:
        objects = (
            Source.objects.filter(source_name__icontains=query)
            | Source.objects.filter(simbad_ID__icontains=query)
            | Source.objects.filter(ned_ID__icontains=query)
        )[:10]
        for source in objects:
            obs = (
                Observation.objects.filter(srcid=source.srcid)
                .filter(reject="N")
                .order_by("obsid")
                .first()
            )
            observation_id = obs.obsid if obs and obs.obsid is not None else None
            results.append(
                {
                    "object_name": source.source_name
                    or source.simbad_ID
                    or source.ned_ID
                    or str(source.srcid),
                    "observation_id": observation_id,
                    "instrument": obs.instrument if obs else "N/A",
                    "source_id": getattr(source, "srcid", None),
                    "summary": (
                        f"Catalog entry for {source.source_name or source.simbad_ID or source.ned_ID}."
                        if source.source_name or source.simbad_ID or source.ned_ID
                        else "Catalog entry for the selected target."
                    ),
                }
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
    return render(request, "catalog/home.html", {"query": ""})


def search(request):
    query = (request.GET.get("q") or "").strip()
    results = _search_results(query)
    return render(request, "catalog/search.html", {"query": query, "results": results})


def _as_plain_text(value):
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def observation_detail(request, obsid):
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
    target = Path(settings.DATA_FILE_ROOT) / filename
    if not target.exists() or not target.is_file():
        raise Http404("Requested data product was not found.")
    return FileResponse(target.open("rb"), as_attachment=True, filename=filename)
