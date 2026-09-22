import math

from django import template

register = template.Library()


def _is_nan(v):
    try:
        return v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))
    except TypeError:
        return True


@register.filter
def pct(value, digits=1):
    """Fraction -> percentage string."""
    if _is_nan(value):
        return "–"
    try:
        return f"{float(value) * 100:.{int(digits)}f}%"
    except (TypeError, ValueError):
        return "–"


@register.filter
def spct(value, digits=1):
    """Signed percentage."""
    if _is_nan(value):
        return "–"
    return f"{float(value) * 100:+.{int(digits)}f}%"


@register.filter
def num(value, digits=2):
    if _is_nan(value):
        return "–"
    try:
        return f"{float(value):,.{int(digits)}f}"
    except (TypeError, ValueError):
        return "–"


@register.filter
def snum(value, digits=2):
    if _is_nan(value):
        return "–"
    return f"{float(value):+,.{int(digits)}f}"


@register.filter
def money(value, digits=0):
    if _is_nan(value):
        return "–"
    v = float(value)
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e9:
        return f"{sign}${v / 1e9:.2f}bn"
    if v >= 1e6:
        return f"{sign}${v / 1e6:.2f}m"
    if v >= 1e4:
        return f"{sign}${v / 1e3:.0f}k"
    return f"{sign}${v:,.{int(digits)}f}"


@register.filter
def money_full(value):
    if _is_nan(value):
        return "–"
    return f"${float(value):,.0f}"


@register.filter
def get_item(d, key):
    if d is None:
        return None
    try:
        return d.get(key)
    except AttributeError:
        return None


@register.filter
def status_badge(status):
    return {"ok": "success", "warn": "warning", "breach": "danger", "n/a": "secondary",
            "high": "danger", "medium": "warning", "low": "info", "info": "secondary"}.get(status, "secondary")


@register.filter
def sign_class(value):
    if _is_nan(value):
        return ""
    return "text-success" if float(value) > 0 else ("text-danger" if float(value) < 0 else "")


@register.filter
def abs_val(value):
    try:
        return abs(float(value))
    except (TypeError, ValueError):
        return value


@register.filter
def mul(value, factor):
    try:
        return float(value) * float(factor)
    except (TypeError, ValueError):
        return ""


@register.filter
def days(value):
    if _is_nan(value):
        return "–"
    v = float(value)
    if v == float("inf") or v > 900:
        return ">250d"
    hours = v * 6.5
    if hours < 1:
        return f"{max(hours * 60, 1):.0f}min"
    if v < 1:
        return f"{hours:.1f}h"
    return f"{v:.1f}d"
