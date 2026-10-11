# -----------------------------------------------------------------------------
# ui/widgets.py
# Reusable widgets and helpers: plain-text labels, themed icons and chips,
# category tabs, date fields, password fields and the reveal toggle, the flow
# layout, the line chart, the toast, and the password generator.
# -----------------------------------------------------------------------------

import secrets
from pathlib import Path

from PySide6 import QtGui
from PySide6.QtCore import (
    QByteArray, QDate, QEvent, QPoint, QRect, QSize, QStandardPaths, Qt,
    QTimer, Signal,
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QGuiApplication, QIcon, QPainter, QPainterPath,
    QPen, QPixmap, QPolygonF,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QCalendarWidget, QCheckBox, QComboBox, QDateEdit, QFrame, QHBoxLayout, QLabel,
    QLayout, QLineEdit, QPushButton, QSizePolicy, QToolTip, QVBoxLayout, QWidget,
)

from core import auth
from core.categories import format_money, format_money_compact, format_month
from services.export_io import FORMULA_LEAD
from ui.theme import current_theme, palette


# -----------------------------------------------------------------------------
# Plain text
#
# Qt guesses whether a label's text is HTML, and guesses yes for anything
# containing something tag-shaped. Vault content must never be guessed at: a
# revealed password 'xK9<b>mq2' would display as 'xK9mq2' in bold, and the
# generator's own symbol set can produce one.
# -----------------------------------------------------------------------------

def plain_label(text: str = '') -> QLabel:
    """A QLabel that always shows its text exactly as written."""
    label = QLabel()
    label.setTextFormat(Qt.PlainText)
    label.setText(text)
    return label


def selectable(label: QLabel) -> QLabel:
    """Lets a figure or a name be selected and copied, as a field in the
    detail view can."""
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


def plain_text(text) -> str:
    """text as HTML that renders exactly as written, for the places that only
    take auto-detected text: tooltips and the static QMessageBox calls."""
    return QtGui.Qt.convertFromPlainText(str(text), Qt.WhiteSpaceNormal)


# -----------------------------------------------------------------------------
# Icons
# -----------------------------------------------------------------------------

# Stroked line icons on a shared 24x24 grid, so they carry the same optical
# weight side by side. Drawn rather than emoji, which every platform renders in
# its own colours, ignoring the theme.
_ICON_PATHS = {
    'lock':    '<rect x="3.5" y="10.5" width="17" height="10.5" rx="2"/>'
               '<path d="M7.5 10.5V7a4.5 4.5 0 0 1 9 0v3.5"/>',
    # A large body with short teeth: at this size the ring has to dominate or
    # the shape reads as a star.
    'gear':    '<circle cx="12" cy="12" r="6.3"/><circle cx="12" cy="12" r="2.4"/>'
               '<path d="M18.3 12h2.9M2.8 12h2.9M12 5.7V2.8M12 18.3v2.9'
               'M16.46 7.54l2.05-2.05M5.49 18.51l2.05-2.05'
               'M16.46 16.46l2.05 2.05M5.49 5.49l2.05 2.05"/>',
    'power':   '<path d="M18.36 6.64a9 9 0 1 1-12.72 0"/><path d="M12 2.5v9.5"/>',
    'refresh': '<path d="M20.5 12a8.5 8.5 0 1 1-2.49-6.01"/>'
               '<path d="M20.5 3.5v6h-6"/>',
    # The trailing zero-length segments are the dots: a round cap on a segment
    # going nowhere draws a filled circle of the stroke's width.
    'alert':   '<path d="M12 3.6 2.7 20.4h18.6z"/><path d="M12 9.6v4.3"/>'
               '<path d="M12 17.3v0.01"/>',
    'info':    '<circle cx="12" cy="12" r="9"/><path d="M12 11.4v5.2"/>'
               '<path d="M12 7.7v0.01"/>',
    # Sort direction: shaft and head, so up and down read as opposites at 14px.
    'arrow-down': '<path d="M12 4.5v15"/><path d="M6 13.5l6 6 6-6"/>',
    'arrow-up':   '<path d="M12 19.5v-15"/><path d="M6 10.5l6-6 6 6"/>',
    'chevron-left':  '<path d="M15 5.5l-6.5 6.5 6.5 6.5"/>',
    'chevron-right': '<path d="M9 5.5l6.5 6.5-6.5 6.5"/>',
    'check':   '<path d="M4.5 12.5l5 5 10-11"/>',
    # Pinned and not pinned are an outline and a fill, which differ at a glance
    # where two shades of one outline do not.
    'star':    '<path d="M12 3 14.29 8.84 20.56 9.22 15.71 13.21 17.29 19.28 '
               '12 15.9 6.71 19.28 8.29 13.21 3.44 9.22 9.71 8.84z"/>',
    'star-filled': '<path d="M12 3 14.29 8.84 20.56 9.22 15.71 13.21 17.29 19.28 '
                   '12 15.9 6.71 19.28 8.29 13.21 3.44 9.22 9.71 8.84z"/>',
}

# Icons painted solid rather than drawn as an outline.
_FILLED_ICONS = frozenset({'star-filled'})

# Which palette entries a role paints with, resting and hovered. A hover of None
# means white, which is what every filled hover rule in the sheet uses.
_ICON_ROLES = {
    'default': ('text_primary', None),
    'danger':  ('danger', None),
    'accent':  ('accent', None),
    # Ghost buttons do not fill on hover; they lift the text instead.
    'ghost':   ('text_secondary', 'text_primary'),
    'subtle':  ('text_secondary', None),
}

_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="{fill}" '
    'stroke="{color}" stroke-width="{weight}" stroke-linecap="round" '
    'stroke-linejoin="round">{paths}</svg>'
)


def _device_ratio() -> float:
    """The screen's device pixel ratio, or 1.0 before there is a screen. Pixmaps
    are drawn at this scale so strokes stay sharp on a scaled display."""
    screen = QGuiApplication.primaryScreen()
    return screen.devicePixelRatio() if screen else 1.0


def icon_pixmap(name: str, color: str, size: int = 18, weight: float = 2.0) -> QPixmap:
    """Renders one bundled icon at a colour and size."""
    document = _ICON_SVG.format(
        color=color, weight=weight, paths=_ICON_PATHS[name],
        fill=color if name in _FILLED_ICONS else 'none',
    )
    # Bound to a name: QSvgRenderer holds the buffer while it parses, and a
    # temporary freed underneath it crashes the process.
    payload = QByteArray(document.encode('utf-8'))
    renderer = QSvgRenderer(payload)

    ratio = _device_ratio()
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


class IconButton(QPushButton):
    """A button whose icon follows the active theme.

    Style sheets cannot recolour a QIcon, so both colour states are rendered up
    front and swapped on hover. The theme is read at construction, because these
    are built long after startup."""

    def __init__(self, name: str, tooltip: str = '', role: str = 'default',
                 text: str = '', size: int = 18, label: str = '', parent=None):
        super().__init__(text, parent)
        self._icon_name = name
        self._role = role
        self._icon_px = size
        self._rest_icon = None
        self._hover_icon = None

        if tooltip:
            self.setToolTip(tooltip)
        # An icon-only button is otherwise an unnamed button to a screen reader.
        self.setAccessibleName(label or text or tooltip)
        if tooltip and tooltip != self.accessibleName():
            self.setAccessibleDescription(tooltip)
        self.setIconSize(QSize(size, size))
        self.setCursor(Qt.PointingHandCursor)
        self.apply_theme(current_theme())

    def apply_theme(self, theme: str):
        colors = palette(theme)
        rest_key, hover_key = _ICON_ROLES.get(self._role, _ICON_ROLES['default'])
        self._rest_icon = QIcon(
            icon_pixmap(self._icon_name, colors[rest_key], self._icon_px)
        )
        self._hover_icon = QIcon(icon_pixmap(
            self._icon_name, colors[hover_key] if hover_key else '#ffffff', self._icon_px
        ))
        self.setIcon(self._hover_icon if self.underMouse() else self._rest_icon)

    def set_icon_name(self, name: str):
        """Swaps which icon this button draws. A no-op when it is already the
        one asked for, so callers can set it on every refresh."""
        if name == self._icon_name:
            return
        self._icon_name = name
        self.apply_theme(current_theme())

    def enterEvent(self, event):
        self.setIcon(self._hover_icon)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setIcon(self._rest_icon)
        super().leaveEvent(event)


def dot_pixmap(color: str, size: int = 8) -> QPixmap:
    """A filled circle, for the category marker on rows and tabs. Painted
    because the colour comes from data, and an inline background-color on a
    widget would outrank the style sheet and survive a theme change."""
    ratio = _device_ratio()
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(pixmap.rect().adjusted(0, 0, -1, -1))
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


class CategoryDot(QLabel):
    """The coloured dot marking which category an account row belongs to. Its
    own widget so it keeps a fixed size whatever the font does, and is not read
    aloud — the row's accessible summary names the category."""

    SIZE = 8

    def __init__(self, color: str, size: int = SIZE, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setPixmap(dot_pixmap(color, size))
        self.setAccessibleName('')


class NavChip(QPushButton):
    """A small status pill in the nav bar, hidden until it has something to say.

    Shows a countdown the app is keeping — the clipboard will clear, the vault
    will lock — and clicking it acts early: clear now, or stay unlocked."""

    def __init__(self, icon_name: str, role: str = 'subtle', parent=None):
        super().__init__(parent)
        self._icon_name = icon_name
        self._role = role
        self.setObjectName('NavChip')
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(13, 13))
        self.hide()
        self.apply_theme(current_theme())

    def apply_theme(self, theme: str):
        rest_key, _ = _ICON_ROLES.get(self._role, _ICON_ROLES['subtle'])
        self.setIcon(QIcon(icon_pixmap(self._icon_name, palette(theme)[rest_key], 13)))

    def set_role(self, role: str):
        """Recolours the chip, escalating the lock countdown to amber near the end."""
        if role == self._role:
            return
        self._role = role
        self.setProperty('level', role)
        # A rule keyed on a dynamic property only applies after a re-polish.
        self.style().unpolish(self)
        self.style().polish(self)
        self.apply_theme(current_theme())

    def update_text(self, text: str, tooltip: str = ''):
        self.setText(f"  {text}")
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip or text)


class ThemedIcon(QLabel):
    """A non-interactive icon that repaints when the theme changes.
    MainWindow.apply_appearance walks these and redraws them."""

    def __init__(self, name: str, role: str = 'accent', size: int = 20, parent=None):
        super().__init__(parent)
        self._icon_name = name
        self._role = role
        self._icon_px = size
        self.setFixedSize(size, size)
        self.apply_theme(current_theme())

    def apply_theme(self, theme: str):
        rest_key, _ = _ICON_ROLES.get(self._role, _ICON_ROLES['accent'])
        self.setPixmap(icon_pixmap(self._icon_name, palette(theme)[rest_key], self._icon_px))

    def set_icon_name(self, name: str, role: str | None = None):
        """Swaps the glyph, and optionally the palette role it is drawn in."""
        if name == self._icon_name and (role is None or role == self._role):
            return
        self._icon_name = name
        if role is not None:
            self._role = role
        self.apply_theme(current_theme())


class CategoryTab(QPushButton):
    """One tab in the category bar, optionally carrying its category's colour.

    The dot is the button's icon, swapped for a white one when checked so it
    does not read as a stray mark on the accent background. The aggregate tabs
    pass no colour and get no dot."""

    # Room a tab needs beyond its label: the sheet's 6px padding and 1px border
    # a side, plus a couple of pixels of slack.
    _PADDING = 2 * (6 + 1) + 4
    _DOT_PX = 8
    # The icon's own width plus the gap Qt leaves between icon and text.
    _DOT_ROOM = _DOT_PX + 6

    def __init__(self, label: str, color: str | None = None, parent=None):
        super().__init__(label, parent)
        self.setObjectName('CategoryTab')
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)

        self._rest_dot = self._checked_dot = None
        if color:
            self._rest_dot = QIcon(dot_pixmap(color, self._DOT_PX))
            self._checked_dot = QIcon(dot_pixmap('#ffffff', self._DOT_PX))
            self.setIconSize(QSize(self._DOT_PX, self._DOT_PX))
            self.setIcon(self._rest_dot)
            self.toggled.connect(self._sync_dot)

        # Sized for the bold checked style, so the label is not clipped when the
        # tab is selected. Fixed rather than minimum: the platform's ~80px button
        # floor would pad short labels out to twice their width.
        self.ensurePolished()
        bold = QFont(self.font())
        bold.setBold(True)
        self.setFixedWidth(
            QFontMetrics(bold).horizontalAdvance(label) + self._PADDING
            + (self._DOT_ROOM if color else 0)
        )

    def _sync_dot(self, checked: bool):
        self.setIcon(self._checked_dot if checked else self._rest_dot)


# Stands in for "no date" in a QDateEdit: the field's minimum doubles as the
# empty value and shows blank through no_date_text. Shared so the form and the
# dialogs agree on which date means "not set".
NO_DATE = QDate(1900, 1, 1)


class _DateEdit(QDateEdit):
    """A date field whose empty state does not strand the user in 1900. Both
    overrides act only while the field is empty."""

    def stepBy(self, steps: int):
        """Takes an empty field to today rather than a day off the sentinel."""
        if self._is_empty():
            self.setDate(QDate.currentDate())
            return
        super().stepBy(steps)

    def eventFilter(self, watched, event) -> bool:
        """Opens an empty field's calendar on the current month.

        Only the page moves: setting the calendar's *selection* would write
        today back into the field, giving an empty field a date for having been
        looked at."""
        if event.type() == QEvent.Show and self._is_empty():
            today = QDate.currentDate()
            watched.setCurrentPage(today.year(), today.month())
        return super().eventFilter(watched, event)

    def _is_empty(self) -> bool:
        """Whether the field is on the sentinel its no_date_text blanks out."""
        return bool(self.specialValueText()) and self.date() == self.minimumDate()


# Width a date field needs beyond its text: the popup arrow, Qt's frame, and the
# stylesheet's padding and border. Qt's own sizeHint clips the year.
_DATE_FIELD_CHROME = 62


def date_edit(date: QDate | None = None, *, minimum: QDate | None = None,
              maximum: QDate | None = None, no_date_text: str | None = None) -> QDateEdit:
    """A calendar-popup date field in the app's MM-dd-yyyy format, defaulting to
    today. no_date_text turns the minimum into an empty-value sentinel."""
    field = _DateEdit()
    field.setCalendarPopup(True)
    field.setDisplayFormat('MM-dd-yyyy')
    # Bounds before the value, so a date outside them clamps immediately.
    if minimum is not None:
        field.setMinimumDate(minimum)
    if maximum is not None:
        field.setMaximumDate(maximum)
    if no_date_text is not None:
        field.setSpecialValueText(no_date_text)
    field.setDate(QDate.currentDate() if date is None else date)
    calendar = field.calendarWidget()
    calendar.installEventFilter(field)
    _add_today_button(calendar, field)
    field.ensurePolished()
    field.setMinimumWidth(
        field.fontMetrics().horizontalAdvance('00-00-0000') + _DATE_FIELD_CHROME
    )
    return field


def _add_today_button(calendar: QCalendarWidget, field: QDateEdit):
    """Puts a Today button under the calendar popup's grid. Emitting the
    calendar's clicked signal is exactly what clicking a day does. Skipped where
    today is outside the field's bounds."""
    if not field.minimumDate() <= QDate.currentDate() <= field.maximumDate():
        return

    button = QPushButton('Today')
    button.setObjectName('Small')
    button.clicked.connect(lambda: calendar.clicked.emit(QDate.currentDate()))
    row = QHBoxLayout()
    row.setContentsMargins(6, 2, 6, 6)
    row.addStretch()
    row.addWidget(button)
    row.addStretch()
    calendar.layout().addLayout(row)

    # The calendar only paints a style-sheet background when asked; this row is
    # the first visible strip of it.
    calendar.setAttribute(Qt.WA_StyledBackground, True)
    # QCalendarWidget sizes itself from the nav bar and grid alone, so the row's
    # height has to be added or it is taken out of the weeks.
    calendar.ensurePolished()
    button.ensurePolished()
    calendar.setMinimumHeight(calendar.sizeHint().height() + row.sizeHint().height())


def export_save_path(name: str) -> str:
    """The path a save dialog should open on for a file leaving the vault:
    Documents, rather than the working directory — which for a shortcut launch
    is the application folder, and for a clone is the git repository. The bare
    name if the platform has no Documents folder."""
    folder = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
    return str(Path(folder) / name) if folder else name


def password_field(placeholder: str = '', min_height: int = 0) -> QLineEdit:
    """A masked single-line input."""
    field = QLineEdit()
    field.setEchoMode(QLineEdit.Password)
    if placeholder:
        field.setPlaceholderText(placeholder)
    if min_height:
        field.setMinimumHeight(min_height)
    return field


def requirement_label(text: str, field: QLineEdit, met) -> QLabel:
    """A caption under a password field that recolours as the rule is met.
    `met` is called with the field's text."""
    label = QLabel(text)
    label.setObjectName('Subtle')
    field.textChanged.connect(lambda value: set_requirement_met(label, met(value)))
    return label


def password_requirements(field: QLineEdit) -> list[QLabel]:
    """The live checklist for a field holding a password being *set*, built
    from auth.PASSWORD_RULES so it cannot disagree with the submit-time check."""
    return [requirement_label(caption, field, met)
            for caption, met, _ in auth.PASSWORD_RULES]


# How long a revealed secret stays revealed. Shorter than the clipboard timer:
# a value on screen is readable by anyone in the room.
REVEAL_SECONDS = 20


def reveal_toggle(label: str, on_change, parent=None) -> QPushButton:
    """A checkable Show/Hide button that re-masks itself after REVEAL_SECONDS.

    on_change(shown) does the part that differs by widget — the detail pane
    swaps a label's text, the form swaps an echo mode. Parented to `parent` so
    the timer dies with the row rather than firing into a deleted widget."""
    button = QPushButton('Show', parent)
    button.setObjectName('Small')
    button.setCheckable(True)
    # Named per field, and renamed on toggle, so a screen reader can tell a pane
    # of these apart and hears what clicking will do.
    button.setAccessibleName(f"Show {label}".strip())
    button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    hide_timer = QTimer(button)
    hide_timer.setSingleShot(True)
    hide_timer.timeout.connect(lambda: button.setChecked(False))

    def toggled(shown: bool):
        button.setText('Hide' if shown else 'Show')
        button.setAccessibleName(f"{'Hide' if shown else 'Show'} {label}".strip())
        on_change(shown)
        if shown:
            hide_timer.start(REVEAL_SECONDS * 1000)
        else:
            hide_timer.stop()

    button.toggled.connect(toggled)
    return button


def to_qdate(value, default: QDate | None = None) -> QDate:
    """Reads a stored YYYY-MM-DD value into a QDate. Anything missing or
    unparseable falls back to `default`, or to today."""
    date = QDate.fromString(str(value)[:10], 'yyyy-MM-dd') if value else QDate()
    if date.isValid():
        return date
    return QDate.currentDate() if default is None else default


def field_label(text: str) -> QLabel:
    """The small uppercase caption above a value or an input. Uppercased here
    because Qt style sheets silently ignore text-transform."""
    label = plain_label(text.upper())
    label.setObjectName('FieldLabel')
    return label


def set_requirement_met(label: QLabel, met: bool):
    """Colours a requirement caption by whether it is currently satisfied.
    By object name, so the colour follows the theme."""
    label.setObjectName('RequirementMet' if met else 'Subtle')
    label.style().unpolish(label)
    label.style().polish(label)


def clear_layout(layout):
    """Empties a layout, recursively, deleting the widgets it held.

    Nested layouts are only emptied, never deleteLater()'d: taking one out of
    its parent already detaches it, and a deferred delete corrupts the parent
    if it is refilled first."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


class FlowLayout(QLayout):
    """Lays widgets out left to right, wrapping when the row runs out of width.
    Qt ships no such layout."""

    def __init__(self, parent=None, margin=0, spacing=6):
        super().__init__(parent)
        self._items: list = []
        self._spacing = spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        left, top, right, bottom = self.getContentsMargins()
        return size + QSize(left + right, top + bottom)

    def _layout(self, rect: QRect, apply: bool) -> int:
        """Places the items and returns the total height used."""
        left, top, right, bottom = self.getContentsMargins()
        area = rect.adjusted(left, top, -right, -bottom)
        x, y, line_height = area.x(), area.y(), 0

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if line_height and next_x - self._spacing > area.right() + 1:
                x = area.x()
                y += line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + bottom


class LineChart(QWidget):
    """Filled line chart with a value axis, sparse x labels and per-point tooltips."""

    def __init__(self, values, x_labels, tooltips, color, theme, height=130, parent=None,
                 x_positions=None):
        super().__init__(parent)
        self._values = [float(v) for v in values]
        self._x_labels = list(x_labels)
        self._tooltips = list(tooltips)
        # Optional real-time axis: one number per point (day ordinals, say), so
        # points sit at their true spacing. Ignored when every point shares a
        # position, which would collapse the chart to a line.
        self._x_positions = [float(p) for p in x_positions] if x_positions else None
        if self._x_positions and (len(self._x_positions) != len(self._values)
                                  or max(self._x_positions) == min(self._x_positions)):
            self._x_positions = None
        self._color = QColor(color)
        self._theme = palette(theme)
        self._points: list[QPoint] = []
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)

    def paintEvent(self, event):
        if len(self._values) < 2:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        pad_left, pad_right, pad_top, pad_bottom = 56, 12, 12, 30
        width = self.width() - pad_left - pad_right
        height = self.height() - pad_top - pad_bottom
        if width <= 0 or height <= 0:
            return

        low, high = min(self._values), max(self._values)
        value_range = (high - low) or 1.0
        count = len(self._values)

        if self._x_positions:
            first_pos = self._x_positions[0]
            span = self._x_positions[-1] - first_pos or 1.0

            def x_of(index):
                return pad_left + ((self._x_positions[index] - first_pos) / span) * width
        else:
            def x_of(index):
                return pad_left + (index / max(count - 1, 1)) * width

        def y_of(value):
            return pad_top + height - ((value - low) / value_range) * height

        small = QFont(self.font())
        small.setPointSizeF(max(7.0, self.font().pointSizeF() - 2))
        painter.setFont(small)

        # Grid lines and value labels
        border = QColor(self._theme['border'])
        text_secondary = QColor(self._theme['text_secondary'])
        for fraction in (0.0, 0.33, 0.66, 1.0):
            value = low + value_range * fraction
            y = y_of(value)
            pen = QPen(border)
            if fraction == 0.0:
                pen.setWidthF(1.0)
            else:
                pen.setWidthF(0.5)
                pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawLine(int(pad_left), int(y), int(self.width() - pad_right), int(y))

            painter.setPen(text_secondary)
            painter.drawText(
                QRect(0, int(y) - 8, pad_left - 6, 16),
                Qt.AlignRight | Qt.AlignVCenter,
                format_money_compact(value),
            )

        self._points = [QPoint(int(x_of(i)), int(y_of(v))) for i, v in enumerate(self._values)]

        # Filled area under the line
        baseline = pad_top + height
        area = QPolygonF(
            [QPoint(self._points[0].x(), int(baseline))]
            + self._points
            + [QPoint(self._points[-1].x(), int(baseline))]
        )
        fill = QColor(self._color)
        fill.setAlphaF(0.12)
        path = QPainterPath()
        path.addPolygon(area)
        painter.fillPath(path, fill)

        # The line itself
        line_pen = QPen(self._color)
        line_pen.setWidthF(2.0)
        line_pen.setJoinStyle(Qt.RoundJoin)
        line_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(line_pen)
        painter.drawPolyline(QPolygonF(self._points))

        # Data points
        painter.setPen(QPen(QColor(self._theme['bg_secondary']), 1.5))
        painter.setBrush(self._color)
        for point in self._points:
            painter.drawEllipse(point, 3, 3)
        painter.setBrush(Qt.NoBrush)

        # Sparse x labels: the two edges always, then evenly-spaced candidates in
        # between, each dropped if it would collide with one already placed.
        painter.setPen(text_secondary)
        metrics = painter.fontMetrics()
        label_y = self.height() - pad_bottom + 4
        taken: list[tuple[int, int]] = []

        def place(index, align):
            label = self._x_labels[index] if index < len(self._x_labels) else ''
            if not label:
                return
            text_width = metrics.horizontalAdvance(label)
            x = self._points[index].x()
            if align == Qt.AlignLeft:
                left = x - 4
            elif align == Qt.AlignRight:
                left = x + 4 - text_width
            else:
                left = x - text_width // 2
            span = (left - 8, left + text_width + 8)
            if any(span[0] < end and start < span[1] for start, end in taken):
                return
            taken.append(span)
            painter.drawText(QRect(left, label_y, text_width, 16),
                             Qt.AlignLeft | Qt.AlignTop, label)

        place(0, Qt.AlignLeft)
        if count > 1:
            place(count - 1, Qt.AlignRight)
        step = max(1, -(-count // 5))
        for index in range(step, count - 1, step):
            place(index, Qt.AlignHCenter)

        painter.end()

    def mouseMoveEvent(self, event):
        """Shows the tooltip for the nearest point when the cursor is close to it."""
        if not self._points:
            return
        position = event.position().toPoint()
        # Points can share an x on a time axis, so break ties on y.
        candidates = [i for i, p in enumerate(self._points) if abs(p.x() - position.x()) <= 12]
        nearest = min(candidates, key=lambda i: abs(self._points[i].y() - position.y()), default=None)
        if nearest is not None and nearest < len(self._tooltips):
            QToolTip.showText(event.globalPosition().toPoint(), self._tooltips[nearest], self)
        else:
            QToolTip.hideText()


def build_chart(values, x_labels, tooltips, color, theme, height=130,
                x_positions=None) -> LineChart | None:
    """Builds a chart, or None when there aren't at least two points to draw."""
    if len([v for v in values if v is not None]) < 2:
        return None
    return LineChart(values, x_labels, tooltips, color, theme, height,
                     x_positions=x_positions)


def build_series_chart(months, values, color, theme, height=120) -> LineChart | None:
    """Trims leading/trailing gaps from a monthly series, carries the last known
    value across interior gaps, and charts the result."""
    first = next((i for i, v in enumerate(values) if v is not None), None)
    if first is None:
        return None
    last = len(values) - 1
    while last > first and values[last] is None:
        last -= 1
    if last - first < 1:
        return None

    month_slice = months[first:last + 1]
    carried = None
    cleaned = []
    for value in values[first:last + 1]:
        if value is not None:
            carried = value
        cleaned.append(carried)

    labels = [format_month(m) for m in month_slice]
    tooltips = [f"{labels[i]}: {format_money(cleaned[i])}" for i in range(len(cleaned))]
    return build_chart(cleaned, labels, tooltips, color, theme, height)


class Toast(QFrame):
    """Brief status message floating over the bottom of its parent window,
    optionally with one action (the undo after a delete).

    The action and its callback are dropped with every new message and on
    dismissal, so an expired offer cannot be reached through a hidden button."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName('Toast')
        self.hide()

        # Messages carry account names, so never interpret them as HTML.
        self._label = plain_label()
        self._label.setObjectName('ToastText')
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setWordWrap(True)

        self._action = QPushButton()
        self._action.setObjectName('ToastAction')
        self._action.setCursor(Qt.PointingHandCursor)
        self._action.clicked.connect(self._run_action)
        self._action.hide()
        self._callback = None

        layout = QHBoxLayout(self)
        # The padding is the stylesheet's.
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(self._label)
        layout.addWidget(self._action)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)

    def show_message(self, message: str, duration_ms: int = 1800,
                     action_text: str = '', on_action=None):
        self._label.setText(message)
        self._callback = on_action if action_text and on_action else None
        self._action.setText(action_text)
        self._action.setVisible(bool(self._callback))

        parent = self.parentWidget()
        if parent is not None:
            self._label.setMaximumWidth(max(240, parent.width() - 160))
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self._timer.start(duration_ms)

    def dismiss(self):
        """Hides the toast and forgets whatever it was offering."""
        self._timer.stop()
        self._callback = None
        self._action.hide()
        self.hide()

    def _run_action(self):
        callback, self._callback = self._callback, None
        self.dismiss()
        if callback is not None:
            callback()

    def reposition(self):
        parent = self.parentWidget()
        if not parent:
            return
        x = (parent.width() - self.width()) // 2
        y = parent.height() - self.height() - 28
        self.move(max(0, x), max(0, y))


# -----------------------------------------------------------------------------
# Password generator
# -----------------------------------------------------------------------------

# Ambiguous characters excluded: O/0, I/l/1 — hard to distinguish when reading.
_UPPER   = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
_LOWER   = 'abcdefghjkmnpqrstuvwxyz'
_DIGITS  = '23456789'
_SYMBOLS = '!@#$%^&*_+-=[]{}|;:,.<>?'


def _avoid_formula_lead(characters: list):
    """Moves a character a spreadsheet would not read as a formula to the front,
    in place, so a generated password needs no escaping in a CSV export.

    Permuting rather than re-drawing keeps the character multiset and the
    "one from each enabled set" guarantee intact, and cannot loop forever on a
    one-character symbols-only password."""
    if not characters or characters[0] not in FORMULA_LEAD:
        return
    eligible = [i for i, c in enumerate(characters) if c not in FORMULA_LEAD]
    if not eligible:
        return  # every character is a formula lead; nothing to swap in
    swap = eligible[secrets.randbelow(len(eligible))]
    characters[0], characters[swap] = characters[swap], characters[0]


def generate_password(length=20, upper=True, lower=True, digits=True, symbols=True) -> str:
    """Generates a password holding at least one character from each enabled set,
    never starting with a character a spreadsheet would treat as a formula."""
    sets = [s for s, enabled in
            ((_UPPER, upper), (_LOWER, lower), (_DIGITS, digits), (_SYMBOLS, symbols))
            if enabled]
    if not sets:
        return ''

    required = [secrets.choice(s) for s in sets]
    charset = ''.join(sets)
    extra = [secrets.choice(charset) for _ in range(max(0, length - len(required)))]

    characters = required + extra
    # Shuffle so the required characters aren't always at the front.
    for i in range(len(characters) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        characters[i], characters[j] = characters[j], characters[i]
    _avoid_formula_lead(characters)
    return ''.join(characters)


_LENGTHS = (12, 16, 20, 24, 32)
_DEFAULT_LENGTH = 20


class GeneratorPanel(QWidget):
    """Collapsible password generator shown beneath a password field.

    Nothing is generated until the panel is shown, and the preview is cleared
    when it hides, so a generated secret never sits in a widget unasked for."""

    accepted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)

        options = QHBoxLayout()
        options.setContentsMargins(0, 0, 0, 0)
        options.setSpacing(10)

        options.addWidget(QLabel('Length'))
        self._length = QComboBox()
        for value in _LENGTHS:
            self._length.addItem(str(value), value)
        self._length.setCurrentIndex(self._length.findData(_DEFAULT_LENGTH))
        self._length.currentIndexChanged.connect(self._refresh)
        options.addWidget(self._length)

        self._toggles = {}
        for key, label in (('upper', 'A–Z'), ('lower', 'a–z'),
                           ('digits', '0–9'), ('symbols', 'Symbols')):
            box = QCheckBox(label)
            box.setChecked(True)
            box.checkStateChanged.connect(self._refresh)
            self._toggles[key] = box
            options.addWidget(box)
        options.addStretch(1)

        self._preview = QLineEdit()
        self._preview.setReadOnly(True)

        regenerate = IconButton('refresh', text='Regenerate', size=13)
        regenerate.setObjectName('Small')
        regenerate.clicked.connect(self._refresh)

        use = QPushButton('Use Password')
        use.setObjectName('Primary')
        use.clicked.connect(self._use)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.addWidget(regenerate)
        actions.addStretch(1)
        actions.addWidget(use)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(options)
        layout.addWidget(self._preview)
        layout.addLayout(actions)

    def _options(self) -> dict:
        return {
            'length': self._length.currentData(),
            **{key: box.isChecked() for key, box in self._toggles.items()},
        }

    def _refresh(self):
        options = self._options()
        if any(options[k] for k in ('upper', 'lower', 'digits', 'symbols')):
            self._preview.setText(generate_password(**options))
            self._preview.setPlaceholderText('')
        else:
            self._preview.clear()
            self._preview.setPlaceholderText('Select at least one character set')

    def showEvent(self, event):
        self._refresh()
        super().showEvent(event)

    def hideEvent(self, event):
        # A generated password is a secret whether or not it was used.
        self._preview.clear()
        super().hideEvent(event)

    def _use(self):
        if self._preview.text():
            self.accepted.emit(self._preview.text())
