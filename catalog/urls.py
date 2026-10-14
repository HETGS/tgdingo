from django.urls import path

from . import views

app_name = "catalog"

urlpatterns = [
    path("", views.home, name="home"),
    path("search/", views.search, name="search"),
    path(
        "observation/<int:obsid>/", views.observation_detail, name="observation_detail"
    ),
    path("object/<str:object_name>/", views.object_detail, name="object_detail"),
    path("download/<str:filename>/", views.download_product, name="download"),
]
