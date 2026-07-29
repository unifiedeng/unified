r"""Pure-geometry layout primitives for the NX drawing generator — no NXOpen
dependency, so they can be unit-tested off-line (see test_placer.py).

Rect + segment/rect intersection + the Placer annotation packer that realises
the rule: annotation hitboxes never overlap, and no leader/feature line runs
across another annotation's hitbox.
"""


class Rect(object):
    def __init__(self, x0, y0, x1, y1):
        self.x0, self.y0, self.x1, self.y1 = min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)

    def overlaps(self, o, pad=0.0):
        return not (self.x1 + pad < o.x0 or o.x1 + pad < self.x0 or
                    self.y1 + pad < o.y0 or o.y1 + pad < self.y0)

    def union(self, o):
        return Rect(min(self.x0, o.x0), min(self.y0, o.y0),
                    max(self.x1, o.x1), max(self.y1, o.y1))

    def __repr__(self):
        return "Rect(%.1f,%.1f - %.1f,%.1f)" % (self.x0, self.y0, self.x1, self.y1)


def seg_hits_rect(p0, p1, r, pad=0.0):
    """True if segment p0->p1 intersects rect r (grown by pad). Liang-Barsky
    clip: used to keep leader/extension lines off annotation hitboxes."""
    x0, y0, x1, y1 = r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    if x0 <= p0[0] <= x1 and y0 <= p0[1] <= y1:
        return True
    t0, t1 = 0.0, 1.0
    for pp, qq in ((-dx, p0[0] - x0), (dx, x1 - p0[0]),
                   (-dy, p0[1] - y0), (dy, y1 - p0[1])):
        if abs(pp) < 1e-12:
            if qq < 0:
                return False
        else:
            t = qq / pp
            if pp < 0:
                if t > t1:
                    return False
                t0 = max(t0, t)
            else:
                if t < t0:
                    return False
                t1 = min(t1, t)
    return t0 <= t1


# Estimated annotation text metrics (sheet mm). NX exposes no rendered bounding
# box headless, so hitboxes are sized from text content. Deliberately generous
# so minor anchor/width error still blocks overlaps; tuned to the A3/B templates.
ANNO_CW = 2.6          # per-character width
ANNO_LH = 6.5          # line pitch (height per text line)


def _text_w(sline):
    """Rendered width in chars: the <O> diameter control code draws one glyph."""
    return len(sline.replace("<O>", "D")) * ANNO_CW


class Placer(object):
    """Deterministic annotation packer. Holds a hitbox per placed annotation,
    per drawing obstacle, and per view; chooses, for each new annotation, the
    first candidate origin whose text box overlaps nothing AND whose leader
    crosses no existing hitbox. Realises the rule: annotations never overlap,
    and feature/leader lines never run across another annotation's box."""

    def __init__(self, field, P):
        self.field = field
        self.P = P
        self.clear = 2.5 * P  # min gap a leader keeps from any annotation/keep-out
        self.text = []        # placed annotation boxes
        self.obstacles = []   # drawing keep-outs (title block, symbols, notes)
        self.views = []       # view rectangles (text avoids; leaders may enter)
        self.leaders = []     # placed leader segments (p0, p1)

    def seed_obstacle(self, r):
        self.obstacles.append(r)

    def seed_view(self, r):
        self.views.append(r)

    def box(self, origin, lines, grow=1.6, anchor="c"):
        """Hitbox sized to the (multi-line) content. NX anchors annotation text
        at its TOP-LEFT (text grows right and down from the origin) — model that
        with anchor="tl"; anchor="c" centres the box (short symmetric tokens)."""
        w = max((_text_w(s) for s in lines), default=ANNO_CW)
        h = len(lines) * ANNO_LH
        ox, oy = origin
        g = grow * self.P
        if anchor == "tl":
            return Rect(ox - g, oy - h - g, ox + w + g, oy + g)
        return Rect(ox - w / 2 - g, oy - h / 2 - g, ox + w / 2 + g, oy + h / 2 + g)

    def text_free(self, r):
        f, p = self.field, self.P
        if r.x0 < f.x0 or r.y0 < f.y0 or r.x1 > f.x1 or r.y1 > f.y1:
            return False
        if any(r.overlaps(o, pad=0.8 * p) for o in self.text):
            return False
        if any(r.overlaps(o, pad=0.8 * p) for o in self.obstacles):
            return False
        if any(r.overlaps(v, pad=0.8 * p) for v in self.views):
            return False
        if any(seg_hits_rect(s0, s1, r, pad=self.clear) for s0, s1 in self.leaders):
            return False
        return True

    def leader_free(self, p0, p1):
        # a leader may enter its own view but must clear annotation boxes and
        # drawing keep-outs (title block, projection symbol, notes), with a
        # comfortable margin so a leader never grazes a value
        return not any(seg_hits_rect(p0, p1, r, pad=self.clear)
                       for r in self.text + self.obstacles)

    def register(self, r, leader=None):
        self.text.append(r)
        if leader is not None:
            self.leaders.append(leader)

    def place(self, lines, candidates, attach=None, grow=1.6, anchor="c"):
        """Try each candidate origin in order; return (origin, box, ok) for the
        first that is collision-free, registering it. Falls back to the first
        candidate (best effort) if none are clear."""
        cands = list(candidates)
        for origin in cands:
            r = self.box(origin, lines, grow, anchor)
            if self.text_free(r) and (attach is None or self.leader_free(attach, origin)):
                self.register(r, (attach, origin) if attach else None)
                return origin, r, True
        origin = cands[0]
        r = self.box(origin, lines, grow, anchor)
        self.register(r, (attach, origin) if attach else None)
        return origin, r, False
