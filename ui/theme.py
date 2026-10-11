# -----------------------------------------------------------------------------
# ui/theme.py
# Colour palettes and the generated Qt stylesheet.
#
# Qt style sheets have no variables, so the sheet is built by formatting a
# template against the active palette.
# -----------------------------------------------------------------------------

import zlib

THEMES = {
    # Plain graphite, with no colour cast in the surfaces. Their lightness runs
    # primary < input < secondary < tertiary, so the panels read as raised
    # against the window and inputs as recessed.
    'dark': {
        'label':         'Dark',
        'bg_primary':    '#17181b',
        'bg_secondary':  '#212327',
        'bg_tertiary':   '#2c2e33',
        'bg_input':      '#1c1e21',
        # Blue rather than a grey accent: Primary buttons and checked category
        # tabs paint white text straight onto this colour, and a grey light
        # enough to sit against the surfaces would leave them looking disabled.
        'accent':        '#4d90f0',
        'accent_hover':  '#3d7ad8',
        'danger':        '#e06a6a',
        'danger_hover':  '#c95a5a',
        'text_primary':  '#e6e7ea',
        'text_secondary': '#9a9ca3',
        'text_placeholder': '#5c5e66',
        'border':        '#34363c',
        'level_warn':    '#e0a44a',
        'level_danger':  '#e88080',
        'level_good':    '#5cc98a',
    },
    'light': {
        'label':         'Light',
        'bg_primary':    '#f0f0f8',
        'bg_secondary':  '#ffffff',
        'bg_tertiary':   '#e4e4f4',
        'bg_input':      '#ffffff',
        'accent':        '#7c6af7',
        'accent_hover':  '#6a59e0',
        'danger':        '#d94040',
        'danger_hover':  '#b83030',
        'text_primary':  '#1a1a2e',
        'text_secondary': '#6060a0',
        'text_placeholder': '#9090c0',
        'border':        '#d0d0e8',
        # Darker than Dark's amber and red on purpose. That pair reaches only
        # 2.19:1 and 3.26:1 on white — the colours whose one job is to catch
        # the eye were the least legible thing on the panel.
        'level_warn':    '#8a5a00',
        'level_danger':  '#b71c1c',
        'level_good':    '#1b6e3a',
    },
}

DEFAULT_THEME = 'dark'

# Base point size per font-size setting; every other size scales from it.
FONT_SIZES = {
    'small':   9,
    'default': 10,
    'large':   12,
    'xl':      14,
}

# Every font size in the sheet, as a multiple of the base above, so headings
# and captions keep their hierarchy at every Font Size setting. QSS drops
# relative units, so stylesheet() interpolates these. The comment beside each is
# its pixel size at the 'default' base.
FONT_SCALES = {
    'micro':   0.75,    # 10px — the theme swatch caption
    'caption': 0.85,    # 11px — field labels, row meta, Small buttons
    'body':    1.05,    # 14px — empty-state body copy
    'section': 1.125,   # 15px — section headings, as big as a panel title
    'title':   1.125,   # 15px — nav brand, list and panel titles
    'heading': 1.50,    # 20px — detail and form titles, empty-state headings
    'display': 1.95,    # 26px — the auth screen title
}

# Category colours. Used by the dashboard charts, and by the dot that marks a
# category on an account row and on its tab — the same colour in all three, so
# a category is recognisable before its name is read.
CATEGORY_COLORS = {
    'Banking':      '#4caf50',
    'Investments':  '#2196f3',
    'Property':     '#78909c',
    'Credit Cards': '#e53935',
    'Loans':        '#ff9800',
    'Utilities':    '#9c27b0',
    'Income':       '#00bfa5',
    'Websites':     '#5c6bc0',
    'Secure Notes': '#8d6e63',
}

# Drawn from for categories the user invented, which have no entry above.
# Deliberately hues the nine built-ins leave spare — pink, cyan, lime, violet,
# amber — rather than more of the same: a custom category sitting next to Loans
# in the tab bar wants to be told apart from it, and an earlier ring that
# included another orange put two near-identical dots side by side. Slate left
# the ring when Property took it, so no custom category can share its dot.
_CUSTOM_COLORS = (
    '#ec407a', '#26c6da', '#d4e157', '#7e57c2', '#ffca28',
)


def category_color(category: str | None) -> str:
    """The colour standing for a category.

    Custom categories are assigned one from _CUSTOM_COLORS by name. crc32 rather
    than hash(): Python salts string hashing per process, so hash() would give a
    custom category a different colour on every launch."""
    if not category:
        return _CUSTOM_COLORS[0]
    known = CATEGORY_COLORS.get(category)
    if known:
        return known
    index = zlib.crc32(category.encode('utf-8')) % len(_CUSTOM_COLORS)
    return _CUSTOM_COLORS[index]

# Age colouring for due dates and last-updated stamps is a palette entry
# (level_warn / level_danger) reached through the #LevelWarn, #LevelDanger and
# #RowMeta* rules below — never an inline setStyleSheet, which outranks this
# sheet and would survive a theme change. One amber and one red for both themes
# measured 2.19:1 and 3.26:1 against the Light panel, so the colours whose whole
# job is to catch the eye were the least legible thing on it. See
# set_requirement_met in widgets.py for the same rule applied to a caption.


def palette(name: str) -> dict:
    return THEMES.get(name, THEMES[DEFAULT_THEME])


_STYLESHEET = """
/* Text colour is global; the window background is not. A universal
   background-color here would also match every plain QWidget used as a layout
   container, painting a rectangle of window background over whatever panel
   holds it — so only the window itself carries it, and panels paint their own. */
QWidget {{
    color: {text_primary};
}}

/* Every top-level window needs a background of its own, since there is no
   universal one to fall back on: QMainWindow here, QDialog further down with
   the rest of the dialog styling, and the popups under Inputs. */
QMainWindow {{
    background-color: {bg_primary};
}}

/* Labels sit on whatever panel holds them — never on their own block of
   window background. */
QLabel {{
    background-color: transparent;
}}

QToolTip {{
    background-color: {bg_tertiary};
    color: {text_primary};
    border: 1px solid {border};
    padding: 4px;
}}

/* --- Panels ------------------------------------------------------------- */

#TopNav {{
    background-color: {bg_secondary};
    border-bottom: 1px solid {border};
}}

#NavBrand {{
    font-size: {title_pt}pt;
    font-weight: bold;
    color: {text_primary};
}}

#CategoryBar {{
    background-color: {bg_secondary};
    border-bottom: 1px solid {border};
}}

#ListPanel, #DetailPanel, #DashboardPanel {{
    background-color: {bg_secondary};
    border: 1px solid {border};
    border-radius: 8px;
}}

#ListTitle, #PanelTitle {{
    font-size: {title_pt}pt;
    font-weight: bold;
}}

#AuthCard {{
    background-color: {bg_secondary};
    border: 1px solid {border};
    border-radius: 12px;
}}

#AuthTitle {{
    font-size: {display_pt}pt;
    font-weight: bold;
}}

#AuthSubtitle, #Subtle, #FieldHint {{
    color: {text_secondary};
}}

/* Which vault the login and setup screens are talking about. Monospaced weight
   rather than colour, so a filename reads as a filename among sentences. */
#AuthVault {{
    color: {accent};
    font-weight: bold;
}}

#ErrorLabel {{
    color: {danger};
}}

/* A requirement caption that is currently satisfied — the password-length hints
   on the setup screen and in Change Password. Lives here rather than as an
   inline colour so it follows the theme; see set_requirement_met in widgets.py. */
#RequirementMet {{
    color: {accent};
}}

/* Uppercased in field_label(): QSS silently drops text-transform. The size is
   interpolated because QSS also drops relative units (em, %). */
#FieldLabel {{
    color: {text_secondary};
    font-size: {caption_pt}pt;
    letter-spacing: 0.5px;
}}

/* The account name on a list row. */
#RowName {{
    font-weight: bold;
}}

/* The balance, sharing the name's line: full-strength text, the figure the row
   is consulted for, but unbolded so the name still reads as the heading. */
#RowBalance {{
    color: {text_primary};
}}

/* Small captions under an account row: the updated/due stamps, and the category
   badge shown in search results. Warn and danger carry the age colouring;
   RowMeta is the neutral one. Size tracks the app font for the same reason
   #FieldLabel does. */
#RowMeta {{
    color: {text_secondary};
    font-size: {caption_pt}pt;
}}

#RowMetaWarn {{
    color: {level_warn};
    font-size: {caption_pt}pt;
}}

#RowMetaDanger {{
    color: {level_danger};
    font-size: {caption_pt}pt;
}}

/* The same age colouring on a detail-view field value, which stays body size —
   only the colour changes. */
#LevelWarn {{
    color: {level_warn};
}}

#LevelDanger {{
    color: {level_danger};
}}

/* In the text colour and larger than a card title: at 12px grey, a heading
   read as less important than the cards it was a heading for. */
#SectionHeading {{
    color: {text_primary};
    font-size: {section_pt}pt;
    font-weight: bold;
}}

/* text_secondary, not text_placeholder. This styles messages the user is meant
   to read — the empty-vault explanation, the dashboard's "no history yet" — and
   the placeholder colour measures 1.94:1 to 3.03:1 against the panel depending
   on theme, well under the 4.5:1 needed for body text. */
#Placeholder {{
    color: {text_secondary};
    font-size: {body_pt}pt;
}}

/* The empty-state heading. Sized in points from the app font for the same
   reason #FieldLabel is — QSS drops relative units without a word. */
#PlaceholderTitle {{
    font-size: {heading_pt}pt;
    font-weight: bold;
}}

/* The secondary line under an empty state. Quieter than the body through size
   rather than a fainter colour, which is the part that has to stay readable. */
#PlaceholderHint {{
    color: {text_secondary};
    font-size: {caption_pt}pt;
}}

#DetailTitle, #FormTitle {{
    font-size: {heading_pt}pt;
    font-weight: bold;
}}

#MaskedValue {{
    color: {text_secondary};
    letter-spacing: 2px;
}}

#LinkValue {{
    color: {accent};
}}

#Toast {{
    background-color: {bg_tertiary};
    color: {text_primary};
    border: 1px solid {border};
    border-radius: 8px;
    padding: 10px 16px;
}}

#ToastText {{
    color: {text_primary};
}}

/* The toast's own action — an undo, most often. Deliberately looks like a
   link rather than a button: it sits inside a message that is about to
   disappear, and a filled button there reads as the primary thing on screen
   when it is the smaller half of a passing notice. */
#ToastAction {{
    background: transparent;
    border: none;
    padding: 0 2px;
    color: {accent};
    font-weight: 600;
}}

#ToastAction:hover {{
    text-decoration: underline;
}}

#WarningBox {{
    background-color: {bg_tertiary};
    border: 1px solid {border};
    border-radius: 8px;
}}

/* --- Dashboard cards ----------------------------------------------------- */

/* The trend cards' headings, and the investing summary in the same style.
   Only a card's *colour* is still set inline: on a category card it is data
   (which category this is) rather than theme, exactly like the colour its
   chart is drawn in, and the dashboard is rebuilt whenever the theme changes.
   Everything else a theme owns lives here. */
#CardTitle, #CardValue {{
    font-weight: bold;
}}

/* The change line under a card's figure. Coloured by whether the
   change was good news, which is not the same as which way it went: a credit
   card balance climbing draws the same arrow as savings climbing and means the
   opposite, so dashboard_view.py picks the name and liabilities invert.

   level_good is its own palette entry rather than a constant here, for exactly
   the reason level_warn and level_danger are: one green cannot serve both a
   near-black panel and a white one. The bright green Dark uses measures about
   3.4:1 on the Light theme's white card — under the 4.5:1 body text needs — so
   Light carries a darker one. */
#DeltaGood {{
    color: {level_good};
    font-size: {caption_pt}pt;
}}

#DeltaBad {{
    color: {level_danger};
    font-size: {caption_pt}pt;
}}

#DeltaFlat {{
    color: {text_secondary};
    font-size: {caption_pt}pt;
}}

/* --- Menus --------------------------------------------------------------- */

/* A menu is a top-level window of its own, so neither QMainWindow nor QDialog
   reaches it — left unpainted it falls back to the platform palette and shows
   a light grey popup carrying the global near-white QWidget text. Same reason
   the combo and calendar popups are painted explicitly above. */
QMenu {{
    background-color: {bg_secondary};
    border: 1px solid {border};
    border-radius: 8px;
    padding: 5px;
}}

QMenu::item {{
    background-color: transparent;
    padding: 6px 26px 6px 14px;
    border-radius: 5px;
}}

QMenu::item:selected {{
    background-color: {accent};
    color: #ffffff;
}}

QMenu::item:disabled {{
    color: {text_placeholder};
}}

QMenu::separator {{
    height: 1px;
    background-color: {border};
    margin: 5px 8px;
}}

/* --- Inputs ------------------------------------------------------------- */

/* QPlainTextEdit is listed separately everywhere below because it is not a
   QTextEdit — both descend from QAbstractScrollArea — so it inherits none of
   these rules by subclass and renders in the platform's own colours if left
   out, which is a white box in every dark theme here. */
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QDateEdit, QSpinBox {{
    background-color: {bg_input};
    color: {text_primary};
    border: 1px solid {border};
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: {accent};
    selection-color: #ffffff;
}}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QDateEdit:focus,
QSpinBox:focus {{
    border: 1px solid {accent};
}}

QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled,
QComboBox:disabled, QDateEdit:disabled, QSpinBox:disabled {{
    color: {text_placeholder};
}}

QComboBox QAbstractItemView {{
    background-color: {bg_input};
    color: {text_primary};
    border: 1px solid {border};
    selection-background-color: {accent};
    selection-color: #ffffff;
    outline: none;
}}

QCheckBox {{
    spacing: 8px;
}}

QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {border};
    border-radius: 4px;
    background-color: {bg_input};
}}

QCheckBox::indicator:checked {{
    background-color: {accent};
    border: 1px solid {accent};
}}

/* The calendar and combo popups are top-level windows in their own right, so
   neither the QMainWindow rule nor QDialog reaches them, and the nav bar inside
   a calendar is a plain QWidget. Left unpainted they fall back to the platform
   palette — a light grey carrying near-white QWidget text in every dark theme. */
QCalendarWidget, QCalendarWidget QWidget {{
    background-color: {bg_secondary};
    alternate-background-color: {bg_secondary};
}}

/* More specific than the rule above, so the grid keeps the input background. */
QCalendarWidget QAbstractItemView:enabled {{
    background-color: {bg_input};
    color: {text_primary};
    selection-background-color: {accent};
    selection-color: #ffffff;
}}

QCalendarWidget QToolButton {{
    color: {text_primary};
    background-color: {bg_secondary};
}}

/* The Today button date_edit() puts under the grid. Two type selectors beat the
   one in the plain QPushButton rule below, so the calendar rule above would
   otherwise hand the button the popup's own background and leave it a hollow
   outline. Hover needs no rule of its own: a pseudo-class outranks a second
   type either way. */
QCalendarWidget QPushButton {{
    background-color: {bg_tertiary};
}}

/* --- Buttons ------------------------------------------------------------ */

QPushButton {{
    background-color: {bg_tertiary};
    color: {text_primary};
    border: 1px solid {border};
    border-radius: 6px;
    padding: 6px 12px;
}}

QPushButton:hover {{
    background-color: {accent};
    color: #ffffff;
    border: 1px solid {accent};
}}

QPushButton:disabled {{
    color: {text_placeholder};
    background-color: {bg_tertiary};
    border: 1px solid {border};
}}

QPushButton#Primary {{
    background-color: {accent};
    color: #ffffff;
    border: 1px solid {accent};
    padding: 8px 16px;
    font-weight: bold;
}}

QPushButton#Primary:hover {{
    background-color: {accent_hover};
    border: 1px solid {accent_hover};
}}

QPushButton#Danger {{
    background-color: transparent;
    color: {danger};
    border: 1px solid {danger};
}}

QPushButton#Danger:hover {{
    background-color: {danger};
    color: #ffffff;
}}

/* A disabled Delete has to read as disabled. The plain QPushButton:disabled
   rule cannot do it: #Danger is an id selector and outranks a bare pseudo-class,
   so the button would keep its live red while refusing to be clicked. */
QPushButton#Danger:disabled {{
    background-color: transparent;
    color: {text_placeholder};
    border: 1px solid {border};
}}

/* Nav buttons carry an icon and no label, so there is no text for the platform
   style to size them from — the padding comes off and _build_nav's fixed width
   does the sizing. */
QPushButton#NavIcon, QPushButton#NavDanger {{
    padding: 0px;
}}

/* The gear button owns the vault menu, and a QPushButton with a menu draws a
   drop-down arrow beside its content by default. There is no room for one
   inside 40px already holding a 20px icon, and it would be the only chrome of
   its kind in the bar. */
QPushButton#NavIcon::menu-indicator {{
    image: none;
    width: 0px;
}}

/* The quit button: Danger's colouring, but nav geometry. It cannot simply be
   #Danger, since a widget carries one object name and this one needs both. */
QPushButton#NavDanger {{
    background-color: transparent;
    color: {danger};
    border: 1px solid {danger};
}}

QPushButton#NavDanger:hover {{
    background-color: {danger};
    color: #ffffff;
    border: 1px solid {danger};
}}

/* Nav status chips: the clipboard and idle-lock countdowns. Quiet by default —
   they sit in the nav bar for minutes at a time and must not compete with the
   content — and escalating to amber through the `level` property as the lock
   deadline closes in. */
QPushButton#NavChip {{
    background-color: transparent;
    border: 1px solid {border};
    border-radius: 11px;
    color: {text_secondary};
    font-size: {caption_pt}pt;
    padding: 3px 10px 3px 7px;
}}

QPushButton#NavChip:hover {{
    background-color: {bg_tertiary};
    color: {text_primary};
    border: 1px solid {text_placeholder};
}}

QPushButton#NavChip[level="warn"] {{
    color: {level_warn};
    border: 1px solid {level_warn};
}}

QPushButton#NavChip[level="warn"]:hover {{
    background-color: {level_warn};
    color: #ffffff;
}}

QPushButton#Ghost {{
    background-color: transparent;
    border: 1px solid transparent;
    color: {text_secondary};
    padding: 4px 8px;
}}

QPushButton#Ghost:hover {{
    color: {text_primary};
    background-color: {bg_tertiary};
    border: 1px solid {border};
}}

/* Category tabs: checked marks the category currently being browsed. */
QPushButton#CategoryTab {{
    background-color: transparent;
    color: {text_secondary};
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 2px 6px;
}}

QPushButton#CategoryTab:hover {{
    color: {text_primary};
    background-color: {bg_tertiary};
    border: 1px solid {border};
}}

QPushButton#CategoryTab:checked {{
    background-color: {accent};
    color: #ffffff;
    border: 1px solid {accent};
    font-weight: bold;
}}

QPushButton#CategoryTab:checked:hover {{
    background-color: {accent_hover};
    border: 1px solid {accent_hover};
}}

QPushButton#Small {{
    padding: 4px 10px;
    font-size: {caption_pt}pt;
}}

QPushButton#Small:hover {{
    background-color: {accent};
    color: #ffffff;
}}

QPushButton#Swatch {{
    padding: 0px;
    background-color: transparent;
    border: 2px solid {border};
    border-radius: 8px;
}}

QPushButton#Swatch:hover {{
    border: 2px solid {text_secondary};
    background-color: transparent;
}}

QPushButton#Swatch:checked {{
    border: 2px solid {accent};
}}

/* The theme's name under its swatch. The smallest text in the app, and the last
   one still carrying a hardcoded pixel size. */
#SwatchName {{
    color: {text_secondary};
    font-size: {micro_pt}pt;
}}

QPushButton#Toggle {{
    padding: 6px 14px;
}}

QPushButton#Toggle:checked {{
    background-color: {accent};
    color: #ffffff;
    border: 1px solid {accent};
}}

/* --- Lists and tables --------------------------------------------------- */

QListWidget {{
    background-color: {bg_secondary};
    border: none;
    outline: none;
}}

QListWidget::item {{
    border-bottom: 1px solid {border};
    padding: 0px;
}}

QListWidget::item:selected {{
    background-color: {bg_tertiary};
    border-left: 3px solid {accent};
}}

QListWidget::item:hover:!selected {{
    background-color: {bg_tertiary};
}}

/* Held explicitly transparent so the item's selection and hover styling shows
   through the row widget, whatever background the platform palette would
   otherwise fill it with. */
#AccountRow {{
    background-color: transparent;
}}

QTableWidget {{
    background-color: {bg_secondary};
    gridline-color: {border};
    border: 1px solid {border};
    border-radius: 6px;
    outline: none;
}}

QTableWidget::item {{
    padding: 4px 6px;
}}

/* Cells picked for copying on the dashboard, the only tables that select.
   Marked the way a selected account row is, by a raised background and an
   accent edge, never an accent fill: a name already drawn in the accent
   colour would vanish into it. The text colour is said outright, or a light
   theme would draw its plain cells in the platform's white. */
QTableWidget::item:selected {{
    background-color: {bg_tertiary};
    color: {text_primary};
    border: 1px solid {accent};
}}

/* Same for the history table's cell widgets: the row's background wins. */
#RowActions {{
    background-color: transparent;
}}

QHeaderView::section {{
    background-color: {bg_tertiary};
    color: {text_secondary};
    border: none;
    border-bottom: 1px solid {border};
    padding: 5px 6px;
}}

/* --- Scroll areas ------------------------------------------------------- */

/* The scroll area and its viewport show the panel behind them. The viewport
   fills itself from the palette by default, so transparent has to be said. */
QScrollArea, QScrollArea > QWidget > QWidget {{
    background-color: transparent;
    border: none;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0px;
}}

QScrollBar::handle:vertical {{
    background: {border};
    border-radius: 5px;
    min-height: 30px;
}}

QScrollBar::handle:vertical:hover {{
    background: {text_placeholder};
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
}}

QScrollBar::handle:horizontal {{
    background: {border};
    border-radius: 5px;
    min-width: 30px;
}}

QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0px;
    width: 0px;
}}

QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
}}

/* --- Dialogs and splitters ---------------------------------------------- */

QDialog {{
    background-color: {bg_secondary};
}}

QSplitter::handle {{
    background-color: {border};
}}

QSplitter::handle:hover {{
    background-color: {accent};
}}

QMessageBox {{
    background-color: {bg_secondary};
}}
"""


_current_theme = DEFAULT_THEME


def current_theme() -> str:
    """The theme the application is currently wearing.

    Recorded so widgets built after startup can colour themselves without being
    handed a theme — icons are the case that needs it, since a QIcon cannot be
    recoloured by the style sheet and has to be rendered against the palette."""
    return _current_theme


def font_points(name: str, font_size: str = 'default') -> float:
    """One entry of FONT_SCALES resolved to points at a font-size setting.

    Used by stylesheet() below to interpolate every `{name}_pt` in the sheet.
    Sizing a font anywhere else means it sits outside the sheet and will not
    follow a later font-size change unless its owner rebuilds it — which is why
    nothing else calls this, and why an object name and a rule above is the way
    to size text."""
    base = FONT_SIZES.get(font_size, FONT_SIZES['default'])
    return round(base * FONT_SCALES.get(name, 1.0), 2)


# Which of categories.py's level strings carry a colour, and the suffix each
# adds to a widget's base object name: 'RowMeta' + 'Warn' is the #RowMetaWarn
# rule below, 'Level' + 'Danger' is #LevelDanger.
#
# Shared by the detail view's field values and the account list's captions, so
# the two agree on which levels are coloured. A level absent here is
# deliberately uncoloured: 'lapsed' is a record nobody rolled forward, not a bill
# due this week.
LEVEL_SUFFIX = {'warn': 'Warn', 'danger': 'Danger'}


def level_object_name(base: str, level: str) -> str:
    """The object name a widget takes for `level`: `base` when the level has no
    colour of its own, base + its suffix when it does."""
    return base + LEVEL_SUFFIX.get(level, '')


def stylesheet(theme_name: str, font_size: str = 'default') -> str:
    """The sheet for a theme, sized for a font-size setting.

    font_size is needed because no font size in the sheet can be written
    relatively — QSS honours only absolute units and drops em and % without a
    word (see the note above #FieldLabel), so every one of them is interpolated
    here from FONT_SCALES as `{name}_pt`. Fractional points are fine; Qt rounds
    them to the nearest pixel.

    This is the one place a theme becomes the active one, so it is also where
    current_theme() is updated."""
    global _current_theme
    _current_theme = theme_name if theme_name in THEMES else DEFAULT_THEME
    return _STYLESHEET.format(
        **palette(theme_name),
        **{f'{name}_pt': font_points(name, font_size) for name in FONT_SCALES},
    )
