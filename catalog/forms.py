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
    num = forms.IntegerField(
        required=False,
        min_value=1,
        widget=forms.NumberInput(attrs={"min": "1", "step": "1", "id": "group-num"}),
    )
    min = forms.IntegerField(
        required=False,
        min_value=1,
        widget=forms.NumberInput(attrs={"min": "1", "step": "1", "id": "group-min"}),
    )
    snr = forms.FloatField(
        required=False,
        widget=forms.NumberInput(
            attrs={"min": "0", "step": "any", "id": "group-snr"}
        ),
    )

    def clean(self):
        cleaned_data = super().clean()
        method = cleaned_data.get("grouping_method")
        parameter = GROUPING_PARAMETER.get(method)
        if parameter is None:
            return cleaned_data

        value = cleaned_data.get(parameter)
        if parameter in self.errors:
            return cleaned_data
        try:
            validate_grouping(method, value)
        except ValueError as exc:
            self.add_error(parameter, str(exc))
        return cleaned_data
