from datetime import date

from autoclip.rights import RightsGate, Video
from autoclip.sources import parse_sources

TODAY = date(2026, 11, 12)


def video(channel_id="UCx", license="youtube", platform="youtube"):
    return Video(platform, "vid1", channel_id, "Chan", "Title", 3600, license, 5000)


def gate(**raw):
    return RightsGate(parse_sources(raw))


def test_denies_by_default():
    d = gate().check(video(), TODAY)
    assert not d.allowed and d.basis is None and "not in sources.yaml" in d.reason


def test_standard_licence_is_denied_even_with_cc_enabled():
    assert not gate(creative_commons={"enabled": True}).check(video(license="youtube"), TODAY).allowed


def test_cc_video_allowed_with_attribution():
    d = gate(creative_commons={"enabled": True}).check(video(license="creativeCommon"), TODAY)
    assert d.allowed and "Creative Commons" in d.basis
    assert "Title" in d.attribution and "Chan" in d.attribution and "watch?v=vid1" in d.attribution


def test_cc_video_denied_when_cc_disabled():
    assert not gate(creative_commons={"enabled": False}).check(video(license="creativeCommon"), TODAY).allowed


def test_cc_only_trusted_on_youtube():
    d = gate(creative_commons={"enabled": True}).check(video(license="creativeCommon", platform="twitch"), TODAY)
    assert not d.allowed


def test_blocked_channel_beats_everything():
    g = gate(creative_commons={"enabled": True, "blocked_channels": ["UCx"]},
             channels=[{"platform": "youtube", "channel_id": "UCx", "basis": "email"}])
    d = g.check(video(license="creativeCommon"), TODAY)
    assert not d.allowed and "blocked" in d.reason


def test_permissioned_channel_allowed_with_its_basis():
    d = gate(channels=[{"platform": "youtube", "channel_id": "UCx", "name": "X", "basis": "email 1 Nov"}]
             ).check(video(), TODAY)
    assert d.allowed and d.basis == "permission: email 1 Nov"


def test_channel_on_other_platform_does_not_match():
    g = gate(channels=[{"platform": "twitch", "channel_id": "UCx", "basis": "email"}])
    assert not g.check(video(), TODAY).allowed


def test_campaign_only_while_active():
    camp = {"platform": "youtube", "channel_id": "UCx", "name": "Camp", "basis": "terms",
            "starts": date(2026, 11, 1), "ends": date(2026, 11, 30)}
    g = gate(campaigns=[camp])
    assert g.check(video(), TODAY).allowed
    late = g.check(video(), date(2026, 12, 1))
    assert not late.allowed and "not active" in late.reason
