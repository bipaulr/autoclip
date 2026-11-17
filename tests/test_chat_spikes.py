from autoclip.pipeline.chat_spikes import Spike, detect_spikes, window_counts


def msgs(burst_at=300, n=80, quiet_every=5, total=600):
    out = [(float(t), "hi") for t in range(0, total, quiet_every)]
    out += [(burst_at + i * 0.1, "NO WAY LMAO") for i in range(n)]
    return out


def test_window_counts_bins_by_10s():
    assert window_counts([(0, "a"), (9.9, "b"), (10, "c"), (25, "d")], 30) == [2, 1, 1]


def test_burst_is_found_with_keywords():
    spikes = detect_spikes(msgs(), 600)
    assert [s.start for s in spikes] == [300]
    assert spikes[0].count >= 80 and "LMAO" in spikes[0].keywords


def test_quiet_chat_has_no_spikes():
    assert detect_spikes([(float(t), "hi") for t in range(0, 600, 5)], 600) == []


def test_tiny_bursts_are_ignored_even_on_a_dead_chat():
    assert detect_spikes([(100.0, "hi"), (101.0, "hi"), (102.0, "hi")], 300) == []


def test_adjacent_windows_merge_into_one_spike():
    chat = [(float(t), "hi") for t in range(0, 600, 5)]
    chat += [(300 + i * 0.2, "WOW") for i in range(60)] + [(312 + i * 0.2, "WOW") for i in range(60)]
    spikes = detect_spikes(chat, 600)
    assert len(spikes) == 1 and spikes[0].start == 300 and spikes[0].end == 320


def test_baseline_adapts_to_a_busy_chat():
    # a chat that always runs ~40 msgs/10s must not flag its own normal level
    chat = [(i * 0.25, "hi") for i in range(2400)]
    assert detect_spikes(chat, 600) == []


def test_spike_is_a_value_object():
    assert Spike(10, 20, 55, ["x"]).peak_t == 10
