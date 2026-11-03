"""FITS / raw image viewer (PyQtGraph) — display stretch + measurement tools.

Receives a **linear** display array (2-D uint16 plane or 3-D uint16 RGB from
``debayer.render_view``) and maps it to the screen through the stretch
(black/white/midtones + linear/log/asinh). The linear array is kept for
measurement (§4): pixel readout on hover, a region-stats ROI, and a
saturation/clipping overlay. None of this touches the data written to FITS.

Signals include ``levels_changed(black, white)`` after an auto-stretch,
``pixel_info(str)`` for the cursor readout, and ``region_info(dict | None)``
for the ROI statistics.
"""

from __future__ import annotations

import html
import logging

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from argos.ui import theme
from argos.core.imaging.stretch import (
    STRETCH_LINEAR,
    apply_stretch,
    auto_stf,
    region_stats,
)

logger = logging.getLogger(__name__)

#: Loupe: on-screen size (px) and the source window it magnifies (display px).
_LOUPE_PX = 168
_LOUPE_SRC = 42

#: Display-rotation modes. "auto" turns portrait frames 90° CW so the wide
#: Seestar sensor reads landscape; the fixed angles are clockwise. Rotation is
#: display-only: the public API (star_clicked, mark_selection, set_*_markers)
#: keeps speaking un-rotated frame coordinates, and saved FITS are untouched.
ROTATION_MODES = ("auto", "0", "90", "180", "270")


def _short_marker_label(value: object, limit: int = 18) -> str:
    """Shorten an on-image catalogue label without losing its identity."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: max(6, limit - 7)]}…{text[-6:]}"


def _marker_tip(*_args: object, data: object = None, **_kwargs: object) -> str:
    """Render PyQtGraph marker data safely as a hover tooltip.

    PyQtGraph invokes custom tips with keyword arguments (``x``, ``y`` and
    ``data``); accepting both calling conventions keeps this callback valid
    across supported PyQtGraph releases.

    PyQtGraph passes the string straight to ``setToolTip``, and Qt shows a
    string without leading markup as plain text, so escaped entities would
    appear literally (``Dec +44°39&#x27;23.1&quot;``). The escaped text is
    therefore wrapped as rich text: catalogue names still cannot inject
    markup, and ``white-space: pre`` keeps the line breaks and aligned spaces.
    """
    text = str(data or "")
    if not text:
        return ""
    return f'<p style="white-space:pre">{html.escape(text)}</p>'


def _to_qimage(arr: np.ndarray) -> QImage:
    """Build a QImage from a uint8 grayscale (H,W) or RGB (H,W,3) array."""
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    if arr.ndim == 2:
        return QImage(arr.data, w, h, w, QImage.Format.Format_Grayscale8).copy()
    return QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


class FitsViewer(QWidget):
    """Image viewer with non-destructive stretch + measurement overlays."""

    levels_changed = pyqtSignal(float, float, float)  # black, white, midtones
    region_info = pyqtSignal(object)
    star_clicked = pyqtSignal(float, float)  # display-px (x, y) of a click

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._last_arr: np.ndarray | None = None  # linear display array (rotated)
        self._arr0: np.ndarray | None = None  # as delivered, un-rotated
        self._rot_mode: str = "0"  # one of ROTATION_MODES
        self._rot_k: int = 0  # 90°-CW turns applied to the current frame
        self._black: float = 0.0
        self._white: float = 65535.0
        self._mode: str = STRETCH_LINEAR
        self._midtones: float = 0.5
        self._auto_on: bool = True  # auto-level each frame until the user adjusts
        self._sat_enabled: bool = False
        self._sat_threshold: int = 60000
        self._roi: pg.RectROI | None = None
        self._crosshair: bool = False  # off by default; when on, follows the cursor only
        self._disp_u8: np.ndarray | None = None  # last stretched display (for the loupe)
        # §5 focus tools: 100% loupe.
        self._green_shape: tuple[int, int] | None = None
        self._loupe: bool = False
        # §6 astrometry overlay: RA/Dec grid + field-centre + target reticle.
        self._wcs_overlay = None  # platesolve.WCSOverlay (green-plane coords)
        self._wcs_on: bool = False
        # §6 catalog overlay: VSX variable-star markers (green-plane coords).
        self._catalog: tuple = ()  # (x_green, y_green, suspected) per object
        self._catalog_on: bool = False
        self._catalog_labels: list = []
        # Named, non-variable Gaia DR3 sources in the solved field. This is a
        # catalogue identity layer, deliberately separate from image detection.
        self._field_catalogue: tuple = ()  # (x_green, y_green, name, tooltip)
        self._field_catalogue_on: bool = False
        self._field_catalogue_labels: list = []
        # Local/cached non-stellar context is rendered in independent layers:
        # an observer can ask for galaxies without drowning the image in Gaia
        # source identifiers, and vice versa.
        self._context_layers: dict[str, dict] = {
            "galaxies": {"points": (), "enabled": False, "labels": [], "item": None},
            "nebulae_clusters": {"points": (), "enabled": False, "labels": [], "item": None},
            "exoplanets": {"points": (), "enabled": False, "labels": [], "item": None},
            "other_objects": {"points": (), "enabled": False, "labels": [], "item": None},
        }
        # §6 R5: VSP comparison-star markers (green px) + their chart labels.
        self._comparisons: tuple = ()  # (x_green, y_green, label) per object
        self._comparisons_on: bool = False
        self._comp_labels: list = []  # pg.TextItem pool, rebuilt on each refresh
        # §6 P1: saved-target markers (green px) — bold amber ring + name label.
        self._targets: tuple = ()  # (x_green, y_green, name) per saved target
        self._targets_on: bool = False
        self._target_labels: list = []
        self._marker_labels_on: bool = False
        self._grid_labels: list = []  # RA/Dec edge labels (pooled)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        pg.setConfigOptions(imageAxisOrder="row-major")
        self._view = pg.ImageView()
        self._view.ui.roiBtn.hide()
        self._view.ui.menuBtn.hide()
        self._view.ui.histogram.hide()
        layout.addWidget(self._view)
        self.set_palette("neutral")

        # Crosshair (toggleable) — only shown while the cursor is over the image
        # (it never parks in the centre at rest).
        self._vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(theme.WARNING, width=1))
        self._hline = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen(theme.WARNING, width=1))
        for line in (self._vline, self._hline):
            line.setVisible(False)
            self._view.getView().addItem(line, ignoreBounds=True)

        # In-image pixel readout — a compact overlay pinned to the top-left,
        # so the value sits where you point instead of in a side column.
        self._readout = QLabel(self)
        self._readout.setStyleSheet(
            f"background: {theme.BG2}; color: {theme.FG};"
            f" font-family: {theme.FONT_MONO}; font-size: 11px;"
            f" padding: 2px 7px; border-radius: 3px;"
        )
        self._readout.move(10, 10)
        self._readout.hide()

        # §5 selected-star highlight: a ring drawn at the *actual* measurement
        # aperture (data-space, so it grows/shrinks with the radius) around a
        # small fixed centre dot — plus a readout pinned bottom-left.
        self._sel_ring = pg.PlotDataItem(pen=pg.mkPen(theme.ACCENT, width=2))
        self._sel_ring.setVisible(False)
        self._view.getView().addItem(self._sel_ring, ignoreBounds=True)
        self._sel_center = pg.ScatterPlotItem(
            pen=None, brush=pg.mkBrush(theme.ACCENT), pxMode=True, size=6, symbol="o"
        )
        self._sel_center.setVisible(False)
        self._view.getView().addItem(self._sel_center, ignoreBounds=True)

        self._sel_label = QLabel(self)
        self._sel_label.setStyleSheet(
            f"background: {theme.BG2}; color: {theme.FG};"
            f" font-family: {theme.FONT_MONO}; font-size: 11px;"
            f" padding: 4px 8px; border-radius: 3px; border: 1px solid {theme.ACCENT};"
        )
        self._sel_label.hide()

        # §5 loupe — a magnified 1:1 inset following the cursor (top-right).
        self._loupe_label = QLabel(self)
        self._loupe_label.setFixedSize(_LOUPE_PX, _LOUPE_PX)
        self._loupe_label.setStyleSheet(f"background: #000; border: 1px solid {theme.WARNING};")
        self._loupe_label.hide()

        # §6 astrometry overlay — RA/Dec grid (one NaN-broken curve) and an
        # optional target reticle. The solved centre RA/Dec is shown as text (no
        # centre marker). All hidden until a solve provides a WCS and it's on.
        # width=2: hairlines vanished on the field laptop (feedback 2026-07-11).
        self._grid_item = pg.PlotDataItem(
            pen=pg.mkPen(theme.ACCENT, width=2, style=Qt.PenStyle.DotLine)
        )
        self._grid_item.setVisible(False)
        self._view.getView().addItem(self._grid_item, ignoreBounds=True)
        self._target_item = pg.PlotDataItem(pen=pg.mkPen(theme.SUCCESS, width=2))
        self._target_item.setVisible(False)
        self._view.getView().addItem(self._target_item, ignoreBounds=True)

        # §6 catalog overlay — VSX variable stars as hollow blue circles.
        # Per-spot pen lets suspected variables render dashed/dimmer.
        self._catalog_item = pg.ScatterPlotItem(brush=None, pxMode=True, size=28, symbol="o")
        self._catalog_item.setVisible(False)
        self._view.getView().addItem(self._catalog_item, ignoreBounds=True)
        self._field_catalogue_item = pg.ScatterPlotItem(
            pen=pg.mkPen(theme.FG_MUTED, width=2), brush=None, pxMode=True, size=18, symbol="o"
        )
        self._field_catalogue_item.setVisible(False)
        self._view.getView().addItem(self._field_catalogue_item, ignoreBounds=True)
        self._context_layers["galaxies"]["item"] = pg.ScatterPlotItem(
            pen=pg.mkPen(theme.CYAN, width=2), brush=None, pxMode=True, size=28, symbol="o"
        )
        self._context_layers["nebulae_clusters"]["item"] = pg.ScatterPlotItem(
            pen=pg.mkPen(theme.WARNING, width=2), brush=None, pxMode=True, size=30, symbol="s"
        )
        self._context_layers["exoplanets"]["item"] = pg.ScatterPlotItem(
            pen=pg.mkPen(theme.SUCCESS, width=2), brush=None, pxMode=True, size=26, symbol="star"
        )
        self._context_layers["other_objects"]["item"] = pg.ScatterPlotItem(
            pen=pg.mkPen(theme.FG_MUTED, width=2), brush=None, pxMode=True, size=24, symbol="o"
        )
        for layer in self._context_layers.values():
            layer["item"].setVisible(False)
            self._view.getView().addItem(layer["item"], ignoreBounds=True)
        # §6 R5 — VSP comparison stars as hollow cyan squares (chart labels added
        # as TextItems in _refresh_comparisons).
        self._comparison_item = pg.ScatterPlotItem(
            pen=pg.mkPen(theme.CYAN, width=2), brush=None, pxMode=True, size=16, symbol="s"
        )
        self._comparison_item.setVisible(False)
        self._view.getView().addItem(self._comparison_item, ignoreBounds=True)
        # §6 P1 — the saved set, styled by role so a glance tells them apart:
        # target = bold ring + name, comparison = bold square, check = thin ring.
        # (Field feedback 2026-07-11: one style for every role was unreadable.)
        self._targets_item = pg.ScatterPlotItem(brush=None, pxMode=True)
        self._targets_item.setVisible(False)
        self._view.getView().addItem(self._targets_item, ignoreBounds=True)
        self._astro_label = QLabel(self)
        self._astro_label.setStyleSheet(
            f"background: {theme.BG2}; color: {theme.FG};"
            f" font-family: {theme.FONT_MONO}; font-size: 11px;"
            f" padding: 3px 7px; border-radius: 3px; border: 1px solid {theme.ACCENT};"
        )
        self._astro_label.hide()

        self._view.scene.sigMouseMoved.connect(self._on_mouse_moved)
        self._view.scene.sigMouseClicked.connect(self._on_mouse_clicked)

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def set_rotation(self, mode: str) -> None:
        """Set the display rotation ("auto" or CW degrees "0"/"90"/"180"/"270")."""
        mode = str(mode).lower().rstrip("°")
        if mode not in ROTATION_MODES or mode == self._rot_mode:
            return
        self._rot_mode = mode
        if self._arr0 is not None:
            self.display(self._arr0)  # re-derives k + refreshes every overlay

    def _effective_k(self, arr: np.ndarray) -> int:
        if self._rot_mode == "auto":
            h, w = arr.shape[:2]
            return 1 if h > w else 0  # portrait sensor → landscape view
        return int(self._rot_mode) // 90

    def _rot_pt(self, x, y):
        """Un-rotated display px → rotated view px (scalars or arrays)."""
        if self._rot_k == 0 or self._arr0 is None:
            return x, y
        h, w = self._arr0.shape[:2]
        if self._rot_k == 1:
            return (h - 1) - y, x
        if self._rot_k == 2:
            return (w - 1) - x, (h - 1) - y
        return y, (w - 1) - x

    def _unrot_pt(self, x, y):
        """Rotated view px → un-rotated display px (inverse of _rot_pt)."""
        if self._rot_k == 0 or self._arr0 is None:
            return x, y
        h, w = self._arr0.shape[:2]
        if self._rot_k == 1:
            return y, (h - 1) - x
        if self._rot_k == 2:
            return (w - 1) - x, (h - 1) - y
        return (w - 1) - y, x

    def display(self, arr: np.ndarray) -> None:
        """Display a linear array (2-D uint16 plane or 3-D uint16 RGB)."""
        if arr.ndim not in (2, 3):
            return
        self._arr0 = arr
        k = self._effective_k(arr)
        rotated = k != self._rot_k
        self._rot_k = k
        self._last_arr = np.ascontiguousarray(np.rot90(arr, -k)) if k else arr
        if rotated:
            self.clear_selection()  # old ring coords belong to the old orientation
        if self._auto_on:
            self._black, self._white, self._midtones = auto_stf(arr)
            self.levels_changed.emit(self._black, self._white, self._midtones)
        self._render()
        self._refresh_astrometry()
        self._refresh_catalog()
        self._refresh_field_catalogue()
        self._refresh_context_layer("galaxies")
        self._refresh_context_layer("nebulae_clusters")
        self._refresh_context_layer("exoplanets")
        self._refresh_context_layer("other_objects")
        self._refresh_comparisons()
        self._refresh_targets()
        if rotated:
            self._view.getView().autoRange()  # the canvas aspect just changed
        if self._roi is not None:
            self._on_roi_changed()

    def _render(self) -> None:
        if self._last_arr is None:
            return
        disp = apply_stretch(self._last_arr, self._black, self._white, self._mode, self._midtones)
        if self._sat_enabled:
            disp = self._overlay_saturation(disp)
        self._disp_u8 = disp  # kept for the loupe (what the eye sees)
        self._view.setImage(
            disp,
            autoRange=False,
            autoLevels=False,
            autoHistogramRange=False,
            levels=(0, 255),
        )

    def set_palette(self, palette: str) -> None:
        """Apply a display-only mono/false-colour palette."""
        colors = {
            "neutral": [(0, 0, 0), (255, 255, 255)],
            "negative": [(255, 255, 255), (0, 0, 0)],
            "fire": [(0, 0, 0), (50, 0, 80), (190, 20, 20), (255, 150, 0), (255, 255, 210)],
            "ice": [(0, 0, 0), (0, 20, 90), (0, 150, 220), (160, 240, 255), (255, 255, 210)],
        }.get(palette, [(0, 0, 0), (255, 255, 255)])
        self._view.setColorMap(pg.ColorMap(np.linspace(0.0, 1.0, len(colors)), colors))

    def _overlay_saturation(self, disp_u8: np.ndarray) -> np.ndarray:
        """Paint pixels at/above the full-well threshold red (display only)."""
        a = self._last_arr
        src = a if a.ndim == 2 else a.max(axis=2)
        mask = src >= self._sat_threshold
        if disp_u8.ndim == 2:
            rgb = np.repeat(disp_u8[:, :, None], 3, axis=2)
        else:
            rgb = disp_u8.copy()
        if mask.any():
            rgb[mask] = (255, 0, 0)
        return rgb

    # ------------------------------------------------------------------
    # Stretch controls (driven by the histogram dock)
    # ------------------------------------------------------------------

    def set_stretch(self, black: float, white: float, midtones: float, mode: str) -> None:
        self._auto_on = False
        self._black, self._white = float(black), float(white)
        self._midtones, self._mode = float(midtones), mode
        self._render()

    def auto_stretch(self) -> None:
        self._auto_on = True
        if self._last_arr is not None:
            self._black, self._white, self._midtones = auto_stf(self._last_arr)
            self.levels_changed.emit(self._black, self._white, self._midtones)
            self._render()

    def set_saturation(self, enabled: bool, threshold: int) -> None:
        self._sat_enabled = bool(enabled)
        self._sat_threshold = int(threshold)
        self._render()

    # ------------------------------------------------------------------
    # Region-stats ROI (§4)
    # ------------------------------------------------------------------

    def set_roi_enabled(self, enabled: bool) -> None:
        if enabled and self._roi is None:
            self._roi = pg.RectROI([10, 10], [80, 80], pen=pg.mkPen("y", width=1))
            self._view.getView().addItem(self._roi)
            self._roi.sigRegionChanged.connect(self._on_roi_changed)
            self._on_roi_changed()
        elif not enabled and self._roi is not None:
            self._view.getView().removeItem(self._roi)
            self._roi = None
            self.region_info.emit(None)

    def _on_roi_changed(self) -> None:
        if self._last_arr is None or self._roi is None:
            return
        pos, size = self._roi.pos(), self._roi.size()
        x0 = int(min(pos.x(), pos.x() + size.x()))
        x1 = int(max(pos.x(), pos.x() + size.x()))
        y0 = int(min(pos.y(), pos.y() + size.y()))
        y1 = int(max(pos.y(), pos.y() + size.y()))
        a = self._last_arr
        h, w = a.shape[:2]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        if x1 <= x0 or y1 <= y0:
            return
        region = a[y0:y1, x0:x1]
        if region.ndim == 3:
            region = region.mean(axis=2)
        self.region_info.emit(region_stats(region))

    # ------------------------------------------------------------------
    # Pixel readout (§4)
    # ------------------------------------------------------------------

    def _on_mouse_moved(self, scene_pos) -> None:
        if self._last_arr is None:
            return
        p = self._view.getImageItem().mapFromScene(scene_pos)
        x, y = int(p.x()), int(p.y())
        a = self._last_arr
        h, w = a.shape[:2]
        if 0 <= x < w and 0 <= y < h:
            if self._crosshair:
                self._vline.setPos(p.x())
                self._hline.setPos(p.y())
                self._vline.setVisible(True)
                self._hline.setVisible(True)
            # Label the un-rotated frame position (what FITS/analysis see);
            # the value is read from the rotated array (same pixel).
            ux, uy = (int(v) for v in self._unrot_pt(x, y))
            if a.ndim == 2:
                self._readout.setText(f"({ux}, {uy})   {int(a[y, x])} ADU")
            else:
                r, g, b = (int(v) for v in a[y, x][:3])
                self._readout.setText(f"({ux}, {uy})   R {r}  G {g}  B {b}")
            self._readout.adjustSize()
            self._readout.show()
            self._readout.raise_()
            if self._loupe:
                self._update_loupe(x, y)
        else:
            self._readout.hide()
            self._vline.setVisible(False)
            self._hline.setVisible(False)
            if self._loupe:
                self._loupe_label.hide()

    def set_crosshair_enabled(self, enabled: bool) -> None:
        # Only toggles the feature; the lines appear on hover, never at rest.
        self._crosshair = bool(enabled)
        if not self._crosshair:
            self._vline.setVisible(False)
            self._hline.setVisible(False)

    # ------------------------------------------------------------------
    # Focus tools (§5): star/FWHM overlay + 100% loupe
    # ------------------------------------------------------------------

    def set_frame_geometry(self, green_shape: tuple[int, int]) -> None:
        """Per-frame green-plane geometry — every overlay scales through it."""
        self._green_shape = green_shape

    # ------------------------------------------------------------------
    # Astrometry overlay (§6): RA/Dec grid + field centre + target reticle
    # ------------------------------------------------------------------

    def set_astrometry_overlay(self, overlay, green_shape: tuple[int, int] | None = None) -> None:
        """Receive grid/centre/target geometry (green-plane coords) from a solve."""
        self._wcs_overlay = overlay
        if green_shape is not None:
            self._green_shape = green_shape
        self._refresh_astrometry()

    def set_astrometry_enabled(self, enabled: bool) -> None:
        self._wcs_on = bool(enabled)
        self._refresh_astrometry()

    def _refresh_astrometry(self) -> None:
        """Redraw the WCS grid/centre/target, scaled green-plane → active view."""
        ov = self._wcs_overlay
        show = self._wcs_on and ov is not None and self._last_arr is not None
        gh, gw = self._green_shape or (0, 0)
        if not show or gh <= 0 or gw <= 0:
            self._grid_item.setVisible(False)
            self._target_item.setVisible(False)
            self._astro_label.hide()
            self._clear_grid_labels()
            return
        dh, dw = self._arr0.shape[:2]
        sx, sy = dw / gw, dh / gh  # green-plane px → display px
        self._draw_grid_labels(getattr(ov, "labels", ()), sx, sy)

        xs_parts, ys_parts = [], []
        gap = np.array([np.nan])
        for xs, ys in ov.lines:
            rx, ry = self._rot_pt(
                np.asarray(xs, dtype=float) * sx, np.asarray(ys, dtype=float) * sy
            )
            xs_parts.extend((rx, gap))
            ys_parts.extend((ry, gap))
        if xs_parts:
            self._grid_item.setData(
                np.concatenate(xs_parts), np.concatenate(ys_parts), connect="finite"
            )
            self._grid_item.setVisible(True)
        else:
            self._grid_item.setVisible(False)

        if ov.target is not None:
            tx, ty = self._rot_pt(ov.target[0] * sx, ov.target[1] * sy)
            rr = 0.05 * min(dw, dh)
            th = np.linspace(0.0, 2.0 * np.pi, 49)
            self._target_item.setData(tx + rr * np.cos(th), ty + rr * np.sin(th))
            self._target_item.setVisible(True)
        else:
            self._target_item.setVisible(False)

        if ov.center_label:
            self._astro_label.setText(ov.center_label)
            self._astro_label.adjustSize()
            self._astro_label.move(10, 36)
            self._astro_label.show()
            self._astro_label.raise_()
        else:
            self._astro_label.hide()

    def _clear_grid_labels(self) -> None:
        view = self._view.getView()
        for t in self._grid_labels:
            view.removeItem(t)
        self._grid_labels = []

    def _draw_grid_labels(self, labels, sx: float, sy: float) -> None:
        """Place RA/Dec coordinate labels at the frame borders (green px → view)."""
        self._clear_grid_labels()
        view = self._view.getView()
        for x, y, text in labels or ():
            t = pg.TextItem(text=str(text), color=theme.ACCENT, anchor=(0.0, 0.0))
            t.setPos(*self._rot_pt(x * sx, y * sy))
            view.addItem(t, ignoreBounds=True)
            self._grid_labels.append(t)

    # ------------------------------------------------------------------
    # Catalog overlay (§6): VSX variable-star markers
    # ------------------------------------------------------------------

    def set_catalog_markers(self, points, green_shape: tuple[int, int] | None = None) -> None:
        """Receive variable-star marker positions (green-plane coords).

        ``points`` is an iterable of ``(x_green, y_green, suspected)`` tuples.
        """
        self._catalog = tuple(points or ())
        if green_shape is not None:
            self._green_shape = green_shape
        self._refresh_catalog()

    def set_catalog_enabled(self, enabled: bool) -> None:
        self._catalog_on = bool(enabled)
        self._refresh_catalog()

    def set_field_catalogue_markers(
        self, points, green_shape: tuple[int, int] | None = None
    ) -> None:
        """Set named Gaia DR3 source positions in green-plane coordinates."""
        self._field_catalogue = tuple(points or ())
        if green_shape is not None:
            self._green_shape = green_shape
        self._refresh_field_catalogue()

    def set_field_catalogue_enabled(self, enabled: bool) -> None:
        self._field_catalogue_on = bool(enabled)
        self._refresh_field_catalogue()

    def set_context_markers(
        self, name: str, points, green_shape: tuple[int, int] | None = None
    ) -> None:
        """Set local/cached contextual objects for a named context layer.

        Each point is ``(x_green, y_green, short_label, tooltip)``.  This
        generic surface deliberately prevents the two optional catalogues from
        becoming ad-hoc special cases in the image viewer.
        """
        if name not in self._context_layers:
            raise ValueError(f"unknown context layer: {name}")
        self._context_layers[name]["points"] = tuple(points or ())
        if green_shape is not None:
            self._green_shape = green_shape
        self._refresh_context_layer(name)

    def set_context_enabled(self, name: str, enabled: bool) -> None:
        if name not in self._context_layers:
            raise ValueError(f"unknown context layer: {name}")
        self._context_layers[name]["enabled"] = bool(enabled)
        self._refresh_context_layer(name)

    def set_marker_labels_enabled(self, enabled: bool) -> None:
        """Show or hide labels for catalogue and saved photometry markers."""
        self._marker_labels_on = bool(enabled)
        self._refresh_catalog()
        self._refresh_field_catalogue()
        self._refresh_context_layer("galaxies")
        self._refresh_context_layer("nebulae_clusters")
        self._refresh_context_layer("exoplanets")
        self._refresh_context_layer("other_objects")
        self._refresh_comparisons()
        self._refresh_targets()

    @staticmethod
    def _claim_label_slot(occupied: set[tuple[int, int]], x: float, y: float) -> bool:
        """Reserve one smart-label cell and reject overlapping neighbours."""
        cell = (int(x // 150), int(y // 42))
        if any((cell[0] + dx, cell[1] + dy) in occupied for dx in (-1, 0, 1) for dy in (-1, 0, 1)):
            return False
        occupied.add(cell)
        return True

    def _refresh_catalog(self) -> None:
        """Redraw variable-star circles, scaled green-plane → active view."""
        view = self._view.getView()
        for t in self._catalog_labels:
            view.removeItem(t)
        self._catalog_labels = []
        if not (self._catalog_on and self._catalog and self._last_arr is not None):
            self._catalog_item.setVisible(False)
            return
        gh, gw = self._green_shape or (0, 0)
        if gh <= 0 or gw <= 0:
            self._catalog_item.setVisible(False)
            return
        dh, dw = self._arr0.shape[:2]
        sx, sy = dw / gw, dh / gh  # green-plane px → display px
        confirmed = pg.mkPen(theme.VARIABLE, width=2)
        suspected = pg.mkPen(theme.VARIABLE, width=1, style=Qt.PenStyle.DashLine)
        spots = []
        label_slots: set[tuple[int, int]] = set()
        for point in self._catalog:
            x, y, suspected_flag, *details = point
            name = details[0] if details else ""
            tip = details[1] if len(details) > 1 else name
            px, py = self._rot_pt(x * sx, y * sy)
            spots.append(
                {
                    "pos": (px, py),
                    "pen": (suspected if suspected_flag else confirmed),
                    "symbol": "o",
                    "size": 30,
                    "data": tip,
                }
            )
            if self._marker_labels_on and name and self._claim_label_slot(label_slots, px, py):
                text = pg.TextItem(
                    text=_short_marker_label(name), color=theme.VARIABLE, anchor=(0.0, 1.3)
                )
                text.setPos(px, py)
                view.addItem(text, ignoreBounds=True)
                self._catalog_labels.append(text)
        self._catalog_item.setData(spots, hoverable=True, tip=_marker_tip, hoverSize=36)
        self._catalog_item.setVisible(True)

    def _refresh_field_catalogue(self) -> None:
        """Redraw named Gaia sources, scaled green-plane → active view."""
        view = self._view.getView()
        for text in self._field_catalogue_labels:
            view.removeItem(text)
        self._field_catalogue_labels = []
        if not (self._field_catalogue_on and self._field_catalogue and self._last_arr is not None):
            self._field_catalogue_item.setVisible(False)
            return
        gh, gw = self._green_shape or (0, 0)
        if gh <= 0 or gw <= 0:
            self._field_catalogue_item.setVisible(False)
            return
        dh, dw = self._arr0.shape[:2]
        sx, sy = dw / gw, dh / gh
        spots = []
        label_slots: set[tuple[int, int]] = set()
        for point in self._field_catalogue:
            x, y, name, *details = point
            tooltip = details[0] if details else name
            px, py = self._rot_pt(x * sx, y * sy)
            spots.append(
                {
                    "pos": (px, py),
                    "symbol": "o",
                    "size": 18,
                    "pen": pg.mkPen(theme.FG_MUTED, width=2),
                    "data": tooltip,
                }
            )
            if self._marker_labels_on and name and self._claim_label_slot(label_slots, px, py):
                text = pg.TextItem(
                    text=_short_marker_label(name), color=theme.FG_MUTED, anchor=(0.0, 1.35)
                )
                text.setPos(px, py)
                view.addItem(text, ignoreBounds=True)
                self._field_catalogue_labels.append(text)
        self._field_catalogue_item.setData(spots, hoverable=True, tip=_marker_tip, hoverSize=24)
        self._field_catalogue_item.setVisible(True)

    def _refresh_context_layer(self, name: str) -> None:
        """Render one optional solved-field context layer."""
        layer = self._context_layers[name]
        view = self._view.getView()
        for text in layer["labels"]:
            view.removeItem(text)
        layer["labels"] = []
        item = layer["item"]
        points = layer["points"]
        if not (layer["enabled"] and points and self._last_arr is not None):
            item.setVisible(False)
            return
        gh, gw = self._green_shape or (0, 0)
        if gh <= 0 or gw <= 0:
            item.setVisible(False)
            return
        dh, dw = self._arr0.shape[:2]
        sx, sy = dw / gw, dh / gh
        colour = {
            "galaxies": theme.CYAN,
            "nebulae_clusters": theme.WARNING,
            "exoplanets": theme.SUCCESS,
            "other_objects": theme.FG_MUTED,
        }[name]
        spots = []
        label_slots: set[tuple[int, int]] = set()
        for point in points:
            x, y, label, *details = point
            tooltip = details[0] if details else label
            px, py = self._rot_pt(x * sx, y * sy)
            spots.append({"pos": (px, py), "data": tooltip})
            if self._marker_labels_on and label and self._claim_label_slot(label_slots, px, py):
                text = pg.TextItem(text=_short_marker_label(label), color=colour, anchor=(0.0, 1.3))
                text.setPos(px, py)
                view.addItem(text, ignoreBounds=True)
                layer["labels"].append(text)
        item.setData(spots, hoverable=True, tip=_marker_tip, hoverSize=36)
        item.setVisible(True)

    # ------------------------------------------------------------------
    # Comparison-star overlay: optional VSP catalogue candidates.
    # ------------------------------------------------------------------

    def set_comparison_markers(self, points, green_shape: tuple[int, int] | None = None) -> None:
        """Receive comparison-star markers (green-plane coords).

        ``points`` is an iterable of ``(x_green, y_green, tooltip)`` tuples.
        VSP chart labels (e.g. ``114`` for V≈11.4) are deliberately not drawn:
        they are magnitudes, not meaningful star names.
        """
        self._comparisons = tuple(points or ())
        if green_shape is not None:
            self._green_shape = green_shape
        self._refresh_comparisons()

    def set_comparison_enabled(self, enabled: bool) -> None:
        self._comparisons_on = bool(enabled)
        self._refresh_comparisons()

    def _clear_comp_labels(self) -> None:
        view = self._view.getView()
        for t in self._comp_labels:
            view.removeItem(t)
        self._comp_labels = []

    def _refresh_comparisons(self) -> None:
        """Redraw optional VSP candidates without pseudo-name labels."""
        self._clear_comp_labels()
        if not (self._comparisons_on and self._comparisons and self._last_arr is not None):
            self._comparison_item.setVisible(False)
            return
        gh, gw = self._green_shape or (0, 0)
        if gh <= 0 or gw <= 0:
            self._comparison_item.setVisible(False)
            return
        dh, dw = self._arr0.shape[:2]
        sx, sy = dw / gw, dh / gh  # green-plane px → display px
        spots = []
        for point in self._comparisons:
            x, y, *details = point
            px, py = self._rot_pt(x * sx, y * sy)
            spots.append(
                {
                    "pos": (px, py),
                    "symbol": "+",
                    "size": 20,
                    "data": details[0] if details else "VSP reference candidate",
                }
            )
        self._comparison_item.setData(spots, hoverable=True, tip=_marker_tip, hoverSize=26)
        self._comparison_item.setVisible(True)

    # ------------------------------------------------------------------
    # Target overlay (§6 P1): the night's selected stars (amber ring + name)
    # ------------------------------------------------------------------

    #: Per-role marker styles: colour, shape, diameter (screen px), pen width.
    #: Shapes carry meaning in colour and night-red modes alike.
    _ROLE_STYLES = {
        "target": ("ACCENT", "o", 34, 3),
        "comparison": ("CYAN", "s", 30, 3),
        "check": ("SUCCESS", "t", 30, 3),
    }

    def set_target_markers(self, points, green_shape: tuple[int, int] | None = None) -> None:
        """Receive saved-set markers as ``(x_green, y_green, name, role)`` tuples."""
        self._targets = tuple(points or ())
        if green_shape is not None:
            self._green_shape = green_shape
        self._refresh_targets()

    def set_target_enabled(self, enabled: bool) -> None:
        self._targets_on = bool(enabled)
        self._refresh_targets()

    def _refresh_targets(self) -> None:
        view = self._view.getView()
        for t in self._target_labels:
            view.removeItem(t)
        self._target_labels = []
        if not (self._targets_on and self._targets and self._last_arr is not None):
            self._targets_item.setVisible(False)
            return
        gh, gw = self._green_shape or (0, 0)
        if gh <= 0 or gw <= 0:
            self._targets_item.setVisible(False)
            return
        dh, dw = self._arr0.shape[:2]
        sx, sy = dw / gw, dh / gh
        spots = []
        for point in self._targets:
            x, y, name, role, *details = point
            tooltip = details[0] if details else name
            colour_attr, symbol, size, width = self._ROLE_STYLES.get(
                role, self._ROLE_STYLES["target"]
            )
            colour = getattr(theme, colour_attr)
            px, py = self._rot_pt(x * sx, y * sy)
            spots.append(
                {
                    "pos": (px, py),
                    "pen": pg.mkPen(colour, width=width),
                    "symbol": symbol,
                    "size": size,
                    "data": tooltip,
                }
            )
            # Comparison stars are recognized by the cyan square; VSP's
            # numeric chart labels are magnitude codes, not identities.
            if self._marker_labels_on and role != "comparison" and name:
                t = pg.TextItem(text=_short_marker_label(name), color=colour, anchor=(0.0, 1.45))
                t.setPos(px, py)
                view.addItem(t, ignoreBounds=True)
                self._target_labels.append(t)
        self._targets_item.setData(spots, hoverable=True, tip=_marker_tip, hoverSize=40)
        self._targets_item.setVisible(True)

    def set_loupe_enabled(self, enabled: bool) -> None:
        self._loupe = bool(enabled)
        if not self._loupe:
            self._loupe_label.hide()

    def _on_mouse_clicked(self, ev) -> None:
        """Left-click on the image → emit the display-px position to measure."""
        if self._last_arr is None:
            return
        try:
            if ev.button() != Qt.MouseButton.LeftButton:
                return
        except Exception:  # pragma: no cover - defensive against event shape
            pass
        p = self._view.getImageItem().mapFromScene(ev.scenePos())
        x, y = p.x(), p.y()
        h, w = self._last_arr.shape[:2]
        if 0 <= x < w and 0 <= y < h:
            ux, uy = self._unrot_pt(x, y)  # public API speaks un-rotated px
            self.star_clicked.emit(float(ux), float(uy))

    def mark_selection(
        self,
        x_disp: float,
        y_disp: float,
        text: str,
        radius_disp: float | None = None,
        show_label: bool = True,
    ) -> None:
        """Mark the selection at display px (x, y) and show its readout.

        ``radius_disp`` (display px) draws the measurement aperture as a ring
        around the centre dot; ``None`` (e.g. a catalog pick) shows the dot only.
        ``show_label=False`` draws only the ring (the live page shows the richer
        info in its on-image card instead of this built-in label).
        """
        x_disp, y_disp = self._rot_pt(x_disp, y_disp)  # into the rotated view
        self._sel_center.setData([{"pos": (x_disp, y_disp), "size": 6}])
        self._sel_center.setVisible(True)
        if radius_disp is not None and radius_disp > 0:
            th = np.linspace(0.0, 2.0 * np.pi, 60)
            self._sel_ring.setData(
                x_disp + radius_disp * np.cos(th), y_disp + radius_disp * np.sin(th)
            )
            self._sel_ring.setVisible(True)
        else:
            self._sel_ring.setVisible(False)
        if not show_label:
            self._sel_label.hide()
            return
        self._sel_label.setText(text)
        self._sel_label.adjustSize()
        self._sel_label.move(10, max(10, self.height() - self._sel_label.height() - 10))
        self._sel_label.show()
        self._sel_label.raise_()

    def clear_selection(self) -> None:
        self._sel_ring.setVisible(False)
        self._sel_center.setVisible(False)
        self._sel_label.hide()

    def _update_loupe(self, cx: int, cy: int) -> None:
        """Refresh the 1:1 magnified inset centred on display pixel (cx, cy)."""
        disp = self._disp_u8
        if disp is None:
            self._loupe_label.hide()
            return
        h, w = disp.shape[:2]
        r = _LOUPE_SRC // 2
        # Fixed-size source window, zero-padded near the edges.
        if disp.ndim == 2:
            crop = np.zeros((_LOUPE_SRC, _LOUPE_SRC), dtype=np.uint8)
        else:
            crop = np.zeros((_LOUPE_SRC, _LOUPE_SRC, 3), dtype=np.uint8)
        sx0, sy0 = max(0, cx - r), max(0, cy - r)
        sx1, sy1 = min(w, cx - r + _LOUPE_SRC), min(h, cy - r + _LOUPE_SRC)
        if sx1 <= sx0 or sy1 <= sy0:
            self._loupe_label.hide()
            return
        dx0, dy0 = sx0 - (cx - r), sy0 - (cy - r)
        crop[dy0 : dy0 + (sy1 - sy0), dx0 : dx0 + (sx1 - sx0)] = disp[sy0:sy1, sx0:sx1]

        pix = QPixmap.fromImage(_to_qimage(crop)).scaled(
            _LOUPE_PX,
            _LOUPE_PX,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation,  # nearest-neighbour → real pixels
        )
        self._draw_loupe_crosshair(pix)
        self._loupe_label.setPixmap(pix)
        self._loupe_label.move(self.width() - _LOUPE_PX - 10, 10)
        self._loupe_label.show()
        self._loupe_label.raise_()

    @staticmethod
    def _draw_loupe_crosshair(pix: QPixmap) -> None:
        painter = QPainter(pix)
        painter.setPen(QPen(QColor(theme.WARNING), 1))
        mid = _LOUPE_PX // 2
        painter.drawLine(mid, 0, mid, _LOUPE_PX)
        painter.drawLine(0, mid, _LOUPE_PX, mid)
        painter.end()

    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset viewer state (call when switching targets)."""
        self._last_arr = None
        self._arr0 = None
        self._disp_u8 = None
        self._loupe_label.hide()
        self._wcs_overlay = None
        self._refresh_astrometry()
        self._catalog = ()
        self._refresh_catalog()
        self._field_catalogue = ()
        self._refresh_field_catalogue()
        for name in self._context_layers:
            self._context_layers[name]["points"] = ()
            self._refresh_context_layer(name)
        self._comparisons = ()
        self._refresh_comparisons()
        self._targets = ()
        self._refresh_targets()
        self.clear_selection()
        self._auto_on = True
        self._black, self._white = 0.0, 65535.0
        self._mode, self._midtones = STRETCH_LINEAR, 0.5
