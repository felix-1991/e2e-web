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


def test_selection_delta_is_bound_to_its_native_control():
    def view(first, second):
        def select(name, selected):
            return [f'- combobox "{name}":', *[
                f'  - option "{option}"' + (' [selected]' if selected == option else '')
                for option in ('A', 'B')]]
        return observation(*select('Source', first), *select('Destination', second))

    before, after = view('A', 'A'), view('B', 'A')
    entry = end_recording(before, after)
    assert matches_end(entry, before, after)
    assert not matches_end(entry, before, view('A', 'B'))


@pytest.mark.parametrize(("owner", "child", "state"), [
    ("listbox", "option", "selected"), ("radiogroup", "radio", "checked"),
    ("tablist", "tab", "selected"), ("menu", "menuitemcheckbox", "checked"),
])
def test_composite_state_delta_requires_the_same_owner(owner, child, state):
    def view(selected):
        rows = []
        for name in ("Source", "Destination"):
            rows.extend([f'- {owner} "{name}"', '  - group ""',
                         f'    - {child} "Default"' + (f' [{state}]' if name == selected else '')])
        return observation(*rows)

    before = view(None)
    trace = end_recording(before, view("Source"))
    assert matches_end(trace, before, view("Source"))
    assert not matches_end(trace, before, view("Destination"))


def test_truncated_end_cannot_prove_disappearance():
    before, after = observation('- button "Apply"'), observation()
    assert not matches_end(end_recording(before, after), before, dict(after, truncated=True))


def test_partial_select_options_cannot_prove_option_disappeared():
    before = observation('- combobox "Country":', '  - option "A"')
    after = observation('- combobox "Country":')
    entry = end_recording(before, after)
    partial = {"nodes": [{"options_truncated": True}]}
    assert not matches_end(entry, before, dict(after, **partial))
    assert not matches_end(entry, dict(before, **partial), after)


def test_passive_clock_delta_defers_to_stable_result_and_preserves_clock_only_evidence():
    start = observation('- text "12:30:10"')
    end = observation('- text "12:30:11"', '- status "Saved"')
    trace = end_recording(start, end)
    assert matches_end(trace, observation('- text "16:30:00"'), observation('- text "16:30:01"', '- status "Saved"'))
    assert not matches_end(trace, start, observation('- text "12:30:11"'))
    assert delta(start, observation('- text "12:30:11"')) == (['- text "12:30:11"'], ['- text "12:30:10"'])


def test_checkbox_delta_is_bound_to_table_row():
    def view(checked):
        rows = []
        for name in ('Alice', 'Bob'):
            rows.extend([f'- row "{name}"', '  - cell ""', '    - checkbox "Enable"' +
                         (' [checked]' if checked == name else '')])
        return observation(*rows)

    trace = end_recording(view(None), view('Alice'))
    assert not matches_end(trace, view(None), view('Bob'))
