# -----------------------------------------------------------------------------
# ui/widgets.py
# Reusable widgets: the line chart, the transient toast, and the password
# generator panel.
# -----------------------------------------------------------------------------

import secrets
from pathlib import Path

from PySide6.QtCore import (
    QByteArray, QDate, QEvent, QPoint, QRect, QRectF, QSize, QStandardPaths, Qt,
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

import auth
from categories import format_money, format_money_compact, format_month
from ui.theme import current_theme, palette


# -----------------------------------------------------------------------------
# Icons
# -----------------------------------------------------------------------------

# Stroked line icons on a shared 24x24 grid, so they carry the same optical
# weight standing next to each other.
#
# These replaced emoji. A platform substitutes its own glyph for U+1F512 or
# U+23FB — a different drawing on every OS, at whatever size and colour that
# font decided, ignoring the theme entirely. Two of them rendered in full
# colour, which is why the nav bar read as placeholder art.
_ICON_PATHS = {
    'lock':    '<rect x="3.5" y="10.5" width="17" height="10.5" rx="2"/>'
               '<path d="M7.5 10.5V7a4.5 4.5 0 0 1 9 0v3.5"/>',
    # A large body with short teeth and a centre hole. An earlier attempt used a
    # small circle with full-diameter spokes and read as a sunburst — at this
    # size the ring has to dominate or the shape becomes a star.
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
    # Sort direction. A plain shaft and head rather than the usual chevron, so
    # the two read as opposites at 14px where a chevron alone is ambiguous.
    'arrow-down': '<path d="M12 4.5v15"/><path d="M6 13.5l6 6 6-6"/>',
    'arrow-up':   '<path d="M12 19.5v-15"/><path d="M6 10.5l6-6 6 6"/>',
    # The Needs Attention empty state.
    'check':   '<path d="M4.5 12.5l5 5 10-11"/>',
    # Favourites. Two entries for one shape, because pinned and not pinned have
    # to be told apart across the width of a list row: an outline and a fill
    # differ at a glance where two shades of the same outline do not, and the
    # fill still reads for anyone who cannot see the colour carrying it.
    'star':    '<path d="M12 3 14.29 8.84 20.56 9.22 15.71 13.21 17.29 19.28 '
               '12 15.9 6.71 19.28 8.29 13.21 3.44 9.22 9.71 8.84z"/>',
    'star-filled': '<path d="M12 3 14.29 8.84 20.56 9.22 15.71 13.21 17.29 19.28 '
                   '12 15.9 6.71 19.28 8.29 13.21 3.44 9.22 9.71 8.84z"/>',
}

# Icons painted solid in their own colour rather than drawn as an outline. The
# template below is otherwise fill="none", which is what makes every other icon
# a line drawing of consistent weight.
_FILLED_ICONS = frozenset({'star-filled'})

# Which palette entries a role paints with, resting and hovered. A hover of None
# means white, which is what every accent- and danger-filled hover rule in the
# sheet puts the label in.
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
    """The screen's device pixel ratio, or 1.0 before there is a screen to ask.

    Every pixmap here is drawn at this scale and tagged with it, so strokes stay
    sharp on a scaled display instead of being upsampled from their nominal
    size."""
    screen = QGuiApplication.primaryScreen()
    return screen.devicePixelRatio() if screen else 1.0


def icon_pixmap(name: str, color: str, size: int = 18, weight: float = 2.0) -> QPixmap:
    """Renders one bundled icon at a colour and size."""
    document = _ICON_SVG.format(
        color=color, weight=weight, paths=_ICON_PATHS[name],
        fill=color if name in _FILLED_ICONS else 'none',
    )
    # Bound to a name rather than passed inline: QSvgRenderer holds the buffer
    # while it parses, and a temporary freed underneath it crashes the process.
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

    Qt style sheets cannot recolour a QIcon, so both colour states are rendered
    up front and swapped on hover. Without that the background and any label
    would turn white underneath an icon still painted in the resting colour.

    The theme is read at construction rather than passed in, because these are
    built long after startup — a form opened mid-session has no one to hand it
    the current theme."""

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
        # A button with an icon and no text exposes nothing to a screen reader —
        # it is announced as an unnamed button. The name has to be said out
        # loud, since there is no label for Qt to infer one from.
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
        """Swaps which icon this button draws, re-rendering both colour states.

        For a button whose glyph is part of its state rather than its identity —
        the sort-direction arrow flipping between up and down. A no-op when the
        icon is already the one asked for, so a caller can set it unconditionally
        on every refresh without repainting."""
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
    """A filled circle, for the category marker on rows and tabs.

    Painted rather than styled: the colour stands for a category, so it comes
    from data and cannot live in the style sheet — and an inline `background-color`
    on a QWidget is the trap the rest of this file keeps climbing out of, since
    it outranks the sheet and survives a theme change. A pixmap this small also
    costs nothing to re-render, which is what makes the swap in CategoryTab
    below cheap."""
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
    """The coloured dot marking which category an account row belongs to.

    Its own widget rather than a character in the row's text so it keeps a fixed
    optical size whatever the font setting does, and so a screen reader is not
    read a bullet it cannot interpret — the row's accessible summary already
    names the category in words."""

    SIZE = 8

    def __init__(self, color: str, size: int = SIZE, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setPixmap(dot_pixmap(color, size))
        self.setAccessibleName('')


class NavChip(QPushButton):
    """A small status pill in the nav bar, hidden until it has something to say.

    Both users of it are promises the app makes and then keeps on a timer — the
    clipboard will clear, the vault will lock — which the user previously had to
    take on faith from a toast that had already vanished. A countdown that stays
    on screen turns each one from an assertion into something watchable.

    A button rather than a label because both are worth clicking: one extends
    the session, the other clears the clipboard early."""

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
        """Recolours the chip — used to escalate the lock countdown from quiet
        to amber as the deadline closes in."""
        if role == self._role:
            return
        self._role = role
        self.setProperty('level', role)
        # A style-sheet rule keyed on a dynamic property only takes effect after
        # the widget is re-polished; Qt does not watch the property itself.
        self.style().unpolish(self)
        self.style().polish(self)
        self.apply_theme(current_theme())

    def update_text(self, text: str, tooltip: str = ''):
        self.setText(f"  {text}")
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip or text)


class ThemedIcon(QLabel):
    """A non-interactive icon that repaints when the theme changes.

    A QLabel holds a pixmap, and a pixmap has its colours already baked in — so
    unlike everything the style sheet drives, these have to be told to redraw.
    MainWindow.apply_appearance walks its children and does exactly that."""

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

    The dot is an icon rather than a child widget because a QPushButton lays out
    its own icon and text and sizes itself from the pair — a child would have to
    be positioned by hand against padding the style sheet owns.

    Two pixmaps are rendered up front and swapped on toggle, the same shape as
    IconButton's hover swap and for the same reason: a checked tab paints white
    text on the accent, and a dot left in the category colour reads as a stray
    mark against it. The aggregate tabs (Dashboard, All, Closed) pass no colour
    and get no dot, which is what distinguishes them at a glance from the
    category tabs they sit beside."""

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

        # The checked style is bold, which is wider than the font the size hint
        # was measured with — reserve the bold width up front or the label gets
        # clipped the moment the tab is selected. Fixed, not minimum: a plain
        # QPushButton's size hint carries the platform style's ~80px floor,
        # which pads short labels like 'Bank' out to more than twice the width
        # the text needs.
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
# empty value and shows blank through no_date_text. Kept here beside date_edit
# so the account form and the dialogs agree on which date means "not set" —
# they write the same fields, and a second sentinel elsewhere would be stored
# as a real date in 1900.
NO_DATE = QDate(1900, 1, 1)


class _DateEdit(QDateEdit):
    """A date field whose empty state does not strand the user in 1900.

    An empty field is one sitting on its minimum under no_date_text (see
    NO_DATE), and Qt reads that minimum as a date like any other: the calendar
    popped open on January 1900 and the arrow keys stepped to the 2nd of it, so
    reaching this year meant crossing the twentieth century first.

    Both overrides fire only while the field is empty, so one holding a real
    date behaves exactly as Qt's own does."""

    def stepBy(self, steps: int):
        """Takes an empty field to today rather than a day off the sentinel.

        The direction is dropped on purpose: from "no date at all" there is no
        day for a step to count from, and today is what any of them means."""
        if self._is_empty():
            self.setDate(QDate.currentDate())
            return
        super().stepBy(steps)

    def eventFilter(self, watched, event) -> bool:
        """Opens an empty field's calendar on the current month.

        date_edit installs this on the calendar widget. It has to run on show:
        the popup seeds the calendar from the field's own date every time it
        opens, which would undo a page set once at build time.

        Only the page moves. Handing the calendar today as its *selection*
        writes today straight back into the field — QCalendarPopup listens to
        selectionChanged — so a field meant to stay empty would acquire a date
        from having been looked at."""
        if event.type() == QEvent.Show and self._is_empty():
            today = QDate.currentDate()
            watched.setCurrentPage(today.year(), today.month())
        return super().eventFilter(watched, event)

    def _is_empty(self) -> bool:
        """Whether the field is on the sentinel its no_date_text blanks out."""
        return bool(self.specialValueText()) and self.date() == self.minimumDate()


# Width a date field needs beyond its text: the popup arrow (14) and the frame
# Qt draws around the editor (18), plus the padding and border the stylesheet
# adds (18) and slack for the text cursor. Qt's own sizeHint covers only the
# first two, which is why at the default width the arrow clips the year.
_DATE_FIELD_CHROME = 62


def date_edit(date: QDate | None = None, *, minimum: QDate | None = None,
              maximum: QDate | None = None, no_date_text: str | None = None) -> QDateEdit:
    """A calendar-popup date field in the app's MM-dd-yyyy format, defaulting to
    today and wide enough to show the whole date beside its arrow.

    minimum and maximum bound what can be picked. no_date_text turns the
    minimum into an empty-value sentinel: the field shows that text in place of
    a date whenever it sits on the bound, and an empty field then takes today
    as the day to start from rather than the sentinel — see _DateEdit."""
    field = _DateEdit()
    field.setCalendarPopup(True)
    field.setDisplayFormat('MM-dd-yyyy')
    # Bounds before the value, so a date outside them clamps rather than being
    # silently kept and then dragged to the bound by the first edit.
    if minimum is not None:
        field.setMinimumDate(minimum)
    if maximum is not None:
        field.setMaximumDate(maximum)
    if no_date_text is not None:
        field.setSpecialValueText(no_date_text)
    field.setDate(QDate.currentDate() if date is None else date)
    # Builds the popup's calendar now rather than leaving it to the first
    # click — it is what the Today button and the current-month correction
    # both hang on.
    calendar = field.calendarWidget()
    calendar.installEventFilter(field)
    _add_today_button(calendar, field)
    field.ensurePolished()
    field.setMinimumWidth(
        field.fontMetrics().horizontalAdvance('00-00-0000') + _DATE_FIELD_CHROME
    )
    return field


def _add_today_button(calendar: QCalendarWidget, field: QDateEdit):
    """Puts a Today button under the calendar popup's grid.

    QCalendarPopup takes a date and closes itself off the calendar's clicked
    signal, so emitting it is the same thing a user does by clicking a day in
    the grid — one click to today from whatever month is on screen. Today is
    read when the button is pressed, so a form left open across midnight still
    means the day the user is looking at.

    Skipped where today falls outside the field's bounds, since there the
    button could only offer a date the field would clamp away."""
    if not field.minimumDate() <= QDate.currentDate() <= field.maximumDate():
        return

    button = QPushButton('Today')
    button.setObjectName('Small')
    button.clicked.connect(lambda: calendar.clicked.emit(QDate.currentDate()))
    # Its own row: the calendar holds the nav bar and the grid flush to the
    # popup's edges, and a button sitting that tight reads as another cell.
    row = QHBoxLayout()
    row.setContentsMargins(6, 2, 6, 6)
    row.addStretch()
    row.addWidget(button)
    row.addStretch()
    calendar.layout().addLayout(row)

    # A calendar is a plain QWidget, which paints a style sheet background only
    # when asked to. Nothing had ever asked, and nothing needed to: the grid
    # covered the widget whole. This row is the first strip of a calendar the
    # user can see, and unpainted it comes up in the platform's own light grey
    # — a band at the foot of the popup that ignores the theme.
    calendar.setAttribute(Qt.WA_StyledBackground, True)
    # QCalendarWidget sizes itself from the nav bar and the grid alone, so the
    # popup would be laid out as if this row were not there and take the height
    # back out of the weeks. Polished first, because both hints are measured in
    # a font the style sheet sets.
    calendar.ensurePolished()
    button.ensurePolished()
    calendar.setMinimumHeight(calendar.sizeHint().height() + row.sizeHint().height())


def export_save_path(name: str) -> str:
    """The path a save dialog should open on for a file leaving the vault.

    QFileDialog resolves a bare filename against the process working directory.
    start.bat and the desktop shortcut both launch from the application's own
    folder, so the default place to put a CSV holding every password in the
    vault was next to the code — and, for anyone running from a clone, inside
    the git repository. Documents is a better default and one the user
    recognises; the dialog still remembers wherever they actually go.

    Falls back to the bare name if the platform has no Documents folder, which
    is the behaviour this replaces rather than a new one."""
    folder = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
    return str(Path(folder) / name) if folder else name


def password_field(placeholder: str = '', min_height: int = 0) -> QLineEdit:
    """A masked single-line input.

    Shared by the two auth screens and the four dialogs that collect a
    password; each used to build its own, and the pair had already drifted on
    whether the field carries a minimum height."""
    field = QLineEdit()
    field.setEchoMode(QLineEdit.Password)
    if placeholder:
        field.setPlaceholderText(placeholder)
    if min_height:
        field.setMinimumHeight(min_height)
    return field


def requirement_label(text: str, field: QLineEdit, met) -> QLabel:
    """A caption under a password field that recolours as the rule is met.

    `met` is called with the field's text and answers whether the requirement
    is currently satisfied. Setup and Change Password both showed one of these
    and wired it up identically."""
    label = QLabel(text)
    label.setObjectName('Subtle')
    field.textChanged.connect(lambda value: set_requirement_met(label, met(value)))
    return label


def password_requirements(field: QLineEdit) -> list[QLabel]:
    """The live checklist for a field holding a password being *set*.

    Built from auth.PASSWORD_RULES rather than from captions written here, so
    the list on screen and the errors validate_password_strength returns on
    submit cannot disagree. They did: both screens showed the length rule only,
    each with its own copy of the test, and neither mentioned the space rule
    that would reject the password anyway."""
    return [requirement_label(caption, field, met)
            for caption, met, _ in auth.PASSWORD_RULES]


# How long a revealed secret stays revealed. Deliberately in the same spirit as
# the clipboard auto-clear, and deliberately shorter: a value on screen is
# readable by anyone in the room for as long as it is up, and reading one takes
# seconds. Not a setting — the clipboard timer is configurable because it trades
# against paste workflows that genuinely need longer, and nothing here does.
REVEAL_SECONDS = 20


def reveal_toggle(label: str, on_change, parent=None) -> QPushButton:
    """A checkable Show/Hide button that re-masks itself after REVEAL_SECONDS.

    on_change(shown) does the part that differs by widget — the detail pane
    swaps a QLabel's text and style, the account form swaps a QLineEdit's echo
    mode — and everything else is the same in both and lives here: the button,
    its two labels, the accessible name that has to move with the state, and
    the timer.

    The timer is what this is really for. The detail pane grew one because a
    revealed password could otherwise sit on screen for the rest of the session
    while the clipboard, holding the same value, cleared itself after thirty
    seconds. The account form had its own copy of the button without it, so the
    same secret revealed one panel to the left stayed up indefinitely. One
    implementation is what stops that happening again.

    Parented to `parent` where given, so the timer dies with the row it belongs
    to rather than firing into a deleted widget after a redraw."""
    button = QPushButton('Show', parent)
    button.setObjectName('Small')
    button.setCheckable(True)
    # Named per field, and renamed on toggle: a pane of identical 'Show'
    # buttons is unusable when the label beside them is the only thing telling
    # them apart, and one that announces 'Show' while the value is already
    # visible says the opposite of what it will do.
    button.setAccessibleName(f"Show {label}".strip())
    # Buttons can absorb a row's spare width; these never should.
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
            # Hidden by hand — stop the timer rather than let it fire later and
            # toggle a button the user has already dealt with.
            hide_timer.stop()

    button.toggled.connect(toggled)
    return button


def to_qdate(value, default: QDate | None = None) -> QDate:
    """Reads a stored YYYY-MM-DD value into a QDate.

    Anything missing or unparseable falls back to `default`, or to today when
    no default is given — so a caller holding a "no date" sentinel says so
    rather than being handed today's date. Lives beside NO_DATE and date_edit
    because it is what feeds them, and because the account form and the dialogs
    write the same fields and had a copy each.
    """
    date = QDate.fromString(str(value)[:10], 'yyyy-MM-dd') if value else QDate()
    if date.isValid():
        return date
    return QDate.currentDate() if default is None else default


def field_label(text: str) -> QLabel:
    """The small uppercase caption that sits above a value or an input.

    Uppercased here rather than in the stylesheet. Qt Style Sheets implement a
    subset of CSS and discard unknown properties without a word, so the
    text-transform this block relied on parsed cleanly, matched cleanly, and did
    nothing — the captions had been mixed case all along.

    Styled by object name so it follows the active theme — see #FieldLabel in
    ui/theme.py."""
    label = QLabel(text.upper())
    label.setObjectName('FieldLabel')
    return label


def set_requirement_met(label: QLabel, met: bool):
    """Colours a requirement caption by whether it is currently satisfied.

    Driven by object name rather than an inline `color:` so the accent follows
    the active theme. The stylesheet is regenerated and reapplied on every theme
    change, but an inline style sheet set on the widget outranks it and would
    survive — which is how this label ended up hardcoded to one theme's accent
    in two different places, neither of them the theme actually on screen."""
    label.setObjectName('RequirementMet' if met else 'Subtle')
    label.style().unpolish(label)
    label.style().polish(label)


def clear_layout(layout):
    """Empties a layout, recursively, deleting the widgets it held.

    Nested layouts are only emptied, never deleteLater()'d: taking one out of
    its parent already detaches it, and deferring its deletion corrupts the
    parent's item list if the layout is refilled before the delete runs."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


class FlowLayout(QLayout):
    """Lays widgets out left to right, wrapping to a new line when the row runs
    out of width. Qt ships no such layout, and a row of category tabs has to
    survive a narrow window without clipping its labels."""

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
    """Filled line chart with a value axis, sparse x labels and per-point tooltips.
    Replaces the SVG chart the web build drew by hand."""

    def __init__(self, values, x_labels, tooltips, color, theme, height=130, parent=None,
                 x_positions=None):
        super().__init__(parent)
        self._values = [float(v) for v in values]
        self._x_labels = list(x_labels)
        self._tooltips = list(tooltips)
        # Optional real-time axis: one number per point (day ordinals, say). Points
        # then sit at their true spacing instead of one even slot each. Ignored when
        # every point shares a position, which would collapse the chart to a line.
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
        # between, each dropped if it would collide with one already placed. On a
        # time axis points bunch up, so spacing can't be decided by index alone.
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
        # Points can share an x on a time axis, so break ties on y — otherwise a
        # same-day cluster would always report whichever entry came first.
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


# -----------------------------------------------------------------------------
# Composition
# -----------------------------------------------------------------------------

# Deliberately thin. A part-to-whole read wants a ribbon rather than a block:
# length is the only thing carrying the measurement, and a saturated fill this
# wide would shout down the trend charts it sits above.
_BAR_HEIGHT = 14
_BAR_RADIUS = 4

# Neighbouring segments are divided by a gap in the surface colour rather than
# by a border drawn round each one. A border adds a line the data does not
# contain and darkens the whole bar; a gap just reads as a join.
_SEGMENT_GAP = 2

# No segment is ever narrower than this. A category with a row in the legend has
# to have a mark on the bar, and a share under a percent rounds to less than a
# pixel — which would leave the legend naming a colour that is nowhere on
# screen. Real vaults hit this: a credit-card balance beside a mortgage is
# routinely well under 1% of the liabilities total.
_MIN_SEGMENT = 3


class CompositionBar(QWidget):
    """A horizontal stacked bar showing how one total divides between categories.

    A stacked bar rather than a pie because the segments share a baseline and are
    compared by length, which is the encoding people read accurately; horizontal
    because the labels are category names rather than numbers.

    Identity never rests on colour alone. Every segment is named, with its amount
    and its share, in the legend the caller puts underneath, and again in the
    tooltip here. That is not only good practice: on the Light theme the category
    green, blue and amber all measure under 3:1 against the card behind them, and
    visible labels are what the palette check requires in exchange."""

    def __init__(self, parts, theme, parent=None):
        super().__init__(parent)
        # (colour, tooltip, share) per segment, already ordered largest first.
        self._parts = list(parts)
        # The card's own colour, because the gaps are it showing through.
        self._surface = palette(theme)['bg_tertiary']
        self._segments: list[tuple[QRect, str]] = []
        self.setFixedHeight(_BAR_HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)

    def _widths(self, usable: int) -> list[int]:
        """Segment widths in pixels, none of them below _MIN_SEGMENT.

        Whatever the minimum adds has to come back off the total, so it is taken
        from the widest segments — the only ones that can lose a few pixels
        without the loss being visible."""
        widths = [max(_MIN_SEGMENT, int(round(share * usable)))
                  for _, _, share in self._parts]
        overflow = sum(widths) - usable
        while overflow > 0:
            widest = max(range(len(widths)), key=lambda i: widths[i])
            if widths[widest] <= _MIN_SEGMENT:
                break   # every segment is at its floor; the clip path crops the rest
            take = min(overflow, widths[widest] - _MIN_SEGMENT)
            widths[widest] -= take
            overflow -= take
        return widths

    def paintEvent(self, event):
        if not self._parts:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), _BAR_RADIUS, _BAR_RADIUS)
        painter.fillPath(path, QColor(self._surface))
        # Clipping to the rounded track rounds the two outer ends and leaves
        # every interior join square, which is what a stacked bar wants —
        # rounding each segment separately would turn the joins into pills.
        painter.setClipPath(path)

        widths = self._widths(self.width())
        self._segments = []
        x = 0
        for index, ((color, tooltip, _share), width) in enumerate(zip(self._parts, widths)):
            last = index == len(widths) - 1
            painter.fillRect(
                QRect(x, 0, width if last else max(1, width - _SEGMENT_GAP), self.height()),
                QColor(color),
            )
            # The hit area is the whole slot, gap included: a 2px dead strip
            # between segments would flicker the tooltip as the cursor crossed.
            self._segments.append((QRect(x, 0, width, self.height()), tooltip))
            x += width

        painter.end()

    def mouseMoveEvent(self, event):
        position = event.position().toPoint()
        for rect, tooltip in self._segments:
            if rect.contains(position):
                QToolTip.showText(event.globalPosition().toPoint(), tooltip, self)
                return
        QToolTip.hideText()


class Toast(QFrame):
    """Brief status message floating over the bottom of its parent window.

    A frame holding a label rather than a label itself, because some messages
    come with something to do about them. The undo after a delete is the whole
    reason: it has to be offered where the user is already looking and for
    exactly as long as the message stands, and a modal asking "are you sure you
    were sure" is the thing an undo exists to replace.

    The action is torn down with every new message, so a toast can never show a
    button belonging to something that has already scrolled past — and the
    callback is dropped at the same moment, which is what stops a stale closure
    holding a deleted account alive after its window has passed.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName('Toast')
        self.hide()

        self._label = QLabel()
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
        # The padding is the stylesheet's; a second set of margins here would
        # add to it rather than replace it.
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

        # parentWidget(), not parent(): the latter is typed as a QObject and has
        # no width() to ask for.
        parent = self.parentWidget()
        if parent is not None:
            self._label.setMaximumWidth(max(240, parent.width() - 160))
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self._timer.start(duration_ms)

    def dismiss(self):
        """Hides the toast and forgets whatever it was offering.

        Dropping the callback here rather than only on the next message is what
        makes the offer expire rather than merely stop being visible: an undo
        whose window has closed must not still be reachable through a button
        that happens to be laid out underneath a hidden widget."""
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

# Four of the symbols above start a formula as far as a spreadsheet is
# concerned, so a password beginning with one is evaluated instead of shown
# when a CSV export is opened — roughly one generated password in twenty.
# services/csv_export.py escapes them on the way out, which covers hand-typed and
# imported values; this keeps our own passwords from needing that treatment.
_FORMULA_LEAD = '=+-@'


def _avoid_formula_lead(characters: list):
    """Moves a non-formula character to the front, in place.

    Permuting rather than re-drawing keeps both the character multiset and the
    "one from each enabled set" guarantee exactly as chosen — only the
    arrangement changes, so the entropy cost is the fraction of orderings ruled
    out (under a tenth of a bit on the default charset). Re-rolling in a loop
    would be the obvious alternative and can hang: a one-character symbols-only
    password has no other ordering to find."""
    if not characters or characters[0] not in _FORMULA_LEAD:
        return
    eligible = [i for i, c in enumerate(characters) if c not in _FORMULA_LEAD]
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

    Nothing is generated until the panel is first shown. Generating in
    __init__ meant every password field in a form silently produced a real
    password and held it in a visible-echo QLineEdit, whether or not the user
    ever opened the generator — and clear() on hide means an accepted one does
    not sit in the widget for the rest of the form's life either."""

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
        # findData, not setCurrentText: a text that matches no item is silently
        # ignored by a non-editable combo, so the default would quietly become
        # whichever length happened to be first.
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
        # Generated when the panel is actually opened, not at construction.
        self._refresh()
        super().showEvent(event)

    def hideEvent(self, event):
        # A generated password is a secret whether or not it was used, and a
        # collapsed panel has nowhere to show it — so it is not kept. Paired
        # with the refresh above: closing and reopening offers a new one rather
        # than the one that was on screen before.
        self._preview.clear()
        super().hideEvent(event)

    def _use(self):
        if self._preview.text():
            self.accepted.emit(self._preview.text())
