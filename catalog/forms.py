# Copyright (C) 2026  MIT - see LICENSE.md
import math

from django import forms


GROUPING_METHODS = (
    ("", "No grouping"),
    ("group_counts", "Minimum counts per bin"),
    ("group_width", "Fixed width (n channels)"),
    ("group_bins", "Fixed number of bins (group_bins)"),
    ("group_snr", "Minimum signal-to-noise (group_snr)"),
    ("group_adapt", "Adaptive counts (group_adapt)"),
    ("group_adapt_snr", "Adaptive signal-to-noise (group_adapt_snr)"),
)

GROUPING_PARAMETER = {
    "group_adapt": "min",
    "group_adapt_snr": "min",
    "group_bins": "num",
    "group_counts": "num",
    "group_snr": "snr",
    "group_width": "num",
}


def validate_grouping(method, value):
    """Validate a grouping method and its typed parameter before Sherpa use."""
    if method in (None, ""):
        return None, None
    if method not in GROUPING_PARAMETER:
        raise ValueError("Unsupported grouping method.")

    parameter = GROUPING_PARAMETER[method]
    if parameter in {"num", "min"}:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{parameter} must be a positive integer.")
    elif (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("snr must be a positive finite number.")

    return method, value


class PlotSelectionForm(forms.Form):
    analysis = forms.ChoiceField(
        choices=(
            ("wavelength", "Wavelength"),
            ("channel", "Channel"),
            ("energy", "Energy"),
        ),
        initial="wavelength",
        widget=forms.Select(attrs={"id": "analysis"}),
    )
    grouping_method = forms.ChoiceField(
        choices=GROUPING_METHODS,
        required=False,
        widget=forms.Select(attrs={"id": "grouping-method"}),
    )
    group_value = forms.CharField(
        required=False,
        widget=forms.NumberInput(attrs={"min": "0", "step": "any", "id": "group-value"}),
    )
    add_plus_minus_orders = forms.BooleanField(required=False, initial=False)
    add_heg_meg = forms.BooleanField(required=False, initial=False)

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("add_heg_meg"):
            cleaned_data["add_plus_minus_orders"] = True

        method = cleaned_data.get("grouping_method")
        parameter = GROUPING_PARAMETER.get(method)
        if parameter is None:
            return cleaned_data

        raw_value = cleaned_data.get("group_value")
        if not raw_value:
            self.add_error("group_value", "Enter a value for the selected grouping method.")
            return cleaned_data

        try:
            value = float(raw_value) if parameter == "snr" else int(raw_value)
            validate_grouping(method, value)
        except (TypeError, ValueError, OverflowError) as exc:
            message = str(exc) or (
                "Enter a positive number." if parameter == "snr"
                else "Enter a positive integer."
            )
            self.add_error("group_value", message)
        else:
            cleaned_data["group_value"] = value
        return cleaned_data
