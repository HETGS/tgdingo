# Copyright (C) 2026  MIT - see LICENSE.md
from django.urls import include, path

urlpatterns = [
    path("", include("catalog.urls")),
]
