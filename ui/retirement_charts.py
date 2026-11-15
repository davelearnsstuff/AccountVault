# -----------------------------------------------------------------------------
# ui/retirement_charts.py
# The retirement planner's charts: the portfolio by age as a band and lines,
# the odds ring, and legend swatches.
#
# Colours are palette roles ('accent', 'level_good' ...) looked up as each one
# paints, so a theme change needs only a repaint, never a rebuild.
# -----------------------------------------------------------------------------

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from core.categories import format_money_compact
from ui.theme import current_theme, palette

# The ring's colour for each of retirement.verdict's tones.
TONE_ROLES = {'good': 'level_good', 'warn': 'level_warn', 'bad': 'level_danger'}

# How strongly the band between two percentiles is shaded.
_BAND_ALPHA = 0.20

# The height of a row of marker labels above a chart.
_LABEL_ROW = 16


def _color(role: str) -> QColor:
    """A palette role's colour, or a '#rrggbb' given outright: a category's
    colour on the dashboard is data, not theme."""
    if role.startswith('#'):
        return QColor(role)
    return QColor(palette(current_theme())[role])


# Leading digits an axis may top out at, each with the number of bands that
# puts every gridline on a round figure. Quarters of 2.5M would label the
# lines 625k and 1.9M, so 2.5 takes fifths. The steps sit close enough that
# the data fills at least two thirds of the plot; with only 1, 2, 2.5 and 5,
# a peak just over 5M drew on an axis to 10M and used half the chart.
_AXIS_TOPS = ((1, 4), (1.5, 3), (2, 4), (2.5, 5), (3, 6), (4, 4),
              (5, 5), (6, 6), (8, 4), (10, 4))


def nice_max(value: float) -> float:
    """A round number at or above value for the top of an axis: one of
    _AXIS_TOPS times a power of ten."""
    if value <= 0:
        return 100_000
    exponent = 10 ** math.floor(math.log10(value))
    fraction = value / exponent
    return next(top for top, _ in _AXIS_TOPS if fraction <= top) * exponent


def grid_steps(top: float) -> int:
    """How many bands to divide an axis topping at nice_max's `top` into."""
    leading = round(top / 10 ** math.floor(math.log10(top)), 1)
    return next((steps for value, steps in _AXIS_TOPS if value == leading), 5)


class BandChart(QWidget):
    """Money by age: a shaded band between two series, lines over it, and
    dashed markers at the ages that matter.

    The axis starts at zero, so a line's height is the size of the portfolio
    and a plan that runs out reaches the floor.

    lines are (values, role, width, dashed); band is (low, high) or None;
    markers are (age, label), and markers at one age share a line; tooltips
    is one string per age, or None."""

    def __init__(self, ages, lines, band=None, markers=(), tooltips=None,
                 height: int = 260, parent=None):
        super().__init__(parent)
        self._ages = list(ages)
        self._lines = [(list(values), role, width, dashed)
                       for values, role, width, dashed in lines]
        self._band = (list(band[0]), list(band[1])) if band else None
        joined: dict[int, list] = {}
        for age, label in markers:
            joined.setdefault(age, []).append(label)
        self._markers = [(age, ' · '.join(labels)) for age, labels in joined.items()]
        self._tooltips = list(tooltips) if tooltips else None
        self._xs: list[float] = []
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)

    def paintEvent(self, event):
        count = len(self._ages)
        if count < 2:
            return
        left, right, bottom = 64, 16, 30
        width = self.width() - left - right
        if width <= 0:
            return
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, self.font().pointSizeF() - 1.5))
        metrics = QFontMetricsF(small)

        def x_of(index):
            return left + width * index / (count - 1)

        # Marker labels sit in a row above the plot. One that would run into
        # the label before it drops to a second row, and the plot makes room.
        labels, row_ends = [], []
        for age, label in sorted(self._markers):
            if age not in self._ages:
                continue
            x = x_of(self._ages.index(age))
            row = next((i for i, end in enumerate(row_ends) if x + 5 >= end), len(row_ends))
            if row == len(row_ends):
                row_ends.append(0.0)
            row_ends[row] = x + 5 + metrics.horizontalAdvance(label) + 8
            labels.append((x, row, label))
        top = 6 + _LABEL_ROW * max(1, len(row_ends))
        height = self.height() - top - bottom
        if height <= 0:
            return

        peak = max([v for values, *_ in self._lines for v in values]
                   + (self._band[1] if self._band else []) + [0])
        y_max = nice_max(peak * 1.02)

        def y_of(value):
            return top + height * (1 - min(max(value, 0), y_max) / y_max)

        self._xs = [x_of(i) for i in range(count)]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setFont(small)
        muted = _color('text_secondary')

        # Value grid, zero at the bottom.
        steps = grid_steps(y_max)
        for step in range(steps + 1):
            value = y_max * step / steps
            y = y_of(value)
            pen = QPen(_color('border'), 1.0 if step == 0 else 0.6)
            if step:
                pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawLine(QPointF(left, y), QPointF(left + width, y))
            painter.setPen(muted)
            painter.drawText(QRectF(0, y - 8, left - 8, 16),
                             Qt.AlignRight | Qt.AlignVCenter, format_money_compact(value))

        # Ages along the bottom: every 5 or 10 years, and both ends, skipping
        # any that would crowd an end.
        step = 10 if count > 40 else 5
        metrics = painter.fontMetrics()
        for index, age in enumerate(self._ages):
            edge = index in (0, count - 1)
            if not edge and (age % step or index < 2 or index > count - 3):
                continue
            text = str(age)
            text_width = metrics.horizontalAdvance(text)
            x = x_of(index) - text_width / 2
            painter.drawText(QRectF(x, top + height + 6, text_width, 16),
                             Qt.AlignCenter, text)

        if self._band:
            low, high = self._band
            outline = [QPointF(x_of(i), y_of(v)) for i, v in enumerate(high)]
            outline += [QPointF(x_of(i), y_of(v)) for i, v in reversed(list(enumerate(low)))]
            fill = _color('accent')
            fill.setAlphaF(_BAND_ALPHA)
            path = QPainterPath()
            path.addPolygon(QPolygonF(outline))
            painter.fillPath(path, fill)

        for x, row, label in labels:
            pen = QPen(muted, 1.0, Qt.DashLine)
            painter.setPen(pen)
            painter.drawLine(QPointF(x, top), QPointF(x, top + height))
            painter.drawText(QRectF(x + 5, 4 + _LABEL_ROW * row, 200, 16),
                             Qt.AlignLeft | Qt.AlignVCenter, label)

        for values, role, line_width, dashed in self._lines:
            pen = QPen(_color(role), line_width)
            pen.setJoinStyle(Qt.RoundJoin)
            pen.setCapStyle(Qt.RoundCap)
            if dashed:
                pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawPolyline(QPolygonF([QPointF(x_of(i), y_of(v))
                                            for i, v in enumerate(values)]))
        painter.end()

    def mouseMoveEvent(self, event):
        if not self._tooltips or not self._xs:
            return
        x = event.position().x()
        nearest = min(range(len(self._xs)), key=lambda i: abs(self._xs[i] - x))
        if abs(self._xs[nearest] - x) <= 12:
            QToolTip.showText(event.globalPosition().toPoint(), self._tooltips[nearest], self)
        else:
            QToolTip.hideText()


class OddsRing(QWidget):
    """The chance the money lasts, as a ring filled that far round and the
    percentage in the middle, coloured by verdict."""

    def __init__(self, success: float, tone: str, size: int = 128, parent=None):
        super().__init__(parent)
        self._success = success
        self._role = TONE_ROLES[tone]
        self.setFixedSize(size, size)
        self.setAccessibleName(f"{round(success * 100)}% chance the money lasts")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        thickness = self.width() * 0.09
        rect = QRectF(thickness, thickness, self.width() - 2 * thickness,
                      self.height() - 2 * thickness)

        painter.setPen(QPen(_color('border'), thickness))
        painter.drawEllipse(rect)
        pen = QPen(_color(self._role), thickness)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        # Qt measures in sixteenths of a degree, anticlockwise from three
        # o'clock; this runs clockwise from twelve.
        painter.drawArc(rect, 90 * 16, -round(self._success * 360 * 16))

        font = QFont(self.font())
        font.setPointSizeF(self.font().pointSizeF() * 2.0)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(_color('text_primary'))
        painter.drawText(self.rect(), Qt.AlignCenter, f"{round(self._success * 100)}%")
        painter.end()


def _top_rounded(left: float, top: float, right: float, bottom: float,
                 radius: float) -> QPainterPath:
    """A rectangle with its top corners rounded: a column's data end, square
    where it stands on the baseline or on the part below."""
    radius = max(0.0, min(radius, bottom - top, (right - left) / 2))
    path = QPainterPath()
    path.moveTo(left, bottom)
    path.lineTo(left, top + radius)
    path.quadTo(left, top, left + radius, top)
    path.lineTo(right - radius, top)
    path.quadTo(right, top, right, top + radius)
    path.lineTo(right, bottom)
    path.closeSubpath()
    return path


class StackedBars(QWidget):
    """One column per year, its parts stacked from the baseline in series
    order, drawn from zero.

    Columns are 24px wide at most, with a rounded top and a 2px gap between
    parts where the card shows through, so neighbouring colours are told apart
    by the gap as well as the hue. series are (role, values); tooltips is one
    string per column, or None."""

    BAR_MAX = 24
    GAP = 2
    RADIUS = 4

    def __init__(self, labels, series, tooltips=None, height: int = 260, parent=None):
        super().__init__(parent)
        self._labels = list(labels)
        self._series = [(role, list(values)) for role, values in series]
        self._tooltips = list(tooltips) if tooltips else None
        self._slots: list[tuple[float, float]] = []
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)

    def paintEvent(self, event):
        count = len(self._labels)
        if not count:
            return
        left, right, top, bottom = 64, 16, 12, 30
        width = self.width() - left - right
        height = self.height() - top - bottom
        if width <= 0 or height <= 0:
            return

        totals = [sum(values[i] for _, values in self._series) for i in range(count)]
        y_max = nice_max(max(totals + [0]) * 1.02)

        def y_of(value):
            return top + height * (1 - min(max(value, 0), y_max) / y_max)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, self.font().pointSizeF() - 1.5))
        painter.setFont(small)
        muted = _color('text_secondary')

        steps = grid_steps(y_max)
        for step in range(steps + 1):
            value = y_max * step / steps
            y = y_of(value)
            pen = QPen(_color('border'), 1.0 if step == 0 else 0.6)
            if step:
                pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawLine(QPointF(left, y), QPointF(left + width, y))
            painter.setPen(muted)
            painter.drawText(QRectF(0, y - 8, left - 8, 16),
                             Qt.AlignRight | Qt.AlignVCenter, format_money_compact(value))

        slot = width / count
        bar = min(self.BAR_MAX, slot * 0.7)
        self._slots = []
        painter.setPen(Qt.NoPen)
        for index in range(count):
            middle = left + slot * (index + 0.5)
            self._slots.append((left + slot * index, left + slot * (index + 1)))
            parts = [(role, values[index]) for role, values in self._series
                     if values[index] > 0]
            stacked = 0.0
            for position, (role, value) in enumerate(parts):
                part_top = y_of(stacked + value)
                part_bottom = y_of(stacked) - (self.GAP if position else 0)
                stacked += value
                if part_bottom - part_top <= 0.5:
                    continue
                painter.setBrush(_color(role))
                if position == len(parts) - 1:
                    painter.drawPath(_top_rounded(middle - bar / 2, part_top,
                                                  middle + bar / 2, part_bottom, self.RADIUS))
                else:
                    painter.drawRect(QRectF(middle - bar / 2, part_top, bar,
                                            part_bottom - part_top))

        painter.setPen(muted)
        metrics = painter.fontMetrics()
        for index, label in enumerate(self._labels):
            if not isinstance(label, int) or label % 5:
                continue
            text = str(label)
            text_width = metrics.horizontalAdvance(text)
            middle = left + slot * (index + 0.5)
            painter.drawText(QRectF(middle - text_width / 2, top + height + 6, text_width, 16),
                             Qt.AlignCenter, text)
        painter.end()

    def mouseMoveEvent(self, event):
        if not self._tooltips or not self._slots:
            return
        x = event.position().x()
        index = next((i for i, (start, end) in enumerate(self._slots) if start <= x < end), None)
        if index is None:
            QToolTip.hideText()
            return
        QToolTip.showText(event.globalPosition().toPoint(), self._tooltips[index], self)


class MixBar(QWidget):
    """How one total divides, as a single bar of parts 2px apart, rounded at
    both ends. parts are (role, value); the zero ones are left out. tooltips,
    one per part given, name the part under the mouse."""

    GAP = 2

    def __init__(self, parts, height: int = 12, parent=None, tooltips=None):
        super().__init__(parent)
        kept = [index for index, (_, value) in enumerate(parts) if value > 0]
        self._parts = [parts[index] for index in kept]
        self._tooltips = [tooltips[index] for index in kept] if tooltips else []
        self._spans: list[tuple[float, float]] = []
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(bool(self._tooltips))

    def paintEvent(self, event):
        total = sum(value for _, value in self._parts)
        if total <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), 4, 4)
        painter.setClipPath(clip)

        room = self.width() - self.GAP * (len(self._parts) - 1)
        x = 0.0
        self._spans = []
        for role, value in self._parts:
            span = room * value / total
            painter.setBrush(_color(role))
            painter.drawRect(QRectF(x, 0, span, self.height()))
            self._spans.append((x, x + span))
            x += span + self.GAP
        painter.end()

    def mouseMoveEvent(self, event):
        if not self._tooltips:
            return
        x = event.position().x()
        index = next((i for i, (start, end) in enumerate(self._spans)
                      if start <= x <= end), None)
        if index is None:
            QToolTip.hideText()
            return
        QToolTip.showText(event.globalPosition().toPoint(), self._tooltips[index], self)


class Swatch(QWidget):
    """A legend key: a short line, a block for a shaded band, or a solid block
    for a bar's part."""

    def __init__(self, role: str, band: bool = False, dashed: bool = False,
                 block: bool = False, parent=None):
        super().__init__(parent)
        self._role, self._band, self._dashed, self._block = role, band, dashed, block
        self.setFixedSize(12 if block else 18, 12)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = _color(self._role)
        if self._block:
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(self.rect()), 2, 2)
        elif self._band:
            color.setAlphaF(_BAND_ALPHA * 2)
            painter.fillRect(self.rect(), color)
        else:
            pen = QPen(color, 3)
            if self._dashed:
                pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            middle = self.height() / 2
            painter.drawLine(QPointF(1, middle), QPointF(self.width() - 1, middle))
        painter.end()
