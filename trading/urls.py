from django.urls import path

from . import views

app_name = "trading"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("positions/", views.positions, name="positions"),
    path("strategies/", views.strategies, name="strategies"),
    path("strategies/<int:pk>/edit/", views.strategy_edit, name="strategy_edit"),
    path("strategies/<int:pk>/toggle/", views.strategy_toggle, name="strategy_toggle"),
    path("strategies/<int:pk>/delete/", views.strategy_delete, name="strategy_delete"),
    path("strategies/<int:pk>/run/", views.strategy_run, name="strategy_run"),
    path("strategies/<int:pk>/chart.json", views.strategy_chart, name="strategy_chart"),
    path("accounts/connect/", views.connect_brokerage, name="connect_brokerage"),
    path("accounts/connect/callback/", views.connect_callback, name="connect_callback"),
    path("accounts/sync/", views.sync_accounts, name="sync_accounts"),
]
