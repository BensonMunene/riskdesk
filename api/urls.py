from django.urls import path

from . import views

urlpatterns = [
    path("", views.api_root, name="api_root"),
    path("portfolios/", views.portfolio_list),
    path("portfolios/<int:pk>/", views.portfolio_summary),
    path("portfolios/<int:pk>/positions/", views.portfolio_positions),
    path("portfolios/<int:pk>/risk/", views.portfolio_risk),
    path("portfolios/<int:pk>/factors/", views.portfolio_factors),
    path("portfolios/<int:pk>/var/", views.portfolio_var),
    path("portfolios/<int:pk>/stress/", views.portfolio_stress),
    path("portfolios/<int:pk>/limits/", views.portfolio_limits),
    path("portfolios/<int:pk>/insights/", views.portfolio_insights),
    path("portfolios/<int:pk>/liquidity/", views.portfolio_liquidity),
    path("portfolios/<int:pk>/correlation/", views.portfolio_correlation),
    path("portfolios/<int:pk>/whatif/", views.portfolio_whatif),
    path("portfolios/<int:pk>/optimize/", views.portfolio_optimize),
    path("assets/", views.asset_list),
    path("prices/<str:ticker>/", views.prices),
]
