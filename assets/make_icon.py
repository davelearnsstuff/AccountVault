"""Draws assets/accountvault.ico — a padlock on a rounded accent square.

Each size is drawn natively rather than downscaled from one large canvas: a
256px padlock squeezed to 16px turns to mush, whereas geometry expressed as a
fraction of the canvas keeps the strokes on whole pixels at every size.
"""
import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication

SIZES = (16, 24, 32, 48, 64, 128, 256)

ACCENT = '#7c6af7'   # The mark's own violet. Independent of the themes, since
                     # the icon is drawn once and shown by the OS, not the app.
WHITE = '#ffffff'


def draw(s: int) -> QImage:
    img = QImage(s, s, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)

    # Rounded background tile.
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(ACCENT))
    radius = s * 0.22
    p.drawRoundedRect(QRectF(0, 0, s, s), radius, radius)

    # Shackle: a half-circle with two legs dropping to the body.
    arc_w = s * 0.30
    arc_x = (s - arc_w) / 2
    arc_y = s * 0.24
    arc_mid_y = arc_y + arc_w / 2
    body_y = s * 0.49

    path = QPainterPath()
    path.moveTo(arc_x, body_y)
    path.lineTo(arc_x, arc_mid_y)
    path.arcTo(QRectF(arc_x, arc_y, arc_w, arc_w), 180, -180)
    path.lineTo(arc_x + arc_w, body_y)

    pen = QPen(QColor(WHITE))
    pen.setWidthF(max(1.5, s * 0.085))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)

    # Lock body.
    body_w, body_h = s * 0.50, s * 0.31
    body_x = (s - body_w) / 2
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(WHITE))
    p.drawRoundedRect(QRectF(body_x, body_y, body_w, body_h), s * 0.06, s * 0.06)

    # Keyhole, only where it will read as a shape rather than a smudge.
    if s >= 32:
        kh = s * 0.085
        p.setBrush(QColor(ACCENT))
        p.drawEllipse(QRectF((s - kh) / 2, body_y + body_h * 0.28, kh, kh))

    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    # The QByteArray is bound to a name deliberately: passed inline it is a
    # temporary, Python frees it while QBuffer still points at it, and the
    # process segfaults on save().
    store = QByteArray()
    buf = QBuffer(store)
    buf.open(QBuffer.WriteOnly)
    img.save(buf, 'PNG')
    buf.close()
    return bytes(store)


def build_ico(images: list[QImage]) -> bytes:
    """Assembles a multi-size .ico with PNG payloads.

    Qt's own ICO writer emits a single image; Windows picks the right size per
    context (16 in the title bar, 32 in Alt-Tab, 256 in large previews) only if
    they are all in the one file, so the container is written by hand.
    """
    blobs = [png_bytes(i) for i in images]
    header = struct.pack('<HHH', 0, 1, len(images))
    offset = len(header) + 16 * len(images)

    entries, payload = b'', b''
    for img, blob in zip(images, blobs):
        dim = 0 if img.width() >= 256 else img.width()
        entries += struct.pack('<BBBBHHII', dim, dim, 0, 0, 1, 32, len(blob), offset)
        payload += blob
        offset += len(blob)
    return header + entries + payload


def main() -> int:
    app = QApplication([])
    target = Path(__file__).resolve().parents[0] / 'accountvault.ico'
    images = [draw(s) for s in SIZES]
    target.write_bytes(build_ico(images))
    print(f'wrote {target}  ({target.stat().st_size:,} bytes, sizes {SIZES})')

    # Also drop the largest as a PNG for anything that wants one.
    images[-1].save(str(target.with_suffix('.png')), 'PNG')
    print(f'wrote {target.with_suffix(".png")}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
