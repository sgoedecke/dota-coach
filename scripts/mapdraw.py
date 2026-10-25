"""Pure-python PNG heatmaps of hero positions (Radiant perspective), with a schematic map overlay."""
import math
import struct
import zlib

G = 64          # grid cells per side
S = 6           # pixels per cell
EXT = 8192      # world units from centre to edge
PANEL = G * S
GAP = 12


def _png(w, h, rows):
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _cell(x, y):
    cx = int((x + EXT) / (2 * EXT) * G)
    cy = int((EXT - y) / (2 * EXT) * G)  # y up -> row down
    return min(G - 1, max(0, cx)), min(G - 1, max(0, cy))


def _heat(v):
    # black -> purple -> orange -> yellow
    v = max(0.0, min(1.0, v))
    stops = [(0, (18, 18, 24)), (0.25, (90, 30, 120)), (0.6, (230, 100, 30)), (1.0, (255, 240, 120))]
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if v <= b:
            f = (v - a) / (b - a)
            return tuple(int(ca[k] + f * (cb[k] - ca[k])) for k in range(3))
    return stops[-1][1]


def _overlay(px, py):
    """Schematic map: river (x+y~0), mid lane (x~y), lane edges, bases."""
    x = px / PANEL * 2 * EXT - EXT
    y = EXT - py / PANEL * 2 * EXT
    if abs(x + y) < 90:
        return (60, 120, 200)
    if abs(x - y) < 60:
        return (110, 110, 110)
    if abs(y + 5900) < 60 or abs(x - 5900) < 60 or abs(x + 5900) < 60 or abs(y - 5600) < 60:
        return (110, 110, 110)
    if (abs(x + 3600) < 40 and y < -3300) or (abs(y + 3300) < 40 and x < -3600):
        return (60, 170, 60)
    if (abs(x - 3300) < 40 and y > 2900) or (abs(y - 2900) < 40 and x > 3300):
        return (170, 60, 60)
    return None


def render(path_png, panels, title_rows=None):
    """panels: list of rows; each row a list of (points[(x,y)], marks[(x,y)]) in Radiant perspective.
    Each panel is normalised independently (share of time), with a sqrt colour scale."""
    nrows, ncols = len(panels), max(len(r) for r in panels)
    W = ncols * PANEL + (ncols - 1) * GAP
    H = nrows * PANEL + (nrows - 1) * GAP
    img = [[40, 40, 40] * W for _ in range(H)]
    for ri, row in enumerate(panels):
        for ci, (pts, marks) in enumerate(row):
            grid = [[0.0] * G for _ in range(G)]
            for x, y in pts:
                cx, cy = _cell(x, y)
                grid[cy][cx] += 1
            tot = max(1, len(pts))
            mx = max(max(r) for r in grid) / tot or 1
            ox, oy = ci * (PANEL + GAP), ri * (PANEL + GAP)
            for py in range(PANEL):
                row_px = img[oy + py]
                for pxx in range(PANEL):
                    col = _overlay(pxx, py)
                    v = grid[py // S][pxx // S] / tot / mx
                    if v > 0:
                        c = _heat(math.sqrt(v))
                    else:
                        c = col or (18, 18, 24)
                    if col and v > 0:
                        c = tuple((a + b) // 2 for a, b in zip(c, col))
                    k = 3 * (ox + pxx)
                    row_px[k:k + 3] = c
            for x, y in marks:  # deaths: red X
                cx = int((x + EXT) / (2 * EXT) * PANEL) + ox
                cy = int((EXT - y) / (2 * EXT) * PANEL) + oy
                for d in range(-4, 5):
                    for qx, qy in ((cx + d, cy + d), (cx + d, cy - d)):
                        if ox <= qx < ox + PANEL and oy <= qy < oy + PANEL:
                            img[qy][3 * qx:3 * qx + 3] = [255, 30, 30]
    with open(path_png, "wb") as f:
        f.write(_png(W, H, img))
