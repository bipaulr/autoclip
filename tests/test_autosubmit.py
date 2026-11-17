import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from autoclip.integrations.cr_submit import BrowserSubmitter, MockSubmitter, SubmitResult, run_submit
from autoclip.integrations.telegram import MockNotifier
from autoclip.models import Account, Campaign, Clip, DecisionLog, Highlight, Post, SourceVideo, Submission
from autoclip.services import autosubmit

URL = "https://contentrewards.com/c/campaigns/0b178e3c-fbd3-4294-8ded-394e3da9c62f"
AWAY = (120, 600)  # 02:00-10:00 IST
NIGHT = datetime(2026, 11, 17, 1, 0, tzinfo=timezone.utc)  # 06:30 IST: owner asleep
EVENING = datetime(2026, 11, 17, 13, 0, tzinfo=timezone.utc)  # 18:30 IST: owner awake


class Loc:
    def __init__(self, page, key, present=True):
        self.page, self.key, self.present = page, key, present

    def count(self):
        if self.key == ("dialog",):
            return 1 if self.page.dialog_open else 0
        return 1 if self.present else 0

    @property
    def first(self):
        return self

    def click(self):
        self.page.actions.append(("click", self.key))
        if self.key == ("page", "button", "Submit clip"):
            self.page.dialog_open = True
        if self.key == ("dialog", "button", "Submit clip") and not self.page.refuse:
            self.page.dialog_open = False

    def fill(self, text):
        self.page.actions.append(("fill", text))

    def check(self, **kw):
        self.page.actions.append(("check",))

    def hold(self, ms):
        self.page.actions.append(("hold", self.key, ms))
        self.page.notice = None  # accepted: the notice closes

    def inner_text(self):
        return f"Before you submit\n{self.page.notice}\nHold to confirm" if self.page.notice else "Submit clip"

    def get_by_role(self, role, name=None):
        return Loc(self.page, ("dialog", role, name))


class FakePage:
    def __init__(self, url="https://contentrewards.com/c/campaigns/x", body="Submitted", refuse=False,
                 notice=None):
        self.url, self.body, self.refuse, self.notice = url, body, refuse, notice
        self.actions, self.dialog_open = [], False

    def goto(self, url, wait_until=None):
        self.actions.append(("goto", url))

    def get_by_role(self, role, name=None):
        if role == "dialog":
            return Loc(self, ("dialog",))
        if name == "Hold to confirm":
            return Loc(self, ("page", role, name), present=bool(self.notice))
        return Loc(self, ("page", role, name), present=name != "Not now")

    def wait_for_timeout(self, ms):
        pass

    def inner_text(self, sel):
        return self.body


def test_click_path_fills_ticks_and_submits():
    page = FakePage()
    r = run_submit(page, URL, "https://www.instagram.com/reel/abc/", pause=lambda: None)
    assert r.ok and r.reason == "submitted"
    assert ("fill", "https://www.instagram.com/reel/abc/") in page.actions and ("check",) in page.actions
    assert page.actions[-1] == ("click", ("dialog", "button", "Submit clip"))


def test_dry_run_never_clicks_the_final_button():
    page = FakePage()
    r = run_submit(page, URL, "https://www.instagram.com/reel/abc/", pause=lambda: None, dry_run=True)
    assert r.ok and "dry run" in r.reason
    assert ("click", ("dialog", "button", "Submit clip")) not in page.actions


def test_logged_out_and_unknown_page_are_reported():
    assert run_submit(FakePage(url="https://contentrewards.com/login"), URL, "u", pause=lambda: None).logged_out
    assert not run_submit(FakePage(), "https://contentrewards.com", "u", pause=lambda: None).ok
    assert BrowserSubmitter("/nonexistent/state.json").submit(URL, "u").logged_out


def test_refusal_is_not_counted_as_success():
    r = run_submit(FakePage(body="This post can't be submitted", refuse=True), URL, "u", pause=lambda: None)
    assert not r.ok and "refused" in r.reason


def test_the_standing_count_heading_is_not_a_refusal_and_already_submitted_is_success():
    # the dialog always lists older posts under "11 posts can't be submitted"; after sending, the form
    # re-checks the link and says it is already submitted: that is what success looks like
    page = FakePage(body="11 posts can't be submitted\nThis clip is already submitted to this campaign.",
                    refuse=True)
    r = run_submit(page, URL, "https://www.instagram.com/reel/abc/", pause=lambda: None)
    assert r.ok and "already submitted" in r.reason
    heading_only = FakePage(body="11 posts can't be submitted\nSubmit clip", refuse=True)
    r = run_submit(heading_only, URL, "u", pause=lambda: None)
    assert not r.ok and "refused" not in r.reason  # no real error text: reported as unconfirmed


class GreyButton(Loc):
    def get_attribute(self, name):
        return "true" if name == "aria-disabled" else None


def test_a_greyed_out_submit_button_reports_the_forms_reason_without_clicking():
    page = FakePage(body="11 posts can't be submitted\nThis clip is not eligible: posted more than 30 minutes ago")
    page.get_by_role = (lambda orig: lambda role, name=None: (
        Loc(page, ("dialog",)) if role == "dialog" else orig(role, name)))(page.get_by_role)
    Loc.get_by_role = lambda self, role, name=None: (GreyButton if (role, name) == ("button", "Submit clip")
                                                     else Loc)(self.page, ("dialog", role, name))
    try:
        r = run_submit(page, URL, "u", pause=lambda: None)
    finally:
        Loc.get_by_role = lambda self, role, name=None: Loc(self.page, ("dialog", role, name))
    assert not r.ok and "not eligible" in r.reason
    assert ("click", ("dialog", "button", "Submit clip")) not in page.actions


def _pending(session, posted_at, name="$9,000 Jason Pool Campaign (Reach)"):
    camp = Campaign(marketplace="m", external_id=name, name=name, url=URL, status="joined")
    acc = Account(platform="instagram", handle=f"@{name[:8]}", niche="x", style_preset="s", active=True)
    v = SourceVideo(platform="twitch", video_id=f"v{name}{posted_at}", channel_id="c", channel_title="c",
                    title="t", duration_s=600, license="", views=1, status="done", rights_basis="x")
    session.add_all([camp, acc, v])
    session.flush()
    h = Highlight(source_video_id=v.id, start_s=0, end_s=30, title="t", reason="r", score=60, llm="m",
                  status="rendered")
    session.add(h)
    session.flush()
    c = Clip(highlight_id=h.id, path="p", layout="g", style_preset="s", duration_s=30, status="posted",
             campaign_id=camp.id)
    session.add(c)
    session.flush()
    p = Post(clip_id=c.id, account_id=acc.id, platform="instagram", scheduled_at=posted_at, status="posted",
             url=f"https://www.instagram.com/reel/{c.id}/")
    session.add(p)
    session.flush()
    s = Submission(post_id=p.id, campaign_id=camp.id, deadline=posted_at + timedelta(minutes=30),
                   status="pending", alerted_at=posted_at, updated_at=posted_at)
    session.add(s)
    session.flush()
    return s


def test_overnight_post_is_submitted_once_after_a_short_wait(session):
    sub = _pending(session, NIGHT)
    sb, note, rng = MockSubmitter(), MockNotifier(), random.Random(0)
    assert autosubmit.run(session, sb, note, NIGHT + timedelta(minutes=1), keywords=("jason",), away=AWAY,
                          rng=rng) == []
    out = autosubmit.run(session, sb, note, NIGHT + timedelta(minutes=8), keywords=("jason",), away=AWAY, rng=rng)
    assert out and len(sb.calls) == 1 and sb.calls[0][0] == URL
    assert session.get(Submission, sub.id).status == "submitted" and "Auto-submitted" in note.sent[-1]
    autosubmit.run(session, sb, note, NIGHT + timedelta(minutes=9), keywords=("jason",), away=AWAY, rng=rng)
    assert len(sb.calls) == 1


def test_awake_hours_other_campaigns_and_expired_windows_are_left_alone(session):
    _pending(session, EVENING)
    _pending(session, NIGHT, name="Stream House LA Clipping")
    late = _pending(session, NIGHT - timedelta(hours=1), name="Jason 2")
    sb = MockSubmitter()
    autosubmit.run(session, sb, MockNotifier(), EVENING + timedelta(minutes=8), keywords=("jason",), away=AWAY)
    autosubmit.run(session, sb, MockNotifier(), NIGHT + timedelta(minutes=8), keywords=("jason",), away=AWAY)
    assert sb.calls == [] and session.get(Submission, late.id).status == "pending"
    assert autosubmit.run(session, sb, MockNotifier(), NIGHT, keywords=(), away=AWAY) == []


def test_failure_alerts_and_asks_for_a_new_login(session):
    _pending(session, NIGHT)
    note = MockNotifier()
    autosubmit.run(session, MockSubmitter(SubmitResult(False, "the saved login has expired", logged_out=True)),
                   note, NIGHT + timedelta(minutes=8), keywords=("jason",), away=AWAY, rng=random.Random(0))
    assert any("FAILED" in m for m in note.sent) and any("cr_login.py" in m for m in note.sent)
    assert session.scalars(select(DecisionLog).where(DecisionLog.module == "autosubmit")).first().action == "failed"


def test_auto_campaigns_may_post_in_us_prime_time_others_may_not(session):
    from autoclip.pipeline.decide import IST_OFFSET, Bandit, make_windows, next_slot, parse_away
    a = Account(platform="instagram", handle="@w", niche="x", style_preset="s", daily_cap=8, active=True)
    session.add(a)
    session.flush()
    w = make_windows("01:00-17:00", "", "", "05:30-09:30", "jason")
    now = datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc)  # 04:30 IST
    auto_hours, other_hours = set(), set()
    for seed in range(30):
        rng = random.Random(seed)
        t, _ = next_slot(session, a, Bandit(session, rng), now, rng, campaign=True, away=parse_away("02:00-10:00"),
                         quiet=w, campaign_name="$9,000 Jason Pool Campaign")
        auto_hours.add((t + IST_OFFSET).hour)
        t, _ = next_slot(session, a, Bandit(session, rng), now, rng, campaign=True, away=parse_away("02:00-10:00"),
                         quiet=w, campaign_name="Stream House LA")
        other_hours.add((t + IST_OFFSET).hour)
    assert auto_hours & {5, 6, 7, 8, 9}
    assert not other_hours & set(range(2, 17))


def test_always_mode_also_submits_while_the_owner_is_awake(session):
    _pending(session, EVENING)
    sb = MockSubmitter()
    autosubmit.run(session, sb, MockNotifier(), EVENING + timedelta(minutes=8), keywords=("jason",), away=AWAY,
                   rng=random.Random(0), always=True)
    assert len(sb.calls) == 1


def test_auto_campaign_post_alert_says_no_tap_needed(session):
    from autoclip.services.submissions import queue_submission
    sub = _pending(session, NIGHT)
    post_id, camp_id = sub.post_id, sub.campaign_id
    session.delete(sub)
    session.flush()
    note = MockNotifier()
    s2 = queue_submission(session, session.get(Post, post_id), session.get(Campaign, camp_id), note,
                          NIGHT, auto=True)
    assert "auto-submitted" in note.sent[-1] and not note.buttons[-1] and s2.status == "pending"


def test_star_means_every_campaign(session):
    _pending(session, NIGHT, name="Stream House LA Clipping")
    sb = MockSubmitter()
    autosubmit.run(session, sb, MockNotifier(), NIGHT + timedelta(minutes=8), keywords=("*",), away=AWAY,
                   rng=random.Random(0))
    assert len(sb.calls) == 1


def test_before_you_submit_notice_is_held_to_confirm_and_its_text_returned():
    page = FakePage(notice="Your captions must use the hashtag: #CORE and hashtags for each CORE member in the video.")
    r = run_submit(page, URL, "https://www.youtube.com/shorts/abc", pause=lambda: None)
    assert r.ok and "#CORE" in r.notice and "Hold to confirm" not in r.notice
    held = [a for a in page.actions if a[0] == "hold"]
    assert held and held[0][2] >= 2000  # a real press-and-hold, not a click
    assert page.actions.index(held[0]) < page.actions.index(("fill", "https://www.youtube.com/shorts/abc"))


def test_no_notice_means_no_hold():
    page = FakePage()
    r = run_submit(page, URL, "u", pause=lambda: None)
    assert r.ok and r.notice is None and not [a for a in page.actions if a[0] == "hold"]


def test_notice_seen_while_submitting_is_saved_to_the_campaign_once(session):
    from autoclip.services.autosubmit import remember_notice
    camp = Campaign(marketplace="cr", external_id="core", name="CORE Pool", url=URL, status="joined",
                    rules_text="Clip the CORE channel.", brief={"platforms": ["youtube"]})
    session.add(camp)
    session.flush()
    note = MockNotifier()
    text = "Your captions must use the hashtag: #CORE and hashtags for each CORE member in the video."
    assert remember_notice(session, camp, text, note)
    assert "[Before you submit] Your captions must use the hashtag: #CORE" in camp.rules_text
    assert camp.brief is None  # re-parsed on the next brief run
    assert note.sent and "#CORE" in note.sent[0]
    camp.brief = {"platforms": ["youtube"]}
    assert not remember_notice(session, camp, text, note)  # same notice again: nothing changes
    assert camp.brief == {"platforms": ["youtube"]}


def test_any_already_submitted_wording_counts_as_submitted():
    for body in ("This clip was already submitted.", "Already submitted", "11 posts can't be submitted\n"
                 "This clip is already submitted to this campaign."):
        r = run_submit(FakePage(body=body, refuse=True), URL, "u", pause=lambda: None)
        assert r.ok and "already submitted" in r.reason, body


def test_a_saved_notice_survives_imports_and_the_brief_is_parsed_again(session, tmp_path):
    import json

    from autoclip.integrations.llm import MockLLM
    from autoclip.services.campaigns import import_campaigns
    from autoclip.tasks import parse_new_briefs
    yml = tmp_path / "c.yaml"
    yml.write_text("campaigns:\n  - marketplace: cr\n    id: core\n    name: CORE Pool\n"
                   "    rules_text: Clips of at least 2 CORE members.\n    joined: true\n")
    import_campaigns(yml, session)
    c = session.scalars(select(Campaign)).one()
    c.brief = {"required_hashtags": []}
    session.flush()
    assert autosubmit.remember_notice(session, c, "Your captions must use #CORE and a hashtag per member.")
    session.flush()
    session.expire_all()  # read back what the database holds: a cleared brief must be SQL NULL
    assert session.scalars(select(Campaign).where(Campaign.brief.is_(None))).one() is c
    import_campaigns(yml, session)  # the yaml has no notice: it must not erase it
    assert "[Before you submit] Your captions must use #CORE" in c.rules_text
    assert c.rules_text.startswith("Clips of at least 2 CORE members.")
    llm = MockLLM([json.dumps({"required_hashtags": ["CORE"], "caption_rules": "a hashtag per member"})])
    assert parse_new_briefs(session, llm) == 1 and c.brief["required_hashtags"] == ["#CORE"]
    import_campaigns(yml, session)  # unchanged rules: the parsed brief is kept
    assert c.brief is not None
