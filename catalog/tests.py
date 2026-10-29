# Copyright (C) 2026  MIT - see LICENSE.md
import datetime
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, call, patch

from astropy import units as u
from astropy.coordinates import SkyCoord
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from catalog.forms import PlotSelectionForm

OBS_DATE = datetime.datetime(2024, 1, 2, 3, 4, 5)


class SecretKeyValidationTests(SimpleTestCase):
    def test_rejects_missing_or_weak_secret_from_environment(self):
        from django.core.exceptions import ImproperlyConfigured

        from tgdingo import settings

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                settings.get_required_secret("TGDINGO_SECRET_KEY")

        with patch.dict("os.environ", {"TGDINGO_SECRET_KEY": "short"}, clear=True):
            with self.assertRaises(ImproperlyConfigured):
                settings.get_required_secret("TGDINGO_SECRET_KEY")


class FakeQuerySet:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def exclude(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, item):
        data = list(self.rows)
        if isinstance(item, slice):
            return FakeQuerySet(data[item])
        return data[item]

    def __or__(self, other):
        return FakeQuerySet(list(self.rows) + list(other))


class SpectralPlotUnitTests(SimpleTestCase):
    def test_setup_spectra_caches_pha_and_arf_loading(self):
        from catalog.utils import specta

        spectra = [
            SimpleNamespace(
                header={"TG_M": -1, "TG_PART": 3, "INSTRUME": "HRC-S"},
                set_response=MagicMock(),
            ),
            SimpleNamespace(
                header={"TG_M": 1, "TG_PART": 3, "INSTRUME": "HRC-S"},
                set_response=MagicMock(),
            ),
        ]
        specta.load_spectra.cache_clear()
        try:
            with (
                patch("catalog.utils.specta.FileRecord.objects.filter") as filter_files,
                patch("catalog.utils.specta.read_pha", return_value=spectra) as read_pha,
                patch(
                    "catalog.utils.specta.read_arf", side_effect=lambda path: path
                ) as read_arf,
                patch("catalog.utils.specta.file_record_path") as file_path,
            ):
                file_rows = filter_files.return_value.filter.return_value.filter.return_value.exclude.return_value.exclude.return_value.exclude.return_value.order_by.return_value
                file_rows.first.return_value = SimpleNamespace(
                    value="pha2.gz", arc="arcA"
                )
                file_path.side_effect = lambda tgcat_id, kind, value, arc: (
                    f"/data/{kind}/tgcid_{tgcat_id}_{value}"
                )
                first_result = specta.load_spectra(2459)
                second_result = specta.load_spectra(2459)

            self.assertIs(first_result, second_result)
            read_pha.assert_called_once()
            self.assertEqual(read_arf.call_count, 16)
            self.assertEqual(spectra[0].set_response.call_count, 8)
            self.assertEqual(spectra[1].set_response.call_count, 8)
        finally:
            specta.load_spectra.cache_clear()

    def test_renderer_rejects_untrusted_grouping_arguments_before_database_access(self):
        from catalog.utils.specta import render_spectral_plot_html

        invalid_arguments = (
            ("group_counts", 0),
            ("group_width", 1.5),
            ("group_snr", float("nan")),
            ("group_snr", float("inf")),
            ("unknown_grouping", 1),
        )
        with patch("catalog.utils.specta.Observation.objects.filter") as mock_filter:
            for method, value in invalid_arguments:
                with self.subTest(method=method, value=value):
                    with self.assertRaises(ValueError):
                        render_spectral_plot_html(123, "wavelength", method, value)

        mock_filter.assert_not_called()

    def test_reset_sherpa_bokeh_whisker_colors_matches_line(self):
        from catalog.utils import bokeh_utils

        class TeeHead:
            def __init__(self):
                self.line_color = "black"
                self.line_alpha = 1

        class Whisker:
            def __init__(self):
                self.line_color = "black"
                self.line_alpha = 1
                self.upper_head = TeeHead()
                self.lower_head = TeeHead()

        class Line:
            def __init__(self, line_color):
                self.line_color = line_color

        first_whisker = Whisker()
        first_line = SimpleNamespace(glyph=Line("red"))
        second_line = SimpleNamespace(glyph=Line("blue"))
        second_whisker = Whisker()

        figure = SimpleNamespace(
            renderers=[first_line, second_line],
            center=[first_whisker, second_whisker],
            above=(),
            below=(),
            left=(),
            right=(),
        )

        bokeh_utils.reset_sherpa_bokeh_whisker_colors(figure)

        self.assertEqual(first_whisker.line_color, "red")
        self.assertEqual(first_whisker.upper_head.line_color, "red")
        self.assertEqual(first_whisker.lower_head.line_color, "red")
        self.assertEqual(second_whisker.line_color, "blue")
        self.assertEqual(second_whisker.upper_head.line_color, "blue")
        self.assertEqual(second_whisker.lower_head.line_color, "blue")

    def test_collect_sherpa_bokeh_whisker_renderers_finds_nested_whiskers(self):
        from catalog.utils import bokeh_utils

        class Whisker:
            pass

        whisker_renderer = SimpleNamespace(glyph=Whisker())
        other_renderer = SimpleNamespace(glyph=object())
        figure = SimpleNamespace(
            renderers=[other_renderer],
            center=[whisker_renderer],
            above=(),
            below=(),
            left=(),
            right=(),
        )

        self.assertEqual(
            bokeh_utils.collect_sherpa_bokeh_whisker_renderers(figure),
            [whisker_renderer],
        )

    def test_collect_plot_renderers_includes_direct_and_nested_renderers(self):
        from catalog.utils import bokeh_utils

        class FakeGlyphRenderer:
            def __init__(self):
                self.data_source = object()

        direct_renderer = FakeGlyphRenderer()
        nested_renderer = FakeGlyphRenderer()
        figure = SimpleNamespace(
            renderers=[direct_renderer],
            center=[nested_renderer],
            above=(),
            below=(),
            left=(),
            right=(),
        )

        self.assertEqual(
            bokeh_utils.collect_plot_renderers(figure),
            [direct_renderer, nested_renderer],
        )

    @patch("catalog.utils.specta.components", return_value=("<div>", "<script></script>"))
    @patch("catalog.utils.specta.sherpa_plot.set_backend")
    @patch("catalog.utils.specta.DataPHAPlot")
    @patch("catalog.utils.specta.load_spectra")
    @patch("catalog.views.Observation.objects.filter")
    def test_plot_overlays_each_spectrum_with_its_matching_responses(
        self,
        mock_observation_filter,
        mock_load_spectra,
        mock_plot_class,
        mock_set_backend,
        mock_components,
    ):
        first_spectrum = SimpleNamespace(
            header={"TG_M": -1, "TG_PART": 1},
            set_response=MagicMock(),
            set_analysis=MagicMock(),
            group_counts=MagicMock(),
        )
        second_spectrum = SimpleNamespace(
            header={"TG_M": 1, "TG_PART": 2},
            set_response=MagicMock(),
            set_analysis=MagicMock(),
            group_counts=MagicMock(),
        )
        mock_observation_filter.return_value.filter.return_value.first.return_value = (
            SimpleNamespace(id=123, obsid=873, obi=1, srcid=1)
        )
        mock_load_spectra.return_value = [first_spectrum, second_spectrum]
        mock_plot = mock_plot_class.return_value

        from catalog import views
        from catalog.utils import specta

        bokeh_figure = SimpleNamespace(
            width=None,
            height=None,
            sizing_mode=None,
            renderers=[],
            legend=SimpleNamespace(click_policy=None),
        )
        legend = SimpleNamespace(click_policy=None)
        bokeh_figure.add_layout = MagicMock(
            side_effect=lambda added_legend, position: setattr(
                bokeh_figure, "legend", added_legend
            )
        )
        mock_plot.plot.side_effect = lambda **kwargs: bokeh_figure.renderers.extend(
            [object(), object()]
        )
        backend_figure = SimpleNamespace(children=[(bokeh_figure, 0, 0)])
        with (
            patch.object(
                specta.sherpa_plot.backend,
                "current_fig",
                backend_figure,
                create=True,
            ),
            patch("catalog.utils.utils.obsid_has_multiple_obi", return_value=True),
            patch.object(specta, "LegendItem") as mock_legend_item,
            patch.object(specta, "Legend", return_value=legend) as mock_legend,
            patch.object(views.Source.objects, "filter") as source_filter,
        ):
            source_filter.return_value.first.return_value = SimpleNamespace(
                source_name="CAPELLA"
            )
            result = views.render_spectral_plot_html(123, "channel", "group_counts", 8)

        self.assertIn("<div>", result)
        self.assertIn("<script", result)
        mock_load_spectra.assert_called_once_with(123)
        self.assertEqual(bokeh_figure.width, 960)
        self.assertEqual(bokeh_figure.height, 540)
        self.assertIsNone(bokeh_figure.sizing_mode)
        self.assertEqual(bokeh_figure.legend.click_policy, "mute")
        self.assertEqual(
            [call_args.kwargs["label"] for call_args in mock_legend_item.call_args_list],
            ["HEG -1", "MEG 1"],
        )
        self.assertEqual(mock_legend.call_count, 1)
        self.assertEqual(bokeh_figure.add_layout.call_count, 2)
        self.assertEqual(
            bokeh_figure.add_layout.call_args_list[0],
            call(ANY, "above"),
        )
        self.assertEqual(
            bokeh_figure.add_layout.call_args_list[1],
            call(legend, "right"),
        )
        mock_components.assert_called_once_with(bokeh_figure)
        plotted_spectra = [call.args[0] for call in mock_plot.prepare.call_args_list]
        self.assertEqual(plotted_spectra, [first_spectrum, second_spectrum])
        self.assertIsNot(plotted_spectra[0], first_spectrum)
        self.assertIsNot(plotted_spectra[1], second_spectrum)
        for pha in plotted_spectra:
            pha.set_analysis.assert_called_once_with("channel")
            pha.group_counts.assert_called_once_with(8)
        first_spectrum.set_analysis.assert_not_called()
        second_spectrum.set_analysis.assert_not_called()
        self.assertEqual(
            mock_plot.plot.call_args_list,
            [
                call(
                    label="HEG -1",
                    linestyle="-",
                    marker=None,
                    overplot=False,
                    xerrorbars=True,
                    yerrorbars=True,
                ),
                call(
                    label="MEG 1",
                    linestyle="-",
                    marker=None,
                    overplot=True,
                    xerrorbars=True,
                    yerrorbars=True,
                ),
            ],
        )
        self.assertEqual(mock_plot.title, "ObsID: 873 (OBI: 1, CAPELLA)")


class PlotSelectionViewTests(SimpleTestCase):
    @patch("catalog.views.render_spectral_plot_html", return_value="<plot>")
    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_replot_form_defaults_to_wavelength_and_posts_selected_mode(
        self, mock_observation_filter, mock_source_filter, mock_render_plot
    ):
        mock_observation_filter.return_value.filter.return_value.first.return_value = (
            SimpleNamespace(id=42, obsid=873, obi=None, srcid=1944, reject="N")
        )
        mock_source_filter.return_value.first.return_value = SimpleNamespace(
            source_name="CAPELLA"
        )

        get_response = self.client.get("/plot-spectrum/?tgcat_ids=42")
        self.assertEqual(get_response.status_code, 200)
        self.assertContains(get_response, "Selected: ObsID: 873 (CAPELLA)")
        self.assertContains(get_response, 'value="wavelength" selected')
        self.assertContains(get_response, 'name="group_value"')
        self.assertContains(get_response, 'name="add_plus_minus_orders"')
        self.assertContains(get_response, 'name="add_heg_meg"')
        self.assertNotContains(get_response, 'name="num"')
        self.assertNotContains(get_response, 'name="min"')
        self.assertNotContains(get_response, 'name="snr"')
        mock_render_plot.assert_called_once_with(
            42, "wavelength", None, None, add_plus_minus_orders=False, add_heg_meg=False
        )

        post_response = self.client.post(
            "/plot-spectrum/", {"tgcat_ids": "42", "analysis": "energy"}
        )
        self.assertEqual(post_response.status_code, 200)
        self.assertContains(post_response, 'value="energy" selected')
        self.assertEqual(
            mock_render_plot.call_args_list,
            [
                call(
                    42,
                    "wavelength",
                    None,
                    None,
                    add_plus_minus_orders=False,
                    add_heg_meg=False,
                ),
                call(
                    42,
                    "energy",
                    None,
                    None,
                    add_plus_minus_orders=False,
                    add_heg_meg=False,
                ),
            ],
        )

        grouped_response = self.client.post(
            "/plot-spectrum/",
            {
                "tgcat_ids": "42",
                "analysis": "channel",
                "grouping_method": "group_snr",
                "group_value": "0.75",
                "add_heg_meg": "on",
            },
        )
        self.assertEqual(grouped_response.status_code, 200)
        mock_render_plot.assert_called_with(
            42,
            "channel",
            "group_snr",
            0.75,
            add_plus_minus_orders=True,
            add_heg_meg=True,
        )

        call_count = mock_render_plot.call_count
        invalid_response = self.client.post(
            "/plot-spectrum/",
            {
                "tgcat_ids": "42",
                "analysis": "channel",
                "grouping_method": "group_counts",
                "group_value": "0",
            },
        )
        self.assertEqual(invalid_response.status_code, 200)
        self.assertContains(invalid_response, "num must be a positive integer")
        self.assertEqual(mock_render_plot.call_count, call_count)

        multi_response = self.client.post(
            "/plot-spectrum/",
            {"tgcat_ids": ["42", "43"], "analysis": "channel"},
        )
        self.assertEqual(multi_response.status_code, 200)
        self.assertContains(multi_response, 'name="tgcat_ids" value="42"')
        self.assertContains(multi_response, 'name="tgcat_ids" value="43"')
        mock_render_plot.assert_called_with(
            [42, 43],
            "channel",
            None,
            None,
            add_plus_minus_orders=False,
            add_heg_meg=False,
        )


class GroupingFormTests(SimpleTestCase):
    def test_grouping_methods_validate_their_parameter(self):
        valid_inputs = (
            {"grouping_method": "group_adapt", "group_value": "5"},
            {"grouping_method": "group_adapt_snr", "group_value": "1"},
            {"grouping_method": "group_bins", "group_value": "10"},
            {"grouping_method": "group_counts", "group_value": "20"},
            {"grouping_method": "group_snr", "group_value": "2.5"},
            {"grouping_method": "group_width", "group_value": "4"},
        )
        for grouping in valid_inputs:
            with self.subTest(grouping=grouping):
                form = PlotSelectionForm({"analysis": "wavelength", **grouping})
                self.assertTrue(form.is_valid(), form.errors)

    def test_grouping_parameters_must_be_positive(self):
        invalid_inputs = (
            {"grouping_method": "group_adapt", "group_value": "0"},
            {"grouping_method": "group_counts", "group_value": "-1"},
            {"grouping_method": "group_counts", "group_value": "1.5"},
            {"grouping_method": "group_snr", "group_value": "0"},
            {"grouping_method": "group_snr", "group_value": "-0.5"},
            {"grouping_method": "group_snr", "group_value": "nan"},
            {"grouping_method": "group_snr", "group_value": "inf"},
            {"grouping_method": "group_snr", "group_value": "not-a-number"},
        )
        for grouping in invalid_inputs:
            with self.subTest(grouping=grouping):
                form = PlotSelectionForm({"analysis": "wavelength", **grouping})
                self.assertFalse(form.is_valid())

    def test_selected_method_requires_its_parameter(self):
        form = PlotSelectionForm(
            {"analysis": "energy", "grouping_method": "group_bins"}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("group_value", form.errors)

    def test_heg_meg_checkbox_enables_plus_minus_orders(self):
        form = PlotSelectionForm({"analysis": "wavelength", "add_heg_meg": True})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.cleaned_data["add_heg_meg"])
        self.assertTrue(form.cleaned_data["add_plus_minus_orders"])


class CatalogSmokeTests(TestCase):
    def test_home_page_loads(self):
        response = self.client.get(reverse("catalog:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TGCat")

    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.SkyCoord.from_name")
    def test_home_page_search_loads(self, mock_from_name, mock_source_filter):
        mock_from_name.side_effect = ValueError("not found")
        mock_source_filter.return_value = FakeQuerySet([])

        response = self.client.get(reverse("catalog:home") + "?q=cygnus")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Find targets and observations")
        self.assertContains(response, "Showing matches for")

    def test_help_page_loads(self):
        response = self.client.get(reverse("catalog:help"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Example queries")

    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.FileRecord.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_bulk_download_selection_page_lists_selected_products(
        self, mock_obs_filter, mock_file_filter, mock_source_filter
    ):
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            SimpleNamespace(
                id=101,
                obsid=10,
                srcid=1944,
                instrument="ACIS",
                grating="NONE",
                obi=1,
                exposure=100.0,
            ),
            SimpleNamespace(
                id=202,
                obsid=11,
                srcid=1945,
                instrument="LETGS",
                grating="HETG",
                obi=2,
                exposure=200.0,
            ),
        ]
        mock_source_filter.return_value.first.side_effect = [
            SimpleNamespace(
                srcid=1944,
                source_name="CAPELLA",
                simbad_ID="Capella",
                ned_ID=None,
            ),
            SimpleNamespace(
                srcid=1945,
                source_name="CASTOR",
                simbad_ID="Castor",
                ned_ID=None,
            ),
            SimpleNamespace(
                srcid=1944,
                source_name="CAPELLA",
                simbad_ID="Capella",
                ned_ID=None,
            ),
            SimpleNamespace(
                srcid=1945,
                source_name="CASTOR",
                simbad_ID="Castor",
                ned_ID=None,
            ),
        ]
        file_queryset = mock_file_filter.return_value
        file_queryset.filter.return_value.exclude.return_value.exclude.return_value.order_by.return_value.all.return_value = []

        response = self.client.get(
            reverse("catalog:download_selection") + "?obsids=10&obsids=11"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ARF, RMF, PHA2")
        self.assertContains(response, "light curves")
        self.assertContains(response, "summary plots")
        self.assertContains(response, "Choose one or more file groups for the selected observations.")
        self.assertNotContains(response, "evt2_10.fits")
        self.assertNotContains(response, "pha_11.fits")

        response = self.client.get(
            reverse("catalog:download_selection") + "?tgcat_ids=10&tgcat_ids=11"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Selected:")
        self.assertContains(response, "CAPELLA")
        self.assertContains(response, "CASTOR")

    @patch(
        "catalog.views.render_spectral_plot_html", return_value="<div>Bokeh plot</div>"
    )
    @patch("catalog.views.FileRecord.objects.filter")
    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_plot_selection_page_renders_plot_for_single_selected_observation(
        self,
        mock_obs_filter,
        mock_source_filter,
        mock_file_filter,
        mock_render_plot,
    ):
        mock_obs_filter.return_value.filter.return_value.first.return_value = (
            SimpleNamespace(
                id=42,
                obsid=873,
                srcid=1944,
                obi=4,
                target="CAPELLA",
                reject="N",
            )
        )
        mock_source_filter.return_value.first.return_value = SimpleNamespace(
            srcid=1944,
            source_name="CAPELLA",
        )
        file_queryset = mock_file_filter.return_value
        file_queryset.filter.return_value.exclude.return_value.exclude.return_value.order_by.return_value.all.return_value = [
            SimpleNamespace(type="pha2", name="file", arc="arcA", value="pha2.gz"),
            SimpleNamespace(type="arf", name="file", arc="arcA", value="leg_-1.arf.gz"),
            SimpleNamespace(type="rmf", name="file", arc="arcA", value="leg_-1.rmf.gz"),
        ]

        response = self.client.get(
            reverse("catalog:plot_spectrum") + "?tgcat_ids=42"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bokeh plot")
        self.assertContains(response, "Selected: ObsID: 873 (CAPELLA)")
        self.assertContains(response, 'value="wavelength" selected')
        mock_render_plot.assert_called_once_with(
            42,
            "wavelength",
            None,
            None,
            add_plus_minus_orders=False,
            add_heg_meg=False,
        )

        response = self.client.post(
            reverse("catalog:plot_spectrum"),
            {"tgcat_ids": "42", "analysis": "channel"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="channel" selected')
        self.assertEqual(
            mock_render_plot.call_args_list,
            [
                call(
                    42,
                    "wavelength",
                    None,
                    None,
                    add_plus_minus_orders=False,
                    add_heg_meg=False,
                ),
                call(
                    42,
                    "channel",
                    None,
                    None,
                    add_plus_minus_orders=False,
                    add_heg_meg=False,
                ),
            ],
        )

    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_plot_selection_page_lists_selected_observations(
        self, mock_obs_filter, mock_source_filter
    ):
        mock_obs_filter.return_value.filter.return_value.first.side_effect = [
            SimpleNamespace(
                id=10,
                obsid=10,
                srcid=1944,
                obi=1,
                target="CAPELLA",
                reject="N",
            ),
            SimpleNamespace(
                id=11,
                obsid=11,
                srcid=1945,
                obi=2,
                target="CASTOR",
                reject="N",
            ),
        ]
        mock_source_filter.return_value.first.side_effect = [
            SimpleNamespace(srcid=1944, source_name="CAPELLA"),
            SimpleNamespace(srcid=1945, source_name="CASTOR"),
        ]

        response = self.client.get(
            reverse("catalog:plot_spectrum") + "?tgcat_ids=10&tgcat_ids=11"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Plot selected observations")
        self.assertContains(response, "ObsID: 10")
        self.assertContains(response, "ObsID: 11")
        self.assertContains(response, "Ready to plot")

    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_search_finds_observation_by_obsid(
        self, mock_obs_filter, mock_source_filter
    ):
        observation = SimpleNamespace(
            id=1,
            obsid=873,
            target="CAPELLA",
            srcid=1944,
            instrument="ACIS",
            grating="NONE",
            obi=3,
            exposure=123.45,
            date_obs=OBS_DATE,
            reject="N",
        )
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            observation
        ]
        mock_source_filter.return_value.first.return_value = SimpleNamespace(
            srcid=1944,
            source_name="CAPELLA",
            simbad_ID="Capella",
            ned_ID=None,
            ra=79.17237,
            decl=45.99796,
            reject="N",
        )

        response = self.client.get(reverse("catalog:home") + "?q=873")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CAPELLA")
        self.assertContains(response, "873")
        self.assertContains(response, "View observation")

    @patch("catalog.views.FileRecord.objects.filter")
    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_observation_detail_page_loads(
        self, mock_obs_filter, mock_source_filter, mock_file_filter
    ):
        observation = SimpleNamespace(
            id=42,
            obsid=873,
            target="CAPELLA",
            srcid=1944,
            instrument="ACIS",
            grating="NONE",
            obi=4,
            readmode="TIMED",
            datamode="FAINT",
            events=12345,
            comments=b"Observation note with plain text output.",
            ra=79.17237,
            decl=45.99796,
            ra_targ=79.17964,
            dec_targ=46.00259,
            exposure=14575.8,
            date_obs=OBS_DATE,
        )
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.first.return_value = observation
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            observation
        ]

        source = SimpleNamespace(
            srcid=1944,
            source_name="CAPELLA",
            simbad_ID="Capella",
            ned_ID=None,
        )
        mock_source_filter.return_value.first.return_value = source

        file_record = SimpleNamespace(
            name="file", type="evt2", arc="arcA", value="evt2.gz", size=12345
        )
        file_queryset = mock_file_filter.return_value
        file_queryset.exclude.return_value.exclude.return_value.order_by.return_value = [
            file_record
        ]

        response = self.client.get(
            reverse("catalog:observation_detail", args=[observation.obsid])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Observation")
        self.assertContains(response, "873")
        self.assertContains(response, "Capella")
        self.assertContains(
            response, "https://simbad.cds.unistra.fr/simbad/sim-basic?Ident=Capella"
        )
        self.assertContains(response, "TIMED")
        self.assertContains(response, "FAINT")
        self.assertContains(response, "12345")
        self.assertContains(response, "Observation note with plain text output.")
        self.assertContains(response, "Images linked to this observation")
        self.assertNotContains(response, "(OBI:")

    @patch("catalog.views.FileRecord.objects.filter")
    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_observation_detail_handles_duplicate_obsid_rows(
        self, mock_obs_filter, mock_source_filter, mock_file_filter
    ):
        first_row = SimpleNamespace(
            id=10,
            obsid=5,
            target="DUPLICATE",
            srcid=999,
            instrument="LETGS",
            grating="HETG",
            obi=1,
            ra=1.0,
            decl=2.0,
            ra_targ=1.1,
            dec_targ=2.1,
            exposure=100.0,
            date_obs=OBS_DATE,
        )
        second_row = SimpleNamespace(
            id=11,
            obsid=5,
            target="DUPLICATE",
            srcid=999,
            instrument="LETGS",
            grating="HETG",
            obi=2,
            ra=3.0,
            decl=4.0,
            ra_targ=3.1,
            dec_targ=4.1,
            exposure=200.0,
            date_obs=OBS_DATE,
        )
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.first.return_value = first_row
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            first_row
        ]
        mock_source_filter.return_value.first.return_value = SimpleNamespace(
            srcid=999,
            source_name="DUPLICATE",
            simbad_ID="DUPLICATE",
            ned_ID=None,
        )
        file_queryset = mock_file_filter.return_value
        file_queryset.exclude.return_value.exclude.return_value.order_by.return_value = []

        response = self.client.get(reverse("catalog:observation_detail", args=[5]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Observation")
        self.assertContains(response, "5")
        self.assertContains(response, "DUPLICATE")

    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.FileRecord.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_detail_route_accepts_obsid_obi_slug_and_defaults_to_first_obi(
        self, mock_obs_filter, mock_file_filter, mock_source_filter
    ):
        rows = [
            SimpleNamespace(
                id=20,
                obsid=5,
                target="DUPLICATE",
                srcid=999,
                instrument="LETGS",
                grating="HETG",
                obi=1,
                ra=1.0,
                decl=2.0,
                ra_targ=1.1,
                dec_targ=2.1,
                exposure=100.0,
                date_obs=OBS_DATE,
            ),
            SimpleNamespace(
                id=21,
                obsid=5,
                target="DUPLICATE",
                srcid=999,
                instrument="LETGS",
                grating="HETG",
                obi=2,
                ra=3.0,
                decl=4.0,
                ra_targ=3.1,
                dec_targ=4.1,
                exposure=200.0,
                date_obs=OBS_DATE,
            ),
        ]
        queryset = MagicMock()
        queryset.first.return_value = rows[0]
        queryset.all.return_value = rows
        queryset.filter.return_value.first.return_value = rows[1]
        queryset.filter.return_value.all.return_value = [rows[1]]
        mock_obs_filter.return_value.filter.return_value.order_by.return_value = (
            queryset
        )
        mock_source_filter.return_value.first.return_value = SimpleNamespace(
            srcid=999,
            source_name="DUPLICATE",
            simbad_ID="DUPLICATE",
            ned_ID=None,
        )
        mock_file_filter.return_value.filter.return_value.exclude.return_value.exclude.return_value.order_by.return_value.all.return_value = []

        default_response = self.client.get(
            reverse("catalog:observation_detail", args=[5])
        )
        self.assertEqual(default_response.status_code, 200)
        self.assertEqual(default_response.context["observation"].obi, 1)

        specific_response = self.client.get(
            reverse("catalog:observation_detail", args=["5-2"])
        )
        self.assertEqual(specific_response.status_code, 200)
        self.assertEqual(specific_response.context["observation"].obi, 2)

    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.FileRecord.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_bulk_download_zips_selected_file_groups(
        self, mock_obs_filter, mock_file_filter, mock_source_filter
    ):
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_root = Path(tmp_dir)
            (data_root / "arcA" / "arf").mkdir(parents=True)
            (data_root / "arcA" / "lc").mkdir(parents=True)
            (data_root / "arcA" / "evt2").mkdir(parents=True)
            (data_root / "arcA" / "arf" / "tgcid_101_arf1.fits").write_bytes(b"arf")
            (data_root / "arcA" / "lc" / "tgcid_101_lc1.fits").write_bytes(b"lc")
            (data_root / "arcA" / "evt2" / "tgcid_101_evt2.fits").write_bytes(b"evt2")

            observation = SimpleNamespace(
                id=101,
                obsid=10,
                target="CAPELLA",
                srcid=1944,
                instrument="ACIS",
                grating="NONE",
                obi=1,
                exposure=100.0,
                date_obs=OBS_DATE,
            )
            mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
                observation
            ]
            mock_source_filter.return_value.first.return_value = SimpleNamespace(
                srcid=1944,
                source_name="CAPELLA",
                simbad_ID="Capella",
                ned_ID=None,
            )
            mock_file_filter.return_value.filter.return_value.exclude.return_value.exclude.return_value.order_by.return_value.all.return_value = [
                SimpleNamespace(
                    name="file", type="arf", value="arf1.fits", arc="arcA", size=3
                ),
                SimpleNamespace(
                    name="file", type="lc", value="lc1.fits", arc="arcA", size=2
                ),
                SimpleNamespace(
                    name="file", type="evt2", value="evt2.fits", arc="arcA", size=4
                ),
            ]

            with patch("django.conf.settings.DATA_FILE_ROOT", new=data_root):
                response = self.client.post(
                    reverse("catalog:download_selection") + "?obsids=10",
                    {"categories": ["ARF, RMF, PHA2", "light curves"]},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "application/zip")
            self.assertIn(
                'attachment; filename="tgcat_selection.zip"',
                response["Content-Disposition"],
            )
            with tempfile.NamedTemporaryFile() as tmp_file:
                tmp_file.write(response.content)
                tmp_file.flush()
                import zipfile

                with zipfile.ZipFile(tmp_file.name) as archive:
                    names = sorted(archive.namelist())
                    self.assertIn("obs_10_tgid_101/tgcid_101_arf1.fits", names)
                    self.assertIn("obs_10_tgid_101/tgcid_101_lc1.fits", names)
                    self.assertNotIn("obs_10_tgid_101/tgcid_101_evt2.fits", names)

    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    @patch("catalog.views.FileRecord.objects.filter")
    def test_observation_detail_shows_preview_gallery(
        self, mock_file_filter, mock_obs_filter, mock_source_filter
    ):
        with tempfile.TemporaryDirectory() as tmp_dir:
            preview_root = Path(tmp_dir)
            (preview_root / "tgcid_6972_summary_flux_overview_thumb.png").write_bytes(
                b"thumb"
            )
            (preview_root / "tgcid_6972_summary_flux_overview.png").write_bytes(b"full")
            (preview_root / "tgcid_9999_summary_flux_overview_thumb.png").write_bytes(
                b"thumb"
            )

            observation = SimpleNamespace(
                id=6972,
                obsid=5,
                target="CAPELLA",
                srcid=1944,
                instrument="ACIS",
                grating="NONE",
                obi=9,
                readmode="TIMED",
                datamode="FAINT",
                events=12345,
                comments=b"Observation note",
                ra=79.17237,
                decl=45.99796,
                ra_targ=79.17964,
                dec_targ=46.00259,
                exposure=14575.8,
                date_obs=OBS_DATE,
            )
            mock_obs_filter.return_value.filter.return_value.order_by.return_value.first.return_value = observation
            mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
                observation
            ]
            mock_source_filter.return_value.first.return_value = SimpleNamespace(
                source_name="CAPELLA",
                simbad_ID="Capella",
            )
            mock_file_filter.return_value.filter.return_value.filter.return_value.exclude.return_value.exclude.return_value.order_by.return_value.all.return_value = [
                SimpleNamespace(value="summary_flux_overview.ps"),
                SimpleNamespace(value="summary_flux_overview_thumb.ps"),
            ]

            with patch("catalog.views.PREVIEW_IMAGE_ROOT", new=preview_root):
                response = self.client.get(
                    reverse("catalog:observation_detail", args=[5])
                )

            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Preview images")
            self.assertContains(response, "tgcid_6972_summary_flux_overview.png")
            self.assertContains(
                response, 'href="/preview/tgcid_6972_summary_flux_overview.png/"'
            )
            self.assertNotContains(
                response, "tgcid_6972_summary_flux_overview_thumb.png"
            )

    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_search_returns_all_matching_obsid_rows(
        self, mock_obs_filter, mock_source_filter
    ):
        first_obs = SimpleNamespace(
            id=301,
            obsid=3,
            target="TRAPEZIUM",
            srcid=2959,
            instrument="ACIS",
            grating="NONE",
            obi=1,
            exposure=10.0,
            date_obs=OBS_DATE,
        )
        second_obs = SimpleNamespace(
            id=302,
            obsid=3,
            target="TRAPEZIUM",
            srcid=1618,
            instrument="ACIS",
            grating="NONE",
            obi=2,
            exposure=20.0,
            date_obs=OBS_DATE,
        )
        third_obs = SimpleNamespace(
            id=303,
            obsid=3,
            target="TRAPEZIUM",
            srcid=2972,
            instrument="ACIS",
            grating="NONE",
            obi=3,
            exposure=30.0,
            date_obs=OBS_DATE,
        )
        fourth_obs = SimpleNamespace(
            id=304,
            obsid=3,
            target="TRAPEZIUM",
            srcid=2970,
            instrument="ACIS",
            grating="NONE",
            obi=4,
            exposure=40.0,
            date_obs=OBS_DATE,
        )
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            first_obs,
            second_obs,
            third_obs,
            fourth_obs,
        ]
        mock_source_filter.return_value.first.return_value = SimpleNamespace(
            srcid=2959,
            source_name="TRAPEZIUM",
            simbad_ID="TRAPEZIUM",
            ned_ID=None,
        )

        response = self.client.get(reverse("catalog:home") + "?q=3")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["results"]), 4)
        self.assertContains(response, "3")
        self.assertContains(response, "TRAPEZIUM")

    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_search_uses_source_target_name_for_obsid_matches(
        self, mock_obs_filter, mock_source_filter
    ):
        first_obs = SimpleNamespace(
            id=701,
            obsid=7,
            target="GENERIC TARGET",
            srcid=1,
            instrument="ACIS",
            grating="NONE",
            obi=1,
            exposure=7.5,
            date_obs=OBS_DATE,
        )
        second_obs = SimpleNamespace(
            id=702,
            obsid=7,
            target="GENERIC TARGET",
            srcid=2,
            instrument="ACIS",
            grating="NONE",
            obi=2,
            exposure=8.5,
            date_obs=OBS_DATE,
        )
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            first_obs,
            second_obs,
        ]
        mock_source_filter.return_value.first.side_effect = [
            SimpleNamespace(
                srcid=101,
                source_name="NGC 1234",
                simbad_ID="NGC 1234",
                ned_ID=None,
                ra=1.0,
                decl=2.0,
                reject="N",
            ),
            SimpleNamespace(
                srcid=102,
                source_name="NGC 5678",
                simbad_ID="NGC 5678",
                ned_ID=None,
                ra=3.0,
                decl=4.0,
                reject="N",
            ),
        ]

        response = self.client.get(reverse("catalog:home") + "?q=7")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "NGC 1234")
        self.assertContains(response, "NGC 5678")
        self.assertNotContains(response, "GENERIC TARGET")

    @patch("catalog.views._coordinate_search_results")
    def test_search_uses_coordinate_search_over_valid_sources(
        self, mock_coordinate_search_results
    ):
        mock_coordinate_search_results.return_value = [
            {
                "name": "NGC 1234",
                "observation_id": 101,
                "instrument": "ACIS",
            },
            {
                "name": "NGC 5678",
                "observation_id": 102,
                "instrument": "LETGS",
            },
        ]

        response = self.client.get(
            reverse("catalog:home") + "?q=165.45593,+-34.70856&radius=0.5"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "NGC 1234")
        self.assertContains(response, "NGC 5678")
        self.assertContains(response, "0.5")

    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_name_search_lists_every_accepted_obsid_for_a_source(
        self, mock_obs_filter, mock_source_filter
    ):
        source = SimpleNamespace(
            srcid=42,
            source_name="TW Hya",
            simbad_ID="TW Hya",
            ned_ID=None,
            ra=165.46625,
            decl=-34.70473056,
            reject="N",
        )
        mock_source_filter.return_value = FakeQuerySet([source])
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            SimpleNamespace(
                id=1001,
                srcid=42,
                obsid=101,
                instrument="ACIS",
                grating="NONE",
                obi=1,
                exposure=101.0,
                date_obs=OBS_DATE,
                reject="N",
            ),
            SimpleNamespace(
                id=1002,
                srcid=42,
                obsid=203,
                instrument="LETGS",
                grating="HETG",
                obi=2,
                exposure=203.0,
                date_obs=OBS_DATE,
                reject="N",
            ),
            SimpleNamespace(
                id=1003,
                srcid=42,
                obsid=999,
                instrument="ACIS",
                grating="NONE",
                obi=3,
                exposure=999.0,
                date_obs=OBS_DATE,
                reject="N",
            ),
        ]

        response = self.client.get(reverse("catalog:home") + "?q=TW Hydrae")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {result["observation_id"] for result in response.context["results"]},
            {101, 203, 999},
        )
        self.assertContains(response, "TW Hya")

    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_digit_only_query_uses_obsid_search_only(
        self, mock_obs_filter, mock_source_filter
    ):
        observation = SimpleNamespace(
            id=42,
            obsid=42,
            target="TW Hya",
            srcid=77,
            instrument="ACIS",
            grating="NONE",
            obi=7,
            exposure=42.0,
            date_obs=OBS_DATE,
        )
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            observation
        ]
        mock_source_filter.return_value.first.return_value = SimpleNamespace(
            srcid=77,
            source_name="TW Hya",
            simbad_ID="TW Hya",
            ned_ID=None,
        )

        response = self.client.get(reverse("catalog:home") + "?q=42")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["results"]), 1)
        self.assertEqual(response.context["results"][0]["observation_id"], 42)
        self.assertContains(response, "TW Hya")

    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_same_obsid_with_different_obi_is_kept_as_separate_result(
        self, mock_obs_filter, mock_source_filter
    ):
        source = SimpleNamespace(
            srcid=77,
            source_name="TW Hya",
            simbad_ID="TW Hya",
            ned_ID=None,
            reject="N",
        )
        mock_source_filter.return_value = FakeQuerySet([source])
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            SimpleNamespace(
                id=1,
                obsid=433,
                srcid=77,
                target="TW Hya",
                instrument="ACIS",
                grating="NONE",
                obi=1,
                exposure=10.0,
                date_obs=OBS_DATE,
                reject="N",
            ),
            SimpleNamespace(
                id=2,
                obsid=433,
                srcid=77,
                target="TW Hya",
                instrument="ACIS",
                grating="NONE",
                obi=2,
                exposure=20.0,
                date_obs=OBS_DATE,
                reject="N",
            ),
            SimpleNamespace(
                id=3,
                obsid=433,
                srcid=77,
                target="TW Hya",
                instrument="ACIS",
                grating="NONE",
                obi=3,
                exposure=30.0,
                date_obs=OBS_DATE,
                reject="N",
            ),
        ]

        response = self.client.get(reverse("catalog:home") + "?q=433")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["results"]), 3)
        self.assertEqual(
            {result["obi"] for result in response.context["results"]},
            {1, 2, 3},
        )

    @patch("catalog.views._coordinate_search_results")
    @patch("catalog.views.SkyCoord.from_name")
    @patch("catalog.views._parse_coordinate")
    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_name_search_matches_coordinate_search_for_tw_hydrae(
        self,
        mock_obs_filter,
        mock_source_filter,
        mock_parse_coordinate,
        mock_from_name,
        mock_coordinate_search_results,
    ):
        source_rows = [
            SimpleNamespace(
                srcid=1000 + index,
                source_name="TW Hya",
                simbad_ID="TW Hya",
                ned_ID=None,
                ra=165.46625 + index * 0.001,
                decl=-34.70473056 - index * 0.001,
                reject="N",
            )
            for index in range(12)
        ]
        mock_source_filter.return_value = FakeQuerySet(source_rows)
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            SimpleNamespace(
                id=2000 + index,
                srcid=1000 + index,
                obsid=2000 + index,
                instrument="ACIS",
                grating="NONE",
                obi=index + 1,
                exposure=float(index + 1),
                date_obs=OBS_DATE,
                reject="N",
            )
            for index in range(12)
        ]

        expected_results = [
            {
                "object_name": "TW Hya",
                "observation_id": 2000 + index,
                "instrument": "ACIS",
                "source_id": 1000 + index,
            }
            for index in range(12)
        ]
        mock_coordinate_search_results.return_value = expected_results
        mock_parse_coordinate.side_effect = [
            None,
            SkyCoord(165.46625, -34.70473, unit=u.deg),
        ]
        mock_from_name.return_value = SkyCoord(165.46625, -34.70473, unit=u.deg)

        name_response = self.client.get(reverse("catalog:home") + "?q=TW Hydrae")
        coord_response = self.client.get(
            reverse("catalog:home") + "?q=11+01+51.9053285064+-34+42+17.033218380"
        )

        self.assertEqual(name_response.status_code, 200)
        self.assertEqual(coord_response.status_code, 200)

        name_obsids = {
            result["observation_id"]
            for result in name_response.context["results"]
            if result.get("observation_id") is not None
        }
        coord_obsids = {
            result["observation_id"]
            for result in coord_response.context["results"]
            if result.get("observation_id") is not None
        }

        self.assertGreaterEqual(len(name_obsids), 11)
        self.assertGreaterEqual(len(coord_obsids), 11)
        self.assertEqual(name_obsids, coord_obsids)
        mock_from_name.assert_called_once_with("TW Hydrae")
        self.assertGreaterEqual(mock_coordinate_search_results.call_count, 1)

    @patch("catalog.views._parse_coordinate")
    @patch("catalog.views.Observation.objects.filter")
    @patch("catalog.views.Source.objects.filter")
    def test_coordinate_search_returns_all_obsids_for_same_target(
        self, mock_source_filter, mock_obs_filter, mock_parse_coordinate
    ):
        mock_parse_coordinate.return_value = SkyCoord(
            165.46625, -34.70473056, unit=u.deg
        )

        source = SimpleNamespace(
            srcid=42,
            source_name="TW Hya",
            simbad_ID="TW Hya",
            ned_ID=None,
            ra=165.46625,
            decl=-34.70473056,
            reject="N",
        )
        mock_source_filter.return_value = FakeQuerySet([source])
        mock_obs_filter.return_value = FakeQuerySet(
            [
                SimpleNamespace(
                    id=1,
                    srcid=42,
                    obsid=5,
                    instrument="ACIS",
                    grating="NONE",
                    obi=1,
                    exposure=5.0,
                    date_obs=OBS_DATE,
                    reject="N",
                ),
                SimpleNamespace(
                    id=2,
                    srcid=42,
                    obsid=6443,
                    instrument="LETGS",
                    grating="HETG",
                    obi=2,
                    exposure=6443.0,
                    date_obs=OBS_DATE,
                    reject="N",
                ),
                SimpleNamespace(
                    id=3,
                    srcid=42,
                    obsid=7435,
                    instrument="ACIS",
                    grating="NONE",
                    obi=3,
                    exposure=7435.0,
                    date_obs=OBS_DATE,
                    reject="N",
                ),
            ]
        )

        results = __import__(
            "catalog.views", fromlist=["_coordinate_search_results"]
        )._coordinate_search_results("11 01 51.90 -34 42 17.03", radius_deg=1.0)

        self.assertEqual(len(results), 3)
        self.assertEqual(
            {result["observation_id"] for result in results}, {5, 6443, 7435}
        )

    @patch("catalog.views._search_results")
    def test_search_page_handles_missing_observation_id(self, mock_search_results):
        mock_search_results.return_value = [
            {
                "object_name": "Target Without Observation",
                "observation_id": "N/A",
                "instrument": "ACIS",
            }
        ]

        response = self.client.get(
            reverse("catalog:home") + "?q=Target Without Observation"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Target Without Observation")
        self.assertContains(response, "Unavailable")
        self.assertNotContains(response, "View observation")
