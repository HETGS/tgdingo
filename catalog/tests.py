from types import SimpleNamespace
from unittest.mock import patch

from astropy import units as u
from astropy.coordinates import SkyCoord
from django.test import TestCase
from django.urls import reverse


class CatalogSmokeTests(TestCase):
    def test_home_page_loads(self):
        response = self.client.get(reverse("catalog:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TGCat")

    def test_home_page_search_loads(self):
        response = self.client.get(reverse("catalog:home") + "?q=cygnus")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Find targets and observations")
        self.assertContains(response, "Showing matches for")

    def test_help_page_loads(self):
        response = self.client.get(reverse("catalog:help"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Example queries")

    @patch("catalog.views.FileRecord.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_bulk_download_selection_page_lists_selected_products(
        self, mock_obs_filter, mock_file_filter
    ):
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            SimpleNamespace(id=101, obsid=10, instrument="ACIS"),
            SimpleNamespace(id=202, obsid=11, instrument="LETGS"),
        ]
        file_queryset = mock_file_filter.return_value
        file_queryset.exclude.return_value.exclude.return_value.order_by.return_value = [
            SimpleNamespace(
                name="file", type="evt2", value="evt2_10.fits", arc="arcA", size=123
            ),
            SimpleNamespace(
                name="file", type="pha", value="pha_11.fits", arc="arcB", size=456
            ),
        ]

        response = self.client.get(
            reverse("catalog:download_selection") + "?obsids=10&obsids=11"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "evt2_10.fits")
        self.assertContains(response, "pha_11.fits")

    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_search_finds_observation_by_obsid(
        self, mock_obs_filter, mock_source_filter
    ):
        observation = SimpleNamespace(
            obsid=873,
            target="CAPELLA",
            srcid=1944,
            instrument="ACIS",
        )
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            observation
        ]
        mock_source_filter.return_value.first.return_value = SimpleNamespace(
            source_name="CAPELLA",
            simbad_ID="Capella",
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
            readmode="TIMED",
            datamode="FAINT",
            events=12345,
            notes=b"Observation note with plain text output.",
            ra=79.17237,
            decl=45.99796,
            ra_targ=79.17964,
            dec_targ=46.00259,
            exposure=14575.8,
        )
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.first.return_value = observation

        source = SimpleNamespace(source_name="CAPELLA", simbad_ID="Capella")
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
        self.assertContains(response, "evt2.gz")

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
            ra=1.0,
            decl=2.0,
            ra_targ=1.1,
            dec_targ=2.1,
            exposure=100.0,
        )
        second_row = SimpleNamespace(
            id=11,
            obsid=5,
            target="DUPLICATE",
            srcid=999,
            instrument="LETGS",
            ra=3.0,
            decl=4.0,
            ra_targ=3.1,
            dec_targ=4.1,
            exposure=200.0,
        )
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.first.return_value = first_row
        mock_source_filter.return_value.first.return_value = SimpleNamespace(
            source_name="DUPLICATE"
        )
        file_queryset = mock_file_filter.return_value
        file_queryset.exclude.return_value.exclude.return_value.order_by.return_value = []

        response = self.client.get(reverse("catalog:observation_detail", args=[5]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Observation")
        self.assertContains(response, "5")
        self.assertContains(response, "DUPLICATE")

    @patch("catalog.views.Observation.objects.filter")
    @patch("catalog.views.Source.objects.filter")
    def test_object_detail_only_lists_accepted_observations(
        self, mock_source_filter, mock_obs_filter
    ):
        source = SimpleNamespace(srcid=77, source_name="TW Hya", simbad_ID="TW Hya")
        mock_source_filter.return_value.first.return_value = source

        observation_queryset = mock_obs_filter.return_value
        observation_queryset.filter.return_value.values_list.return_value.__getitem__.return_value = [
            101,
            102,
        ]

        response = self.client.get(reverse("catalog:object_detail", args=["TW Hya"]))

        self.assertEqual(response.status_code, 200)
        observation_queryset.filter.assert_any_call(reject="N")
        self.assertContains(response, "TW Hya")

    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_search_returns_all_matching_obsid_rows(
        self, mock_obs_filter, mock_source_filter
    ):
        first_obs = SimpleNamespace(
            obsid=3,
            target="TRAPEZIUM",
            srcid=2959,
            instrument="ACIS",
        )
        second_obs = SimpleNamespace(
            obsid=3,
            target="TRAPEZIUM",
            srcid=1618,
            instrument="ACIS",
        )
        third_obs = SimpleNamespace(
            obsid=3,
            target="TRAPEZIUM",
            srcid=2972,
            instrument="ACIS",
        )
        fourth_obs = SimpleNamespace(
            obsid=3,
            target="TRAPEZIUM",
            srcid=2970,
            instrument="ACIS",
        )
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            first_obs,
            second_obs,
            third_obs,
            fourth_obs,
        ]
        mock_source_filter.return_value.first.return_value = SimpleNamespace(
            source_name="TRAPEZIUM"
        )

        response = self.client.get(reverse("catalog:home") + "?q=3")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.content.decode("utf-8").count(
                "Observation 3 matches the requested ObsID."
            ),
            4,
        )
        self.assertContains(response, "3")

    @patch("catalog.views.Source.objects.filter")
    @patch("catalog.views.Observation.objects.filter")
    def test_search_uses_source_target_name_for_obsid_matches(
        self, mock_obs_filter, mock_source_filter
    ):
        first_obs = SimpleNamespace(
            obsid=7,
            target="GENERIC TARGET",
            srcid=1,
            instrument="ACIS",
        )
        second_obs = SimpleNamespace(
            obsid=7,
            target="GENERIC TARGET",
            srcid=2,
            instrument="ACIS",
        )
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            first_obs,
            second_obs,
        ]
        mock_source_filter.return_value.first.side_effect = [
            SimpleNamespace(source_name="NGC 1234"),
            SimpleNamespace(source_name="NGC 5678"),
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
                "object_name": "NGC 1234",
                "observation_id": 101,
                "instrument": "ACIS",
                "summary": "Target NGC 1234 is within 0.5° of the searched coordinates.",
            },
            {
                "object_name": "NGC 5678",
                "observation_id": 102,
                "instrument": "LETGS",
                "summary": "Target NGC 5678 is within 0.5° of the searched coordinates.",
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
        class FakeSourceQuerySet(list):
            def __or__(self, other):
                return FakeSourceQuerySet(list(self) + list(other))

        source = SimpleNamespace(
            srcid=42,
            source_name="TW Hya",
            simbad_ID="TW Hya",
            ned_ID=None,
        )
        mock_source_filter.return_value = FakeSourceQuerySet([source])
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            SimpleNamespace(obsid=101, instrument="ACIS"),
            SimpleNamespace(obsid=203, instrument="LETGS"),
            SimpleNamespace(obsid=999, instrument="ACIS"),
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
            obsid=42,
            target="TW Hya",
            srcid=77,
            instrument="ACIS",
        )
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            observation
        ]
        mock_source_filter.return_value.first.return_value = SimpleNamespace(
            source_name="TW Hya"
        )

        response = self.client.get(reverse("catalog:home") + "?q=42")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Observation 42 matches the requested ObsID.")
        self.assertEqual(len(response.context["results"]), 1)
        self.assertEqual(response.context["results"][0]["observation_id"], 42)

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
        class FakeSourceQuerySet(list):
            def __or__(self, other):
                return FakeSourceQuerySet(list(self) + list(other))

            def __getitem__(self, item):
                data = list(self)
                if isinstance(item, slice):
                    return FakeSourceQuerySet(data[item])
                return data[item]

        source_rows = [
            SimpleNamespace(
                srcid=1000 + index,
                source_name="TW Hya",
                simbad_ID="TW Hya",
                ned_ID=None,
            )
            for index in range(12)
        ]
        mock_source_filter.return_value = FakeSourceQuerySet(source_rows)
        mock_obs_filter.return_value.filter.return_value.order_by.return_value.all.return_value = [
            SimpleNamespace(obsid=2000 + index, instrument="ACIS")
            for index in range(12)
        ]

        expected_results = [
            {
                "object_name": "TW Hya",
                "observation_id": 2000 + index,
                "instrument": "ACIS",
                "source_id": 1000 + index,
                "summary": "Catalog entry for TW Hya.",
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
        class FakeQuerySet:
            def __init__(self, rows):
                self.rows = rows

            def filter(self, *args, **kwargs):
                return self

            def exclude(self, *args, **kwargs):
                return self

            def order_by(self, *args, **kwargs):
                return self

            def all(self):
                return self.rows

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
        )
        mock_source_filter.return_value = FakeQuerySet([source])
        mock_obs_filter.return_value = FakeQuerySet(
            [
                SimpleNamespace(obsid=5, instrument="ACIS"),
                SimpleNamespace(obsid=6443, instrument="LETGS"),
                SimpleNamespace(obsid=7435, instrument="ACIS"),
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
                "summary": "No observation record for this target.",
            }
        ]

        response = self.client.get(
            reverse("catalog:home") + "?q=Target Without Observation"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Target Without Observation")
        self.assertContains(response, "Unavailable")
        self.assertNotContains(response, "View observation")
