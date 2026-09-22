from django.urls import path

from .views import construction, market, overview, portfolio, reports, risk, trades

urlpatterns = [
    path("", overview.home, name="home"),
    path("market/", market.universe, name="market"),
    path("market/<str:ticker>/", market.asset_detail, name="asset_detail"),

    path("portfolios/new/", portfolio.portfolio_create, name="portfolio_create"),
    path("p/<int:pk>/edit/", portfolio.portfolio_edit, name="portfolio_edit"),
    path("p/<int:pk>/delete/", portfolio.portfolio_delete, name="portfolio_delete"),
    path("p/<int:pk>/positions/", portfolio.positions, name="positions"),
    path("p/<int:pk>/positions/import/", portfolio.positions_import, name="positions_import"),
    path("p/<int:pk>/positions/<int:pos_id>/delete/", portfolio.position_delete, name="position_delete"),
    path("p/<int:pk>/settings/", portfolio.analytics_settings, name="portfolio_settings"),
    path("p/<int:pk>/limits/", portfolio.limits, name="limits"),
    path("p/<int:pk>/limits/<int:limit_id>/delete/", portfolio.limit_delete, name="limit_delete"),

    path("p/<int:pk>/", overview.overview, name="overview"),
    path("p/<int:pk>/risk/decomposition/", risk.decomposition, name="risk_decomposition"),
    path("p/<int:pk>/risk/factors/", risk.factors, name="risk_factors"),
    path("p/<int:pk>/risk/tail/", risk.tail, name="risk_tail"),
    path("p/<int:pk>/risk/stress/", risk.stress, name="risk_stress"),
    path("p/<int:pk>/risk/liquidity/", risk.liquidity, name="risk_liquidity"),
    path("p/<int:pk>/risk/correlation/", risk.correlation, name="risk_correlation"),
    path("p/<int:pk>/risk/history/", risk.history, name="risk_history"),

    path("p/<int:pk>/trades/whatif/", trades.whatif, name="whatif"),
    path("p/<int:pk>/trades/proposals/", trades.proposals, name="proposals"),
    path("p/<int:pk>/trades/proposals/<int:proposal_id>/", trades.proposal_detail, name="proposal_detail"),
    path("p/<int:pk>/trades/proposals/<int:proposal_id>/status/", trades.proposal_status, name="proposal_status"),
    path("p/<int:pk>/trades/proposals/<int:proposal_id>/apply/", trades.proposal_apply, name="proposal_apply"),

    path("p/<int:pk>/construct/optimize/", construction.optimize, name="optimize"),
    path("p/<int:pk>/construct/sizing/", construction.sizing, name="sizing"),
    path("p/<int:pk>/construct/backtest/", construction.backtest, name="backtest"),

    path("p/<int:pk>/report/", reports.risk_report, name="report"),
    path("p/<int:pk>/export/positions.csv", reports.export_positions_csv, name="export_positions"),
    path("p/<int:pk>/export/risk.xlsx", reports.export_risk_xlsx, name="export_risk_xlsx"),
    path("api-docs/", reports.api_docs, name="api_docs"),
]
