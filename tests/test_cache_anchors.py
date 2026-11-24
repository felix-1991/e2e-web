"""Replay postconditions for repeated semantic rows and scoped alerts."""

import pytest

from e2e_web.cache import delta, matches_end, route


def observation(*lines, frames=()):
    return {"url": "https://app.example/form", "tree": "\n".join(lines), "frames": frames}


def end_recording(before, after):
    appeared, gone = delta(before, after)
    return {"appeared": appeared, "gone": gone, "end": route(after["url"])}


@pytest.mark.parametrize("start, end", [(1, 2), (2, 1), (1, 3), (3, 1)])
def test_replay_checks_the_change_in_counts(start, end):
    row = '- text "Item"'
    before, after = observation(*([row] * start)), observation(*([row] * end))
    trace = end_recording(before, after)
    assert matches_end(trace, before, after)
    assert not matches_end(trace, after, after)
    if abs(end - start) > 1:
        intermediate_count = (start + end) // 2
        assert not matches_end(trace, before, observation(*([row] * intermediate_count)))


@pytest.mark.parametrize("alert", ['- alert:', '- alert "Unexpected"', '- alertdialog "Unexpected"'])
def test_iframe_alerts_are_scoped_and_counted(alert):
    def frame(selector, *lines):
        return {"selector": selector, "tree": "\n".join(lines)}

    status = '- status "Saved"'
    before = observation(frames=[frame("iframe >> nth=0")])
    after = observation(frames=[frame("iframe >> nth=0", status)])
    new_alert = observation(frames=[frame("iframe >> nth=0", status, alert)])
    doubled = observation(frames=[frame("iframe >> nth=0", status, alert, alert)])
    other_frame = observation(frames=[frame("iframe >> nth=0", status, alert), frame("iframe >> nth=1", alert)])

    assert not matches_end(end_recording(before, after), before, new_alert)
    expected = end_recording(before, new_alert)
    assert matches_end(expected, before, new_alert)
    assert not matches_end(expected, before, doubled)
    assert not matches_end(expected, before, other_frame)
