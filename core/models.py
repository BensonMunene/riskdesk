from django.conf import settings as dj_settings
from django.db import models


class Asset(models.Model):
    ASSET_CLASSES = [
        ("Equity", "Equity"), ("ETF", "ETF"), ("Fixed Income", "Fixed Income"),
        ("Commodity", "Commodity"), ("Currency", "Currency"), ("Other", "Other"),
    ]
    ticker = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=120, blank=True)
    asset_class = models.CharField(max_length=30, choices=ASSET_CLASSES, default="Equity")
    sector = models.CharField(max_length=60, blank=True)
    industry = models.CharField(max_length=80, blank=True)
    country = models.CharField(max_length=40, blank=True, default="United States")
    currency = models.CharField(max_length=8, default="USD")
    market_cap = models.FloatField(null=True, blank=True)
    is_factor_etf = models.BooleanField(default=False, help_text="Used to build the ETF factor model")

    class Meta:
        ordering = ["ticker"]

    def __str__(self):
        return self.ticker


class Price(models.Model):
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="prices")
    date = models.DateField()
    close = models.FloatField()
    volume = models.BigIntegerField(default=0)

    class Meta:
        unique_together = [("asset", "date")]
        indexes = [models.Index(fields=["date"])]
        ordering = ["date"]


class Portfolio(models.Model):
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    owner = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    base_currency = models.CharField(max_length=8, default="USD")
    cash = models.FloatField(default=0.0, help_text="Cash balance; negative means borrowed")
    benchmark = models.CharField(max_length=20, default="SPY")
    settings = models.JSONField(default=dict, blank=True,
                                help_text="Overrides for analytics: lookback_days, cov_method, var_confidence, target_beta ...")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def analytics_settings(self) -> dict:
        base = dict(dj_settings.RISKDESK_DEFAULTS)
        base.update(self.settings or {})
        base["benchmark"] = self.benchmark or base.get("benchmark", "SPY")
        return base


class Position(models.Model):
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name="positions")
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT)
    quantity = models.FloatField(help_text="Shares/units; negative = short")
    avg_cost = models.FloatField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("portfolio", "asset")]
        ordering = ["asset__ticker"]

    def __str__(self):
        return f"{self.portfolio}: {self.asset} x {self.quantity:g}"


class RiskLimit(models.Model):
    METRICS = [
        ("gross_exposure", "Gross exposure (% NAV)"),
        ("net_exposure", "Net exposure (% NAV)"),
        ("long_exposure", "Long exposure (% NAV)"),
        ("short_exposure", "Short exposure (% NAV)"),
        ("position_weight", "Single position |weight| (% NAV) - scope: ticker or blank for max"),
        ("sector_gross", "Sector gross exposure (% NAV) - scope: sector"),
        ("sector_net", "Sector net exposure (% NAV) - scope: sector"),
        ("beta", "Beta to benchmark"),
        ("vol", "Annualised volatility"),
        ("var", "Value at Risk (% NAV, portfolio confidence/horizon)"),
        ("cvar", "Expected shortfall (% NAV)"),
        ("top5_weight", "Top-5 positions share of gross"),
        ("max_risk_share", "Largest single-name share of volatility"),
        ("effective_n", "Effective number of positions"),
        ("days_to_liquidate", "Days to liquidate (worst, or scope: ticker)"),
        ("factor_beta", "|Factor beta| - scope: factor name"),
        ("stress_loss", "Worst stress scenario loss (% NAV)"),
        ("drawdown", "Current pro-forma drawdown (% NAV)"),
    ]
    OPERATORS = [("lte", "must be <="), ("gte", "must be >=")]
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name="limits")
    metric = models.CharField(max_length=40, choices=METRICS)
    scope = models.CharField(max_length=60, blank=True, help_text="Ticker, sector or factor, depending on metric")
    operator = models.CharField(max_length=3, choices=OPERATORS, default="lte")
    threshold = models.FloatField(help_text="Fractions for percentages (0.25 = 25%)")
    label = models.CharField(max_length=80, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["metric", "scope"]

    def __str__(self):
        return self.display_label

    @property
    def display_label(self):
        if self.label:
            return self.label
        base = dict(self.METRICS).get(self.metric, self.metric).split(" - ")[0]
        return f"{base} [{self.scope}]" if self.scope else base

    def as_dict(self):
        return {"id": self.id, "metric": self.metric, "scope": self.scope or None, "operator": self.operator,
                "threshold": self.threshold, "label": self.display_label}


class TradeProposal(models.Model):
    STATUS = [("draft", "Draft"), ("review", "In review"), ("approved", "Approved"),
              ("rejected", "Rejected"), ("executed", "Executed")]
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name="proposals")
    name = models.CharField(max_length=120)
    rationale = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default="draft")
    created_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    source = models.CharField(max_length=40, blank=True, help_text="manual | optimizer:<method> | sizing")
    snapshot = models.JSONField(default=dict, blank=True, help_text="Before/after metrics captured at creation")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.status})"

    def trades_dict(self) -> dict:
        return {line.asset.ticker: line.quantity_delta for line in self.lines.select_related("asset")}


class TradeLine(models.Model):
    proposal = models.ForeignKey(TradeProposal, on_delete=models.CASCADE, related_name="lines")
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT)
    quantity_delta = models.FloatField(help_text="Shares to buy (+) or sell (-)")
    limit_price = models.FloatField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["asset__ticker"]


class OptimizationRun(models.Model):
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name="optimization_runs")
    method = models.CharField(max_length=40)
    params = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class RiskSnapshot(models.Model):
    """Daily record of headline risk numbers so the CIO can see how risk has evolved."""
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name="snapshots")
    as_of = models.DateField()
    metrics = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("portfolio", "as_of")]
        ordering = ["as_of"]
