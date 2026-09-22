from django import forms

from analytics.backtest import REBALANCE_FREQ, STRATEGIES
from analytics.covariance import COV_METHODS
from analytics.factors import FACTOR_SPEC
from analytics.optimize import METHODS, RETURN_MODELS
from core.models import Asset, Portfolio, Position, RiskLimit


class PortfolioForm(forms.ModelForm):
    class Meta:
        model = Portfolio
        fields = ["name", "description", "benchmark", "cash"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


class AnalyticsSettingsForm(forms.Form):
    lookback_days = forms.IntegerField(min_value=60, max_value=2000, initial=504,
                                       help_text="Trading days used for covariance, factor regression and VaR")
    cov_method = forms.ChoiceField(choices=list(COV_METHODS.items()), initial="ledoit_wolf")
    ewma_halflife = forms.IntegerField(min_value=5, max_value=500, initial=60)
    var_confidence = forms.ChoiceField(choices=[("0.95", "95%"), ("0.975", "97.5%"), ("0.99", "99%")], initial="0.99")
    var_horizon_days = forms.IntegerField(min_value=1, max_value=20, initial=1)
    risk_free_rate = forms.FloatField(initial=0.04, help_text="Annual, as a fraction")
    liquidity_participation = forms.FloatField(min_value=0.01, max_value=1.0, initial=0.20,
                                               help_text="Share of ADV assumed tradable per day")
    target_beta = forms.FloatField(required=False, help_text="Leave blank if the mandate has no beta target")


class PositionForm(forms.ModelForm):
    ticker = forms.CharField(max_length=20, help_text="Must exist in the price universe (see Market)")

    class Meta:
        model = Position
        fields = ["quantity", "avg_cost", "note"]

    def clean_ticker(self):
        t = self.cleaned_data["ticker"].strip().upper()
        if not Asset.objects.filter(ticker=t).exists():
            raise forms.ValidationError(f"{t} is not in the universe. Load it with `manage.py fetch_prices --tickers {t}`.")
        return t


class PositionImportForm(forms.Form):
    file = forms.FileField(help_text="CSV with columns: ticker, quantity (negative = short). Optional: avg_cost, note")
    replace = forms.BooleanField(required=False, initial=False, label="Replace existing positions")


class RiskLimitForm(forms.ModelForm):
    class Meta:
        model = RiskLimit
        fields = ["metric", "scope", "operator", "threshold", "label", "is_active"]


class WhatIfForm(forms.Form):
    """The trade rows are posted as parallel arrays (ticker[], mode[], value[]) and parsed in the view."""
    name = forms.CharField(required=False, max_length=120)
    rationale = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


METHOD_CHOICES = [(k, v) for k, v in METHODS.items()]


class OptimizeForm(forms.Form):
    method = forms.ChoiceField(choices=METHOD_CHOICES, initial="max_sharpe")
    return_model = forms.ChoiceField(choices=list(RETURN_MODELS.items()), initial="shrunk")
    universe = forms.ChoiceField(choices=[("holdings", "Current holdings only"), ("holdings_plus", "Holdings + candidate tickers")],
                                 initial="holdings")
    candidates = forms.CharField(required=False, help_text="Comma-separated tickers to consider adding")
    long_only = forms.BooleanField(required=False, initial=True)
    budget = forms.FloatField(initial=1.0, help_text="Target net exposure (1.0 = fully invested)")
    w_min = forms.FloatField(initial=0.0, help_text="Lower bound per asset (negative allows shorts)")
    w_max = forms.FloatField(initial=0.25, help_text="Upper bound per asset")
    gross_max = forms.FloatField(required=False, help_text="Max gross exposure (blank = none)")
    sector_max = forms.FloatField(required=False, help_text="Max net weight per sector (blank = none)")
    turnover_max = forms.FloatField(required=False, help_text="Max one-way turnover vs current (blank = none)")
    max_vol = forms.FloatField(required=False, help_text="Cap on annualised vol (blank = none)")
    risk_aversion = forms.FloatField(initial=5.0)
    target = forms.FloatField(required=False, help_text="Target return or vol for the target_* methods")
    keep_hedges = forms.BooleanField(required=False, initial=True, label="Keep ETF hedges/overlays fixed at current weight")
    bl_views = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}),
                               help_text="Black-Litterman views, one per line: `AAPL 0.10 0.6` (absolute) or `AAPL-MSFT 0.03 0.5` (relative)")


class SizingForm(forms.Form):
    target_vol = forms.FloatField(initial=0.10, help_text="Annualised portfolio vol target")
    kelly_fraction = forms.FloatField(initial=0.5)
    return_model = forms.ChoiceField(choices=list(RETURN_MODELS.items()), initial="shrunk")
    new_ticker = forms.CharField(required=False, help_text="Size a new position (optional)")
    new_risk_share = forms.FloatField(initial=0.05, help_text="Desired share of portfolio vol for the new position")


class BacktestForm(forms.Form):
    strategies = forms.MultipleChoiceField(choices=list(STRATEGIES.items()),
                                           initial=["current", "equal_weight", "risk_parity", "min_variance"],
                                           widget=forms.CheckboxSelectMultiple)
    rebalance = forms.ChoiceField(choices=list(REBALANCE_FREQ.items()), initial="M")
    lookback = forms.IntegerField(initial=252, min_value=60, max_value=1000)
    cost_bps = forms.FloatField(initial=10.0)
    w_max = forms.FloatField(initial=0.25)
    start = forms.DateField(required=False, help_text="Backtest start (blank = earliest possible)",
                            widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    long_only = forms.BooleanField(required=False, initial=True)


class ShockForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in FACTOR_SPEC:
            self.fields[f] = forms.FloatField(required=False, label=f, initial=0.0,
                                              widget=forms.NumberInput(attrs={"step": "0.01", "class": "form-control form-control-sm"}))
