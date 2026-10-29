# Copyright (C) 2026  MIT - see LICENSE.md
from django.urls import path

from . import views

app_name = "catalog"

urlpatterns = [
    path("", views.home, name="home"),
    path("help/", views.help, name="help"),
    path("download-selection/", views.download_selection, name="download_selection"),
    path("plot-spectrum/", views.plot_selection, name="plot_spectrum"),
    path(
        "observation/<slug:obsid_slug>/",
        views.observation_detail,
        name="observation_detail",
    ),
    path("download/<str:filename>/", views.download_product, name="download"),
    path("preview/<str:filename>/", views.preview_image, name="preview_image"),
]
