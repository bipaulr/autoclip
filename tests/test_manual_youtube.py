"""The manual YouTube upload loop: video on Telegram with a button, owner pastes the Short's link back."""
from datetime import timedelta

from autoclip.integrations.telegram import MockNotifier
from autoclip.models import Campaign, Clip, DecisionLog, Submission
from autoclip.services.campaign_feed import poll_commands
from autoclip.services.publishing import PublishSettings, publish_due, record_manual_post
from tests.test_launch import MODE, NOW, Pub, make_live_post

LINK = "https://youtube.com/shorts/AbCdEfGhIj0"


def manual_post(session, tmp_path, campaign=True, vid="live_x_1_0"):
    post, _ = make_live_post(session, tmp_path, platform="youtube", vid=vid)
    if campaign:
        camp = Campaign(marketplace="cr", external_id=vid, name="Jason Pool", status="joined",
                        url="https://contentrewards.com/c/campaigns/j")
        session.add(camp)
        session.flush()
        session.get(Clip, post.clip_id).campaign_id = camp.id
    note = MockNotifier()
    publish_due(session, {"youtube": Pub()}, note, PublishSettings(launch=MODE), NOW)
    assert post.status == "manual"
    return post, note


def msg(update_id, text, reply_to=None, chat=42):
    m = {"message_id": 100 + update_id, "chat": {"id": chat}, "text": text}
    if reply_to:
        m["reply_to_message"] = {"text": reply_to}
    return {"update_id": update_id, "message": m}


def tap(update_id, data):
    return {"update_id": update_id, "callback_query": {"id": f"cb{update_id}", "data": data,
                                                       "message": {"message_id": 7, "chat": {"id": 42}}}}


def test_the_manual_upload_message_carries_a_link_button(session, tmp_path):
    post, note = manual_post(session, tmp_path)
    assert note.buttons[-1] == [[{"text": "I posted it: add link", "callback_data": f"ytl:{post.id}"}]]
    caption = note.videos[0][1]
    assert f"post #{post.id}" in caption and "submitted to the campaign for you" in caption


def test_button_then_pasted_link_records_the_post_and_queues_auto_submit(session, tmp_path):
    post, _ = manual_post(session, tmp_path)
    bot = MockNotifier()
    bot.inbox = [tap(1, f"ytl:{post.id}")]
    poll_commands(session, bot, "42", ("*",))
    assert bot.prompts and f"post #{post.id}" in bot.prompts[0]
    bot.inbox.append(msg(2, f"here {LINK}?feature=share", reply_to=bot.prompts[0]))
    assert poll_commands(session, bot, "42", ("*",)) == 1
    assert post.status == "posted" and post.url == LINK and post.external_id == "AbCdEfGhIj0"
    assert session.get(Clip, post.clip_id).status == "posted"
    sub = session.query(Submission).filter_by(post_id=post.id).one()
    assert sub.status == "pending" and any("auto-submitted" in s for s in bot.sent)
    assert any(d.action == "posted" and "by hand" in d.reason for d in session.query(DecisionLog))


def test_a_link_pasted_without_the_button_goes_to_the_oldest_waiting_post(session, tmp_path):
    first, _ = manual_post(session, tmp_path, campaign=False, vid="live_a_1_0")
    bot = MockNotifier()
    bot.inbox = [msg(1, LINK)]
    poll_commands(session, bot, "42")
    assert first.status == "posted" and bot.sent[-1] == f"Recorded post #{first.id}: {LINK}"


def test_bad_links_strangers_and_repeats_are_refused(session, tmp_path):
    post, _ = manual_post(session, tmp_path, campaign=False)
    assert "doesn't look like" in record_manual_post(session, post.id, "https://instagram.com/x", MockNotifier())
    bot = MockNotifier()
    bot.inbox = [msg(1, LINK, chat=999)]
    poll_commands(session, bot, "42")
    assert post.status == "manual"  # only the owner's chat counts
    record_manual_post(session, post.id, LINK, MockNotifier(), now=NOW + timedelta(minutes=5))
    assert "already recorded" in record_manual_post(session, post.id, LINK, MockNotifier())
    assert session.query(Submission).count() == 0  # no campaign, nothing to submit
