from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404
from django.shortcuts import render

from .models import Observation, Source


def _search_results(query: str):
    if not query:
        return []

    try:
        objects = (
            Source.objects.filter(source_name__icontains=query)
            | Source.objects.filter(simbad_ID__icontains=query)
            | Source.objects.filter(ned_ID__icontains=query)
        )[:10]
        results = []
        for source in objects:
            obs = Observation.objects.filter(srcid=source.srcid).order_by("obsid").first()
            results.append(
                {
                    "object_name": source.source_name or source.simbad_ID or source.ned_ID or str(source.srcid),
                    "observation_id": obs.obsid if obs else "N/A",
                    "instrument": obs.instrument if obs else "N/A",
                    "summary": f"Catalog entry for {source.source_name or source.simbad_ID or source.ned_ID}.",
                }
            )
        return results
    except Exception:
        return []


def home(request):
    return render(request, "catalog/home.html", {"query": ""})


def search(request):
    query = (request.GET.get("q") or "").strip()
    results = _search_results(query)
    return render(request, "catalog/search.html", {"query": query, "results": results})


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
        observation_ids = list(Observation.objects.filter(srcid=source.srcid).values_list("obsid", flat=True)[:5])
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
