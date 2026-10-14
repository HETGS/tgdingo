from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse


class CatalogSmokeTests(TestCase):
    def test_home_page_loads(self):
        response = self.client.get(reverse("catalog:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TGCat")

    def test_search_page_loads(self):
        response = self.client.get(reverse("catalog:search") + "?q=cygnus")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Find targets and observations")
        self.assertContains(response, "Showing matches for")

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
            reverse("catalog:search") + "?q=Target Without Observation"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Target Without Observation")
        self.assertContains(response, "Unavailable")
        self.assertNotContains(response, "View observation")
