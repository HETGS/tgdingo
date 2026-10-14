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
