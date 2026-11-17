"""Niche profiles (config/profiles/*.yaml): highlight focus, hook tone, signal weights, presets."""
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_DIR = Path(__file__).resolve().parents[2] / "config" / "profiles"


@dataclass
class Profile:
    name: str
    focus: str
    hook_tone: str
    weights: dict = field(default_factory=lambda: {"llm": 0.7, "loudness": 0.15, "chat": 0.15})
    caption_presets: list[str] = field(default_factory=lambda: ["clean_white"])
    max_seconds: int = 59
    description: str = ""


GENERIC = Profile("generic", "moments with a strong hook, a clear payoff and that stand on their own",
                  "clear and curious")


def load_profiles(folder: str | Path = DEFAULT_DIR) -> dict[str, Profile]:
    out = {}
    for f in sorted(Path(folder).glob("*.yaml")):
        d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        w = d.get("weights") or {}
        total = sum(float(v) for v in w.values()) or 1.0
        out[d["name"]] = Profile(
            name=d["name"], focus=str(d.get("focus", GENERIC.focus)).strip(),
            hook_tone=str(d.get("hook_tone", GENERIC.hook_tone)),
            weights={k: float(v) / total for k, v in w.items()} or dict(GENERIC.weights),
            caption_presets=list(d.get("caption_presets") or GENERIC.caption_presets),
            max_seconds=int(d.get("max_seconds", 59)), description=str(d.get("description", "")))
    return out


def get_profile(name: str | None, folder: str | Path = DEFAULT_DIR) -> Profile:
    return load_profiles(folder).get(name or "", GENERIC)
