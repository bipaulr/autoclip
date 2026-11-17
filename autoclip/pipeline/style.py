"""Captions and value-add layer (plan prompt 8).

Word-by-word ASS captions (max 3 words per line, current word highlighted), a hook headline for the
first 2.5 seconds, an optional context card, and six named style presets. Presets live here as code;
the bandit stores which one it picked per post in the bandit_arm table.
"""
import re
from dataclasses import dataclass

from autoclip.integrations.transcriber import Word

W, H = 1080, 1920


@dataclass(frozen=True)
class Preset:
    name: str
    font: str
    size: int
    primary: str  # ASS colours are &HAABBGGRR
    highlight: str
    outline: str
    outline_w: int
    box: bool  # opaque box behind captions
    margin_v: int  # distance from the bottom; keeps captions above Reels/Shorts buttons
    uppercase: bool
    zoom: float  # punch-in intensity (1.0 = none)
    # "story" editing (modelled on top-performing campaign clips): one huge word at a time in the middle
    # of the screen, coloured key words and amounts, *REACTION* tags, colour flashes, a cold open, and a
    # full-screen face crop whenever there is a face to follow.
    story: bool = False


# ASS colours for story captions (&HAABBGGRR)
YELLOW, GREEN, RED, PINK, WHITE = "&H0000FFFF", "&H0033FF33", "&H003333FF", "&H00C85CFF", "&H00FFFFFF"

PRESETS = {p.name: p for p in [
    Preset("story_pop", "Anton", 150, YELLOW, YELLOW, "&H00000000", 9, False, 0, True, 1.15, story=True),
    Preset("bold_pop", "Archivo Black", 96, "&H00FFFFFF", "&H0000E6FF", "&H00000000", 6, False, 520, True, 1.12),
    Preset("neon_box", "Liberation Sans", 88, "&H00FFFFFF", "&H00FF55FF", "&H80000000", 0, True, 560, False, 1.08),
    Preset("impact_yellow", "Anton", 108, "&H0000FFFF", "&H00FFFFFF", "&H00000000", 7, False, 500, True, 1.12),
    Preset("clean_white", "Liberation Sans", 84, "&H00FFFFFF", "&H0066FF33", "&H00000000", 4, False, 600, False, 1.06),
    Preset("minimal_lower", "DejaVu Sans", 72, "&H00F0F0F0", "&H0000C8FF", "&H00000000", 3, False, 430, False, 1.04),
    Preset("karaoke_blue", "Archivo Black", 92, "&H00FFFFFF", "&H00FFB000", "&H00000000", 5, False, 540, True, 1.10),
]}


def ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def group_words(words: list[Word], max_words: int = 3, max_gap: float = 0.6) -> list[list[Word]]:
    """Lines of up to three words; a pause or sentence end starts a new line."""
    lines, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > max_gap or cur[-1].w[-1:] in ".?!"):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def hook_size(text: str) -> int:
    """Shrinks long headlines so they fit on two lines."""
    n = len(text or "")
    return 84 if n <= 24 else max(60, int(84 * 24 / n) + 8)


MULTIPLIERS = {"thousand": 1_000, "k": 1_000, "grand": 1_000, "million": 1_000_000, "mil": 1_000_000,
               "billion": 1_000_000_000}
CURRENCY_WORDS = {"dollar": "$", "dollars": "$", "bucks": "$", "yen": "¥", "euro": "€", "euros": "€",
                  "pound": "£", "pounds": "£"}
NUMBER = re.compile(r"^([$¥€£]?)(-?\d[\d,]*(?:\.\d+)?)([kKmM]?)$")
LOSS_WORDS = {"lost", "lose", "losing", "minus", "down", "broke", "debt", "owe", "owed", "bill", "fine", "fined"}


def _clean(w: str) -> str:
    return re.sub(r"[^\w$¥€£.,%\-]", "", w).strip(".,").lower()


def merge_amounts(words: list[Word]) -> list[Word]:
    """'1.6 million yen' -> one word '¥1,600,000' spanning the three; '$12,000' stays. Plain words pass."""
    out, i = [], 0
    while i < len(words):
        m = NUMBER.match(_clean(words[i].w))
        if not m:
            out.append(words[i])
            i += 1
            continue
        sym, num, suffix = m.group(1), m.group(2).replace(",", ""), m.group(3).lower()
        value, j = float(num), i + 1
        mult = MULTIPLIERS.get({"m": "million"}.get(suffix, suffix), 1)
        if j < len(words) and _clean(words[j].w) in MULTIPLIERS:
            mult = MULTIPLIERS[_clean(words[j].w)]
            j += 1
        if j < len(words) and _clean(words[j].w) in CURRENCY_WORDS:
            sym = sym or CURRENCY_WORDS[_clean(words[j].w)]
            j += 1
        if j == i + 1 and not sym and mult == 1:
            out.append(words[i])  # a plain number like "3" stays as spoken
            i += 1
            continue
        total = value * mult
        text = f"{'-' if total < 0 else ''}{sym}{abs(total):,.0f}" if total == int(total) else f"{sym}{total:,}"
        out.append(Word(text, words[i].start, words[j - 1].end))
        i = j
    return out


def accent(word: str, emphasis: set[str], prev: str = "") -> str | None:
    """Colour for a story caption word: amounts green (red after a loss word or when negative),
    the clip's key words pink, else None (the preset's yellow)."""
    w = _clean(word)
    if re.search(r"[$¥€£]|\d", w):
        return RED if w.startswith("-") or _clean(prev) in LOSS_WORDS else GREEN
    return PINK if w in emphasis else None


def build_ass(words: list[Word], preset: Preset, *, clip_start: float, duration: float,
              hook_headline: str = "", context_line: str = "", hook_seconds: float = 2.5,
              max_words: int = 3, emphasis: list[str] | None = None, reaction_tag: str = "",
              tag_at: float | None = None) -> str:
    """words carry source-video times; clip_start shifts them so the clip starts at 0."""
    p = preset
    if p.story:
        return _story_ass(words, p, clip_start=clip_start, duration=duration, hook_headline=hook_headline,
                          hook_seconds=hook_seconds, emphasis=emphasis or [], reaction_tag=reaction_tag,
                          tag_at=tag_at)
    border = 3 if p.box else 1
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{p.font},{p.size},{p.primary},{p.highlight},{p.outline},&H80000000,1,0,0,0,100,100,0,0,"
        f"{border},{p.outline_w if not p.box else 18},2,2,80,80,{p.margin_v},1\n"
        f"Style: Hook,{p.font},{hook_size(hook_headline)},&H00FFFFFF,&H00FFFFFF,&H00000000,&HC0000000,1,0,0,0,100,100,0,0,3,22,0,8,"
        "70,70,230,1\n"
        "Style: Ctx,Liberation Sans,52,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4000000,0,0,0,0,100,100,0,0,3,16,0,8,"
        "90,90,420,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    events = []
    if hook_headline:
        events.append(f"Dialogue: 2,{ass_time(0)},{ass_time(min(hook_seconds, duration))},Hook,,0,0,0,,"
                      f"{{\\fad(120,200)}}{_esc(hook_headline.upper())}")
    if context_line:
        start = min(hook_seconds + 0.3, max(0.0, duration - 4))
        events.append(f"Dialogue: 1,{ass_time(start)},{ass_time(min(start + 4.5, duration))},Ctx,,0,0,0,,"
                      f"{{\\fad(150,150)}}{_esc(context_line)}")
    inside = [w for w in words if w.end > clip_start and w.start < clip_start + duration]
    for line in group_words(inside, max_words=max_words):
        for k, w in enumerate(line):
            s = max(0.0, w.start - clip_start)
            e = (line[k + 1].start if k + 1 < len(line) else w.end) - clip_start
            e = min(max(e, s + 0.05), duration)
            parts = []
            for j, other in enumerate(line):
                text = _esc(other.w.upper() if p.uppercase else other.w)
                parts.append(f"{{\\c{p.highlight}}}{text}{{\\c{p.primary}}}" if j == k else text)
            events.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Cap,,0,0,0,,{' '.join(parts)}")
    return header + "\n".join(events) + "\n"


def _story_ass(words: list[Word], p: Preset, *, clip_start: float, duration: float, hook_headline: str,
               hook_seconds: float, emphasis: list[str], reaction_tag: str, tag_at: float | None) -> str:
    """One word at a time, huge, in the middle of the screen, popping in; amounts and key words coloured."""
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{p.font},{p.size},{p.primary},{p.primary},{p.outline},&H96000000,0,0,0,0,100,100,1,0,"
        f"1,{p.outline_w},3,5,40,40,0,1\n"
        f"Style: Tag,{p.font},{int(p.size * 0.8)},{WHITE},{WHITE},{p.outline},&H96000000,0,0,0,0,100,100,1,0,"
        f"1,{p.outline_w},3,5,40,40,0,1\n"
        f"Style: Hook,{p.font},{hook_size(hook_headline)},&H00FFFFFF,&H00FFFFFF,&H00000000,&HC0000000,0,0,0,0,"
        "100,100,0,0,3,22,0,8,70,70,230,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    events = []
    if hook_headline:
        events.append(f"Dialogue: 2,{ass_time(0)},{ass_time(min(hook_seconds, duration))},Hook,,0,0,0,,"
                      f"{{\\fad(120,200)}}{_esc(hook_headline.upper())}")
    keys = {_clean(x) for phrase in emphasis for x in phrase.split()}
    inside = merge_amounts([w for w in words if w.end > clip_start and w.start < clip_start + duration])
    tag_span = (tag_at, tag_at + 1.0) if reaction_tag and tag_at is not None else None
    pop = "{\\fscx70\\fscy70\\t(0,90,\\fscx108\\fscy108)\\t(90,160,\\fscx100\\fscy100)}"
    for k, w in enumerate(inside):
        s = max(0.0, w.start - clip_start)
        nxt = inside[k + 1].start - clip_start if k + 1 < len(inside) else w.end - clip_start + 0.3
        e = min(max(min(nxt, s + 1.2), s + 0.12), duration)
        if tag_span and s < tag_span[1] and e > tag_span[0]:
            continue  # the reaction tag owns the middle of the screen for that second
        colour = accent(w.w, keys, inside[k - 1].w if k else "")
        text = _esc(w.w.upper())
        events.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Cap,,0,0,0,,{pop}"
                      + (f"{{\\c{colour}}}" if colour else "") + text)
    if tag_span:
        tag = "*" + re.sub(r"[^A-Za-z ]", "", reaction_tag).strip().upper()[:16] + "*"
        events.append(f"Dialogue: 1,{ass_time(tag_span[0])},{ass_time(min(tag_span[1], duration))},Tag,,0,0,0,,"
                      f"{pop}{tag}")
    return header + "\n".join(events) + "\n"


def flash_filter(peaks: list[float], seconds: float = 0.25) -> str:
    """A quick full-screen colour tint on the biggest moments (green, then red, alternating)."""
    out = []
    for k, t in enumerate(peaks[:3]):
        colour = "0x33FF33@0.35" if k % 2 == 0 else "0xFF2020@0.40"
        out.append(f"drawbox=x=0:y=0:w=iw:h=ih:color={colour}:t=fill:enable='between(t,{t:.2f},{t + seconds:.2f})'")
    return ",".join(out)


def zoom_expression(peaks: list[float], intensity: float, rise: float = 0.15, hold: float = 0.6,
                    fall: float = 0.4) -> str:
    """ffmpeg expression for a scale factor over time t: a quick punch-in at each peak, then ease out."""
    if not peaks or intensity <= 1.0:
        return "1"
    amp = intensity - 1.0
    terms = []
    for p in peaks:
        a, b, c = p, p + rise, p + rise + hold
        terms.append(f"if(between(t,{a:.2f},{b:.2f}),(t-{a:.2f})/{rise},if(between(t,{b:.2f},{c:.2f}),1,"
                     f"if(between(t,{c:.2f},{c + fall:.2f}),1-(t-{c:.2f})/{fall},0)))")
    return f"1+{amp:.3f}*min(1,{'+'.join(terms)})"
