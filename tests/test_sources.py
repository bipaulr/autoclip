from datetime import date

import pytest

from autoclip.sources import load_sources, parse_sources


def test_missing_file_is_an_empty_allowlist(tmp_path):
    s = load_sources(tmp_path / "nope.yaml")
    assert not s.creative_commons.enabled and s.channels == [] and s.campaigns == []


def test_loads_all_three_bases(tmp_path):
    f = tmp_path / "sources.yaml"
    f.write_text("""
min_duration_minutes: 30
creative_commons:
  enabled: true
  queries: [lecture, talk]
  max_searches_per_run: 1
  min_views: 500
  blocked_channels: [UCbad]
channels:
  - {platform: YouTube, channel_id: UCa, name: A, basis: "email 2026-11-01"}
campaigns:
  - {platform: youtube, channel_id: UCb, name: B, basis: "terms", starts: 2026-11-01, ends: 2026-11-30}
""", encoding="utf-8")
    s = load_sources(f)
    assert s.min_duration_minutes == 30
    cc = s.creative_commons
    assert (cc.enabled, cc.queries, cc.max_searches_per_run, cc.min_views, cc.blocked_channels) == (
        True, ["lecture", "talk"], 1, 500, ["UCbad"])
    assert (s.channels[0].platform, s.channels[0].basis) == ("youtube", "email 2026-11-01")
    c = s.campaigns[0]
    assert c.active_on(date(2026, 11, 1)) and c.active_on(date(2026, 11, 30))
    assert not c.active_on(date(2026, 10, 31)) and not c.active_on(date(2026, 12, 1))


def test_channel_without_basis_is_refused():
    with pytest.raises(ValueError, match="basis"):
        parse_sources({"channels": [{"platform": "youtube", "channel_id": "UCa"}]})


def test_campaign_without_dates_is_refused():
    with pytest.raises(ValueError, match="starts"):
        parse_sources({"campaigns": [{"platform": "youtube", "channel_id": "UCa", "basis": "x"}]})


def test_repo_sources_file_is_valid():
    s = load_sources("sources.yaml")
    # Off by default: the plan's rights basis is campaigns and written permission only.
    assert not s.creative_commons.enabled and s.creative_commons.queries
    assert s.creative_commons.languages == ["en"]


def test_languages_are_lowercased_and_default_to_any():
    assert parse_sources({"creative_commons": {"languages": ["EN", "Hi"]}}).creative_commons.languages == ["en", "hi"]
    assert parse_sources({}).creative_commons.languages == []
