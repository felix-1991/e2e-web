"""Closed model action vocabulary and portable locator descriptors."""

from typing import Any

from .errors import E2EError


CLICK_OPS = {"click", "double_click", "right_click"}
TARGET_OPS = CLICK_OPS | {"long_press", "fill", "type", "press", "check", "uncheck", "select", "hover",
                         "upload", "fill_secret", "drag", "scroll_to"}
FOCUS_OPS = {"type", "press"}
VALUE_OPS = {"fill", "type", "press", "select", "upload"}
VISUAL_OPS = {"click_at", "hover_at", "drag_at", "type_at", "press_at", "select_at"}
TAB_OPS = {"switch_tab", "close_tab"}
SCROLL_DIRECTIONS = {"up", "down", "left", "right"}
MODIFIERS = {"Alt", "Control", "ControlOrMeta", "Meta", "Shift"}


def _is_select_option(value: Any) -> bool:
    if isinstance(value, str):
        return True
    if isinstance(value, dict) and set(value) == {"group", "label"}:
        return all(isinstance(part, str) for part in value.values())
    if not isinstance(value, dict) or len(value) != 1:
        return False
    key, option = next(iter(value.items()))
    if key == "index":
        return type(option) is int and 0 <= option <= 2**53 - 1
    return key in {"label", "value"} and isinstance(option, str)


def recordable_action(action: dict[str, Any]) -> bool:
    op = action["op"]
    return (op not in {"tool", "resize_viewport", "screenshot", "observe"}
            and (op not in FOCUS_OPS or "target" in action) and action.get('times', 1) == 1)


def press_sequence(action: dict[str, Any]) -> list[dict[str, Any]]:
    """Target only the first key press; later presses use the resulting focus."""
    if action['op'] != 'press' or 'times' not in action:
        return [action]
    first = {key: value for key, value in action.items() if key != 'times'}
    return [first, *(dict(op='press', value=action['value']) for _ in range(action['times'] - 1))]


def validate_target(target: Any) -> dict[str, Any]:
    """Validate a serialized locator, including a frame scope and match index."""
    if not isinstance(target, dict) or not isinstance(target.get("by"), str) or target["by"] not in {"role", "text", "label", "placeholder", "test_id", "css", "ref", "semantic"}:
        raise E2EError("MODEL_OUTPUT_INVALID", "target.by must be role, text, label, placeholder, test_id, css, ref or semantic")
    allowed = {"by", "value", "exact", "nth", "frame"} | ({"name"} if target["by"] in {"role", "semantic"} else set())
    if target["by"] == "ref":
        allowed = {"by", "value", "observation"}
        if not isinstance(target.get("observation"), str) or not target["observation"]:
            raise E2EError("MODEL_OUTPUT_INVALID", "ref requires an observation id")
    if target["by"] == "semantic":
        allowed = {"by", "value", "name", "test_id", "frame", "frame_identity", "within", "dynamic_test_id",
                   "href", "input_type", "level", "position", "text", "description"}
        if not isinstance(target.get("name"), str) or not isinstance(target.get("test_id", ""), str):
            raise E2EError("MODEL_OUTPUT_INVALID", "semantic target requires name and optional test_id")
        if "input_type" in target and (not isinstance(target["input_type"], str)
                                       or not 0 < len(target["input_type"]) <= 128):
            raise E2EError("MODEL_OUTPUT_INVALID", "semantic input_type must be bounded text")
        if "level" in target and (target["value"] != "heading" or type(target["level"]) is not int
                                  or not 1 <= target["level"] <= 2**53 - 1):
            raise E2EError("MODEL_OUTPUT_INVALID", "semantic level requires a positive heading level")
        if "href" in target and (target.get("value") != "link" or not isinstance(target["href"], str)
                                 or not target["href"] or len(target["href"]) > 512):
            raise E2EError("MODEL_OUTPUT_INVALID", "semantic href requires a link and a bounded destination")
        if 'dynamic_test_id' in target and (target['dynamic_test_id'] is not True or not target.get('test_id')):
            raise E2EError('MODEL_OUTPUT_INVALID', 'dynamic_test_id requires true and a test_id')
        if 'frame_identity' in target:
            identities = target['frame_identity']
            frames = target.get('frame', [])
            depth = 1 if isinstance(frames, str) else len(frames) if isinstance(frames, list) else -1
            if not isinstance(identities, list) or len(identities) != depth or not all(
                isinstance(item, dict) and set(item) == {'id', 'name', 'src', 'title'} and
                all(isinstance(v, str) for v in item.values()) for item in identities):
                raise E2EError('MODEL_OUTPUT_INVALID', 'frame_identity must describe every frame in the chain')
        for key in ('text', 'description'):
            if key in target and (not isinstance(target[key], str) or not 0 < len(target[key]) <= 512):
                raise E2EError('MODEL_OUTPUT_INVALID', f'semantic {key} must be bounded nonempty text')
        if 'position' in target:
            position = target['position']
            if (not isinstance(position, dict) or set(position) != {'index', 'of'}
                    or type(position['index']) is not int or type(position['of']) is not int
                    or not 1 <= position['of'] <= 1000 or not 0 <= position['index'] < position['of']):
                raise E2EError('MODEL_OUTPUT_INVALID', 'semantic position must name a 0-based index inside a bounded twin count')
        if 'within' in target:
            within = target['within']
            if (not isinstance(within, dict) or set(within) - {'role', 'name', 'test_id'}
                or not all(isinstance(v, str) for v in within.values()) or not within.get('role')
                or not (within.get('name') or within.get('test_id'))):
                raise E2EError('MODEL_OUTPUT_INVALID', 'semantic within requires an ancestor role and a name or test_id')
    if set(target) - allowed or not isinstance(target.get("value"), str) or not target["value"]:
        raise E2EError("MODEL_OUTPUT_INVALID", "target requires a nonempty string value and known fields")
    if "name" in target and not isinstance(target["name"], str):
        raise E2EError("MODEL_OUTPUT_INVALID", "target.name must be a string")
    if "exact" in target and type(target["exact"]) is not bool:
        raise E2EError("MODEL_OUTPUT_INVALID", "target.exact must be boolean")
    if "nth" in target and (type(target["nth"]) is not int or target["nth"] < 0):
        raise E2EError("MODEL_OUTPUT_INVALID", "target.nth must be a nonnegative integer")
    if "frame" in target:
        frames = target["frame"] if isinstance(target["frame"], list) else [target["frame"]]
        if not 1 <= len(frames) <= 8 or not all(isinstance(f, str) and f for f in frames):
            raise E2EError("MODEL_OUTPUT_INVALID", "target.frame must be a selector or 1..8 iframe selectors")
    return dict(target)


def validate_action(action: Any) -> dict[str, Any]:
    """Reject invented operations or malformed values before a side effect."""
    if not isinstance(action, dict):
        raise E2EError("MODEL_OUTPUT_INVALID", "action must be an object")
    op = action.get("op")
    if not isinstance(op, str):
        raise E2EError("MODEL_OUTPUT_INVALID", "action.op must be a string")
    fields = {"op"}
    if op in TARGET_OPS:
        if op not in FOCUS_OPS or "target" in action:
            fields.add("target")
            validate_target(action.get("target"))
        elif op == "type" and "replace" in action:
            fields.add("replace")
            if type(action["replace"]) is not bool:
                raise E2EError("MODEL_OUTPUT_INVALID", "type.replace must be boolean")
        if op in VALUE_OPS:
            fields.add("value")
            value = action.get("value")
            if op == "upload":
                if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
                    raise E2EError("MODEL_OUTPUT_INVALID", "upload value must be a nonempty list of paths")
            elif op == "select":
                if not (all(_is_select_option(option) for option in value) if isinstance(value, list)
                        else _is_select_option(value)):
                    raise E2EError("MODEL_OUTPUT_INVALID", "select value must be a label, a label/value/index descriptor or a list of options")
            elif not isinstance(value, str):
                raise E2EError("MODEL_OUTPUT_INVALID", "action.value must be a string")
            if op == 'press' and 'times' in action:
                fields.add('times')
                if type(action['times']) is not int or not 1 <= action['times'] <= 20:
                    raise E2EError('MODEL_OUTPUT_INVALID', 'press.times must be an integer from 1 to 20')
        elif op == "fill_secret":
            fields.add("secret")
            if not isinstance(action.get("secret"), str):
                raise E2EError("MODEL_OUTPUT_INVALID", "fill_secret requires a named secret")
        elif op == "drag":
            fields.add("to")
            validate_target(action.get("to"))
        elif op in CLICK_OPS and "modifiers" in action:
            fields.add("modifiers")
            modifiers = action["modifiers"]
            if (not isinstance(modifiers, list)
                or not all(isinstance(key, str) and key in MODIFIERS for key in modifiers)
                or len(modifiers) != len(set(modifiers))):
                raise E2EError("MODEL_OUTPUT_INVALID", "modifiers must be an array of distinct supported keys")
    elif op in VISUAL_OPS:
        fields |= {"x", "y", "screenshot"}
        coordinates = {"x", "y"}
        if op == "drag_at":
            coordinates |= {"to_x", "to_y"}
            fields |= coordinates
        if op in {"type_at", "press_at", "select_at"}:
            fields.add("value")
            if not isinstance(action.get("value"), str):
                raise E2EError("MODEL_OUTPUT_INVALID", f"{op} requires a string value")
        if op == "press_at" and not 1 <= len(action["value"]) <= 64:
            raise E2EError("MODEL_OUTPUT_INVALID", "press_at requires a nonempty key of at most 64 characters")
        if op == "type_at" and "replace" in action:
            fields.add("replace")
            if type(action["replace"]) is not bool:
                raise E2EError("MODEL_OUTPUT_INVALID", "type_at.replace must be boolean")
        for key in coordinates:
            if type(action.get(key)) is not int or not 0 <= action[key] <= 100000:
                raise E2EError("MODEL_OUTPUT_INVALID", "coordinates must be nonnegative bounded integers")
        if not isinstance(action.get("screenshot"), str) or not action["screenshot"]:
            raise E2EError("MODEL_OUTPUT_INVALID", "coordinate actions require a screenshot id")
    elif op == "scroll_find":
        fields |= {"target", "direction", "steps"}
        validate_target(action.get("target"))
        if action["target"]["by"] == "ref" or action.get("direction") not in ("up", "down", "left", "right") or type(action.get("steps")) is not int or not 1 <= action["steps"] <= 800:
            raise E2EError("MODEL_OUTPUT_INVALID", "scroll_find requires a portable target, up/down/left/right and 1..800 steps")
        if "container" in action:
            fields.add("container")
            validate_target(action["container"])
    elif op == "navigate":
        fields.add("url")
        if not isinstance(action.get("url"), str):
            raise E2EError("MODEL_OUTPUT_INVALID", "navigate requires a URL")
    elif op == "resize_viewport":
        fields |= {"width", "height"}
        if any(type(action.get(key)) is not int or not 200 <= action[key] <= 4096 for key in ("width", "height")):
            raise E2EError("MODEL_OUTPUT_INVALID", "viewport width and height must be integers from 200 to 4096")
    elif op in TAB_OPS:
        fields.add("tab")
        if type(action.get("tab")) is not int or not 1 <= action["tab"] <= 2**53 - 1:
            raise E2EError("MODEL_OUTPUT_INVALID", "tab must be a positive integer from the observation")
    elif op == "scroll":
        if "direction" in action:
            fields.add("direction")
            if action.get("direction") not in SCROLL_DIRECTIONS:
                raise E2EError("MODEL_OUTPUT_INVALID", "scroll.direction must be up, down, left or right")
            if "times" in action:
                fields.add("times")
                if type(action["times"]) is not int or not 1 <= action["times"] <= 20:
                    raise E2EError("MODEL_OUTPUT_INVALID", "scroll.times must be an integer from 1 to 20")
        else:
            fields |= {"x", "y"}
            if any(type(action.get(k)) is not int or abs(action[k]) > 10000 for k in ("x", "y")):
                raise E2EError("MODEL_OUTPUT_INVALID", "scroll requires bounded integer x/y deltas or a direction")
        if "target" in action:
            fields.add("target")
            validate_target(action["target"])
    elif op == "dialog":
        fields.add("action")
        if action.get("action") not in ("accept", "dismiss"):
            raise E2EError("MODEL_OUTPUT_INVALID", "dialog requires action accept or dismiss")
        if "text" in action:
            fields.add("text")
            if action["action"] != "accept" or not isinstance(action["text"], str) or len(action["text"]) > 1024:
                raise E2EError("MODEL_OUTPUT_INVALID", "dialog text needs accept and at most 1024 characters")
    elif op == "wait":
        fields.add("seconds")
        if type(action.get("seconds")) not in {int, float} or not 0 < action["seconds"] <= 5:
            raise E2EError("MODEL_OUTPUT_INVALID", "wait.seconds must be between 0 and 5")
    elif op == "tool":
        fields |= {"name", "arguments"}
        if not isinstance(action.get("name"), str) or not isinstance(action.get("arguments"), dict):
            raise E2EError("MODEL_OUTPUT_INVALID", "tool requires name and arguments")
    elif op not in {"back", "forward", "reload", "screenshot", "observe"}:
        raise E2EError("MODEL_OUTPUT_INVALID", "unknown action operation")
    if set(action) != fields:
        raise E2EError("MODEL_OUTPUT_INVALID", f"{op} requires exactly {sorted(fields)}")
    return dict(action)
