from django.urls import path

from . import views

app_name = "trading"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("positions/", views.positions, name="positions"),
    path("strategy/", views.strategy, name="strategy"),
    path("strategy/toggle/", views.toggle_auto, name="toggle_auto"),
    path("strategy/scan/", views.scan_now, name="scan_now"),
    path("strategy/run/", views.run_now, name="run_now"),
    path("strategy/liquidate/", views.liquidate_now, name="liquidate_now"),
    path("holdings/<int:pk>/close/", views.position_close, name="position_close"),
    path("observe/<int:pk>/remove/", views.observe_remove, name="observe_remove"),
    path("chart/<str:symbol>.json", views.chart, name="chart"),
    path("accounts/connect/", views.connect_brokerage, name="connect_brokerage"),
    path("accounts/connect/callback/", views.connect_callback, name="connect_callback"),
    path("accounts/sync/", views.sync_accounts, name="sync_accounts"),
]
