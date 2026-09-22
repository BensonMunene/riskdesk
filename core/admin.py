from django.contrib import admin

from .models import Asset, OptimizationRun, Portfolio, Position, Price, RiskLimit, RiskSnapshot, TradeLine, TradeProposal


class PositionInline(admin.TabularInline):
    model = Position
    extra = 0
    autocomplete_fields = ["asset"]


class LimitInline(admin.TabularInline):
    model = RiskLimit
    extra = 0


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("ticker", "name", "asset_class", "sector", "industry", "is_factor_etf")
    list_filter = ("asset_class", "sector", "is_factor_etf")
    search_fields = ("ticker", "name")


@admin.register(Price)
class PriceAdmin(admin.ModelAdmin):
    list_display = ("asset", "date", "close", "volume")
    list_filter = ("asset",)
    date_hierarchy = "date"


@admin.register(Portfolio)
class PortfolioAdmin(admin.ModelAdmin):
    list_display = ("name", "benchmark", "cash", "updated_at")
    inlines = [PositionInline, LimitInline]


class TradeLineInline(admin.TabularInline):
    model = TradeLine
    extra = 0
    autocomplete_fields = ["asset"]


@admin.register(TradeProposal)
class TradeProposalAdmin(admin.ModelAdmin):
    list_display = ("name", "portfolio", "status", "source", "created_at")
    list_filter = ("status", "portfolio")
    inlines = [TradeLineInline]


admin.site.register(OptimizationRun)
admin.site.register(RiskSnapshot)
admin.site.site_header = "RiskDesk administration"
