from pathlib import Path

from django.conf import settings

from ..models import Observation, Source

__all__ = [
    "file_record_path",
    "format_observation_label",
    "obsid_has_multiple_obi",
    "selected_observation_display_name",
]


def obsid_has_multiple_obi(obsid):
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

def format_observation_label(observation, source_name):
    """Format an observation label consistently across the page and plot."""
    if obsid_has_multiple_obi(observation.obsid):
        return f"ObsID: {observation.obsid} (OBI: {observation.obi}, {source_name})"
    else:
        return f"ObsID: {observation.obsid} ({source_name})"


def selected_observation_display_name(observation_id):
    """Return the shared display label for a selected TGCat row."""
    observation = (
        Observation.objects.filter(id=observation_id).filter(reject="N").first()
    )
    if observation is None:
        return f"(ObsID: {observation_id}, Unknown)"
    source = Source.objects.filter(srcid=observation.srcid).first()
    return format_observation_label(observation, source.source_name)

def file_record_path(observation_id, file_type, value, arc_name=None):
    """Return the resolved file path for a TGCat file record."""
    if not value:
        return None
    if not arc_name:
        arc_name = ""
    arc_name = str(arc_name or "").strip()
    candidate = Path(settings.DATA_FILE_ROOT)
    if arc_name:
        candidate = candidate / arc_name
    candidate = candidate / file_type / f"tgcid_{observation_id}_{value}"
    resolved = candidate.resolve()
    if resolved.exists() and resolved.is_file():
        return resolved
    return None
