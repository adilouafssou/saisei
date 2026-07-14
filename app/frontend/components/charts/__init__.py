"""Dependency-free charting primitives (Feature 9 §8 / build-order step 1).

A tiny, **pure-Python** toolkit for building bespoke SVG charts — the proven
pattern behind the recovery curve, extracted so future multi-series views
(Feature 5's 12–36mo P&L bridge, the Feature 8.1 portfolio sparklines) reuse one
correct implementation instead of re-hand-rolling scales and axes each time.

Why not a JS charting library (recharts etc.)?
----------------------------------------------
The hand-built recovery SVG already looks better than library defaults and keeps
the geometry **deterministic and auditable in Python** — which matches the
product's core thesis (every figure is computed and reproducible). A generic
library trades that control for defaults that read as a dashboard template, and
adds a JS dependency + bundle/upgrade surface. So we keep the geometry here.

Design rules (mirror the project's determinism stance)
------------------------------------------------------
- **Pure + deterministic + offline.** No Reflex, no network, stdlib only. Same
  inputs -> same pixel coordinates. This is what makes it unit-testable and what
  keeps the chart numeric-preservation-safe: the toolkit maps already-computed
  figures to pixels, it never invents or rounds a *figure* (only pixel coords).
- **Display-only.** Nothing here computes a domain value, a verdict, or a gate;
  it computes screen geometry from values the deterministic spine produced.
- **Value space stays exact; only pixels are derived.** Callers pass real
  domain values (yen ints, EWS floats); the scales return pixel floats for the
  SVG. The original values are never mutated.

The Reflex components that render these coordinates live elsewhere (e.g.
``recovery_chart.py``); this module is the geometry engine they read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "Bounds",
    "LinearScale",
    "SeriesKind",
    "Series",
    "AxisTick",
    "linear_ticks",
    "build_polyline",
    "build_area_path",
    "build_bars",
    "build_sparkline",
    "sparkline_trend",
    "DeteriorationRow",
    "rank_by_deterioration",
    "DistributionBand",
    "build_band_distribution",
]


@dataclass(frozen=True)
class Bounds:
    """A rectangular plot region in SVG pixel space (the inner drawing area).

    Attributes:
        x0: Left edge (px).
        y0: Top edge (px).
        x1: Right edge (px).
        y1: Bottom edge (px). Note ``y1 > y0`` because SVG y grows downward.
    """

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        """Plot width in px (always >= 0)."""
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        """Plot height in px (always >= 0)."""
        return max(0.0, self.y1 - self.y0)


@dataclass(frozen=True)
class LinearScale:
    """A linear value->pixel mapping over a fixed domain and pixel range.

    The single source of truth for positioning a value on an axis. A degenerate
    domain (``vmin == vmax``) maps everything to the range midpoint rather than
    dividing by zero, so a flat (single-value) series still renders sanely.

    Attributes:
        vmin: Domain minimum (value space).
        vmax: Domain maximum (value space).
        pmin: Pixel coordinate that ``vmin`` maps to.
        pmax: Pixel coordinate that ``vmax`` maps to. For a Y axis pass the
            INVERTED range (``pmin=bottom``, ``pmax=top``) because SVG y grows
            downward — see :meth:`for_y`.
    """

    vmin: float
    vmax: float
    pmin: float
    pmax: float

    def scale(self, value: float) -> float:
        """Map a domain ``value`` to its pixel coordinate (clamped to domain).

        Computed as ``pmax + (pmin - pmax) * (1 - frac)`` rather than the
        algebraically-equal ``pmin + frac * (pmax - pmin)``: this is the exact
        float arithmetic the original hand-rolled recovery formula used
        (``y0 + plot_h * (1 - frac)``), so the toolkit stays bit-identical to it
        (see tests/test_recovery_chart_geometry.py::test_ews_y_matches_original).
        """
        span = self.vmax - self.vmin
        if span == 0:
            return (self.pmin + self.pmax) / 2.0
        frac = (value - self.vmin) / span
        frac = max(0.0, min(1.0, frac))
        return self.pmax + (self.pmin - self.pmax) * (1.0 - frac)

    @classmethod
    def for_x(cls, vmin: float, vmax: float, bounds: Bounds) -> LinearScale:
        """Build an X scale spanning a plot's left->right pixels."""
        return cls(vmin=vmin, vmax=vmax, pmin=bounds.x0, pmax=bounds.x1)

    @classmethod
    def for_y(cls, vmin: float, vmax: float, bounds: Bounds) -> LinearScale:
        """Build a Y scale (inverted: higher value -> higher on screen).

        SVG y grows downward, so the larger domain value must map to the SMALLER
        pixel (the top). This passes ``pmin=bottom (y1)``, ``pmax=top (y0)`` so
        callers never have to remember the inversion.
        """
        return cls(vmin=vmin, vmax=vmax, pmin=bounds.y1, pmax=bounds.y0)


class SeriesKind(StrEnum):
    """How a data series is drawn."""

    LINE = "line"
    BARS = "bars"
    AREA = "area"


@dataclass(frozen=True)
class Series:
    """One named data series to plot (values in domain/value space).

    Attributes:
        key: Stable identifier (used for legend / tooltip lookup).
        label: Human-facing label.
        kind: How to render it (line / bars / area).
        values: The domain values, one per x position (index = x ordinal).
        accent: A theme color token NAME (e.g. ``"chrome"`` / ``"positive"``),
            resolved to a CSS var by the rendering component — NOT a raw hex,
            so series stay on-brand and theme/dark-mode aware.
        axis: Which y axis this series uses (``"left"`` or ``"right"``) for
            dual-axis charts (e.g. EWS on left, yen on right).
    """

    key: str
    label: str
    kind: SeriesKind
    values: tuple[float, ...]
    accent: str = "chrome"
    axis: str = "left"


@dataclass(frozen=True)
class AxisTick:
    """One axis tick: its domain value, pixel position, and display label."""

    value: float
    pixel: float
    label: str


def linear_ticks(
    scale: LinearScale,
    count: int = 5,
    *,
    integer: bool = False,
) -> list[AxisTick]:
    """Return ``count`` evenly spaced ticks across a scale's domain.

    Deterministic: ticks are evenly spaced in value space and positioned via the
    scale, so they always line up with the plotted data. Labels are the value
    formatted simply (int when ``integer`` or the value is whole, else 1 dp);
    callers that need yen / percent formatting can re-label from ``tick.value``.

    Args:
        scale: The axis scale.
        count: Number of ticks (>= 2 to include both ends; clamped to >= 2).
        integer: Force integer labels.

    Returns:
        Ordered ticks from ``vmin`` to ``vmax``.
    """
    n = max(2, count)
    span = scale.vmax - scale.vmin
    ticks: list[AxisTick] = []
    for i in range(n):
        frac = i / (n - 1)
        value = scale.vmin + frac * span
        label = f"{int(round(value))}" if integer or value == int(value) else f"{value:.1f}"
        ticks.append(AxisTick(value=value, pixel=scale.scale(value), label=label))
    return ticks


def _x_positions(n: int, bounds: Bounds) -> list[float]:
    """Return ``n`` evenly spaced x pixel centers across the plot width.

    A single point is centered; two or more are spread end to end so the first
    sits on the left edge and the last on the right edge (matching how the
    recovery line spans the full plot).
    """
    if n <= 0:
        return []
    if n == 1:
        return [(bounds.x0 + bounds.x1) / 2.0]
    # Match the original hand-rolled even-spacing arithmetic exactly
    # (``x0 + plot_w * i / (n - 1)``) rather than precomputing a step, so month
    # x-positions stay bit-identical to the pre-toolkit formula
    # (see tests/test_recovery_chart_geometry.py::test_month_x_matches_original).
    width = bounds.width
    return [bounds.x0 + width * i / (n - 1) for i in range(n)]


def build_polyline(series: Series, y_scale: LinearScale, bounds: Bounds) -> str:
    """Return an SVG ``points`` string for a line series.

    Each value maps to (even x position, y via ``y_scale``). Coordinates are
    rounded to 2 dp to keep the emitted SVG compact and deterministic.
    """
    xs = _x_positions(len(series.values), bounds)
    pts = [f"{x:.2f},{y_scale.scale(v):.2f}" for x, v in zip(xs, series.values, strict=True)]
    return " ".join(pts)


def build_area_path(series: Series, y_scale: LinearScale, bounds: Bounds) -> str:
    """Return an SVG path ``d`` for the filled area under a line series.

    The path traces the line then closes down to the plot floor (``bounds.y1``)
    and back, giving the soft area fill under the curve. Empty when there are no
    values.
    """
    xs = _x_positions(len(series.values), bounds)
    if not xs:
        return ""
    parts = [f"M {xs[0]:.2f} {y_scale.scale(series.values[0]):.2f}"]
    for x, v in zip(xs[1:], series.values[1:], strict=True):
        parts.append(f"L {x:.2f} {y_scale.scale(v):.2f}")
    # Close down to the floor and back to the start, forming the fill region.
    parts.append(f"L {xs[-1]:.2f} {bounds.y1:.2f}")
    parts.append(f"L {xs[0]:.2f} {bounds.y1:.2f}")
    parts.append("Z")
    return " ".join(parts)


@dataclass(frozen=True)
class Bar:
    """Geometry for one bar rect (pixels), ready to splat into an SVG rect."""

    x: float
    y: float
    width: float
    height: float


def build_bars(
    series: Series,
    y_scale: LinearScale,
    bounds: Bounds,
    *,
    width_ratio: float = 0.6,
    baseline: float = 0.0,
) -> list[Bar]:
    """Return per-value :class:`Bar` rects for a bar series.

    Bars are centered on the same even x positions as the line points, so a
    line+bars combo (the P&L-bridge / recovery look) aligns exactly. A bar grows
    from ``baseline`` (default 0) up to its value; negative values draw below the
    baseline. ``width_ratio`` is the fraction of the per-column slot the bar
    fills (the rest is the gap).

    Args:
        series: The bar series (domain values).
        y_scale: The y scale for this series' axis.
        bounds: The plot region.
        width_ratio: Bar width as a fraction of the column slot (0..1).
        baseline: Domain value the bars grow from (0 for yen uplift).

    Returns:
        One :class:`Bar` per value, in order.
    """
    n = len(series.values)
    xs = _x_positions(n, bounds)
    if n == 0:
        return []
    slot = bounds.width / n if n > 0 else bounds.width
    bar_w = max(1.0, slot * max(0.0, min(1.0, width_ratio)))
    base_px = y_scale.scale(baseline)
    bars: list[Bar] = []
    for x, v in zip(xs, series.values, strict=True):
        v_px = y_scale.scale(v)
        top = min(v_px, base_px)
        height = abs(base_px - v_px)
        bars.append(Bar(x=x - bar_w / 2.0, y=top, width=bar_w, height=height))
    return bars


@dataclass
class ChartGeometry:
    """Convenience bundle of a chart's plot bounds + its axis scales.

    Lets a component build all the geometry for a (possibly dual-axis) chart in
    one place and pass it to the ``build_*`` helpers. Pure data; held by the
    rendering component, never by the deterministic backend.
    """

    bounds: Bounds
    x_scale: LinearScale
    y_left: LinearScale
    y_right: LinearScale | None = None
    series: list[Series] = field(default_factory=list)

    def y_for(self, series: Series) -> LinearScale:
        """Return the y scale a series should use (right axis if requested+present)."""
        if series.axis == "right" and self.y_right is not None:
            return self.y_right
        return self.y_left


# ---------------------------------------------------------------------------
# Portfolio primitives (Feature 8.1 watchlist) — the toolkit's third consumer.
#
# These are PURE, governance-free geometry/ordering helpers for the book-level
# watchlist: a compact per-borrower EWS sparkline and a deterministic
# "sort by deterioration" ranking. They compute screen geometry and an ordering
# over already-computed figures; they hold no data, persist nothing, and decide
# no verdict — so they carry zero data-governance weight and are safe to ship
# ahead of the (opt-in, bank-owned) Portfolio persistence decision.
# ---------------------------------------------------------------------------


def build_sparkline(
    values: tuple[float, ...] | list[float],
    bounds: Bounds,
    *,
    pad_frac: float = 0.12,
) -> str:
    """Return an SVG ``points`` string for a compact, axis-less trend line.

    Unlike :func:`build_polyline` (which needs a caller-supplied y-scale over a
    shared axis), a sparkline auto-scales to its OWN min/max so each tiny
    watchlist row reads on its own terms. A small vertical pad keeps the extremes
    off the row's edges; a flat series maps to the row midline (via
    :class:`LinearScale`'s degenerate-domain handling). Coordinates are rounded
    to 2 dp for compact, deterministic output. Empty when there are no values.

    Args:
        values: The series values (domain space), oldest -> newest.
        bounds: The sparkline's pixel rectangle (a single watchlist cell).
        pad_frac: Fraction of the value span to pad above and below (0..0.5).

    Returns:
        An SVG polyline ``points`` string, or ``""`` for an empty series.
    """
    vals = list(values)
    if not vals:
        return ""
    vmin, vmax = min(vals), max(vals)
    span = vmax - vmin
    pad = span * max(0.0, min(0.5, pad_frac)) if span > 0 else 0.0
    y_scale = LinearScale.for_y(vmin - pad, vmax + pad, bounds)
    xs = _x_positions(len(vals), bounds)
    return " ".join(f"{x:.2f},{y_scale.scale(v):.2f}" for x, v in zip(xs, vals, strict=True))


def sparkline_trend(values: tuple[float, ...] | list[float]) -> int:
    """Return the trend sign of a series: +1 rising, -1 falling, 0 flat/empty.

    Compares the last value to the first. For an EWS series (higher = worse), a
    +1 means deteriorating and -1 means improving — the watchlist colours the
    sparkline accordingly. Pure; needs no geometry.
    """
    vals = list(values)
    if len(vals) < 2:
        return 0
    delta = vals[-1] - vals[0]
    if delta > 0:
        return 1
    if delta < 0:
        return -1
    return 0


@dataclass(frozen=True)
class DeteriorationRow:
    """One borrower row for the watchlist ranking (display values only).

    Attributes:
        key: Stable identifier (e.g. TDB code) — the deterministic tie-break.
        ews: Latest EWS score (higher = worse health).
        crossed: True if the borrower JUST crossed a deterioration threshold
            (these surface at the top regardless of absolute EWS).
        trend: Trend sign of the EWS sparkline (+1 worse / -1 better / 0 flat).
    """

    key: str
    ews: float
    crossed: bool = False
    trend: int = 0


def rank_by_deterioration(rows: list[DeteriorationRow]) -> list[DeteriorationRow]:
    """Return rows ordered worst-first for the watchlist (pure, deterministic).

    Ordering, most-urgent first:
      1. borrowers that JUST crossed a threshold (``crossed=True``) lead, because
         a fresh crossing is the event a banker must catch before the borrower
         calls them;
      2. then by latest EWS descending (higher = worse);
      3. ties broken by ``key`` ascending for byte-stable output.

    The input list is not mutated. This decides DISPLAY ORDER only — it computes
    no figure and changes no verdict.
    """
    return sorted(
        rows,
        key=lambda r: (0 if r.crossed else 1, -float(r.ews), str(r.key)),
    )


@dataclass(frozen=True)
class DistributionBand:
    """One band of the book-level EWS distribution (Feature 8.1 / 9 §7).

    A pure tally bucket for the at-a-glance "where does the book sit?" overview
    on the Portfolio altitude: how many borrowers fall in each FSA health band,
    plus the pixel width of that segment in a single stacked 100%-width bar.

    Attributes:
        key: Stable band identifier (e.g. ``"normal"`` / ``"doubtful"``).
        label: Human-facing band label.
        accent: Theme colour token NAME (resolved to a CSS var by the renderer),
            so the segment stays on-brand and theme/dark-mode aware.
        count: Number of borrowers in this band.
        width_pct: This band's share of the book as a 0–100 percentage of the
            stacked bar width (0 when the book is empty).
    """

    key: str
    label: str
    accent: str
    count: int
    width_pct: float


def build_band_distribution(
    bands: list[tuple[str, str, str]],
    counts: dict[str, int],
) -> list[DistributionBand]:
    """Tally borrowers into ordered bands for the stacked distribution bar.

    Pure and deterministic: it tallies ALREADY-CLASSIFIED counts into a fixed,
    caller-supplied band order and computes each band's share of the total as a
    percentage width for the stacked bar. It classifies nothing itself (the
    deterministic spine already decided each borrower's band) and computes no
    figure beyond the display percentage. An empty / all-zero book yields every
    band at ``count=0`` and ``width_pct=0`` (the renderer shows an empty bar).

    To keep the segments byte-stable and summing to exactly 100 with no rounding
    drift, widths are floored to 2 dp and the largest band absorbs the remainder.

    Args:
        bands: Ordered ``(key, label, accent_token)`` tuples — the band order
            and presentation, fixed by the caller (worst-first or best-first).
        counts: Per-band borrower counts keyed by band ``key`` (missing = 0).

    Returns:
        One :class:`DistributionBand` per input band, in the given order.
    """
    total = sum(max(0, int(counts.get(key, 0))) for key, _, _ in bands)
    if total <= 0:
        return [
            DistributionBand(key=key, label=label, accent=accent, count=0, width_pct=0.0)
            for key, label, accent in bands
        ]

    raw = [(key, label, accent, max(0, int(counts.get(key, 0)))) for key, label, accent in bands]
    widths = [round(count / total * 100.0, 2) for _, _, _, count in raw]
    # Absorb the rounding remainder into the largest band so widths sum to 100.
    drift = round(100.0 - sum(widths), 2)
    if drift != 0.0:
        biggest = max(range(len(raw)), key=lambda i: raw[i][3])
        widths[biggest] = round(widths[biggest] + drift, 2)

    return [
        DistributionBand(key=key, label=label, accent=accent, count=count, width_pct=width)
        for (key, label, accent, count), width in zip(raw, widths, strict=True)
    ]
