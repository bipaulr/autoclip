from datetime import datetime, timedelta, timezone

from autoclip.models import DecisionLog, Highlight, SourceVideo
from autoclip.services.pipeline import requeue_moderation_holds

NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def add(session, reason, status="held"):
    v = SourceVideo(platform="youtube", video_id=f"v{reason}", channel_id="c", channel_title="C", title="t",
                    duration_s=600, license="", views=0, status="done")
    session.add(v)
    session.flush()
    h = Highlight(source_video_id=v.id, start_s=0, end_s=30, title="t", reason="r", score=70, llm="m",
                  status=status, gate_reason=reason)
    session.add(h)
    session.flush()
    return v, h


def test_moderation_holds_go_back_to_the_gate_hourly(session):
    v, h = add(session, "moderation unavailable (JSONDecodeError); held until it can run")
    v2, other = add(session, "no active account for niche 'x'")
    assert requeue_moderation_holds(session, NOW) == 1
    assert h.status == "candidate" and v.status == "highlighted"
    assert other.status == "held" and v2.status == "done"
    assert session.query(DecisionLog).filter_by(action="requeued").count() == 1
    h.status = "held"
    assert requeue_moderation_holds(session, NOW + timedelta(minutes=30)) == 0  # throttled
    assert requeue_moderation_holds(session, NOW + timedelta(hours=1)) == 1


def test_local_model_flags_are_requeued_for_gemini(session):
    v, h = add(session, "local model flagged: gambling; held for a re-check by Gemini")
    assert requeue_moderation_holds(session, NOW) == 1 and h.status == "candidate"


def test_a_clip_moderation_skipped_is_requeued_too():
    from autoclip.integrations.llm import MockLLM
    from autoclip.pipeline.gate import moderate
    r = moderate(MockLLM(['{"results": []}']), ["x"])[0]
    assert r.hold and r.reason.startswith("moderation unavailable")
