import base64
import json

from proxy import PLACEHOLDER, prune_responses_payload


def image(char: bytes = b"x", size: int = 12_000) -> str:
    return "data:image/png;base64," + base64.b64encode(char * size).decode()


def response_body(items: list[dict]) -> bytes:
    return json.dumps({"model": "gpt-test", "stream": True, "input": items}).encode()


def test_keeps_latest_two_image_items_and_prunes_older_images():
    body = response_body(
        [
            {"role": "user", "content": [{"type": "input_image", "image_url": image(b"a")}]},
            {"role": "assistant", "content": [{"type": "output_text", "text": "seen"}]},
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "example",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": [{"type": "input_image", "image_url": image(b"b")}],
            },
            {"role": "user", "content": [{"type": "input_image", "image_url": image(b"c")}]},
        ]
    )

    pruned, stats = prune_responses_payload(body, keep_recent_image_items=2, max_body_bytes=10_000_000)
    data = json.loads(pruned)

    assert stats.found_images == 3
    assert stats.removed_images == 1
    assert data["input"][0]["content"][0] == {"type": "input_text", "text": PLACEHOLDER}
    assert data["input"][3]["output"][0]["image_url"].startswith("data:image/png;base64,")
    assert data["input"][4]["content"][0]["image_url"].startswith("data:image/png;base64,")


def test_hard_budget_removes_recent_images_oldest_first():
    body = response_body(
        [
            {"role": "user", "content": [{"type": "input_image", "image_url": image(b"a", 90_000)}]},
            {"role": "user", "content": [{"type": "input_image", "image_url": image(b"b", 90_000)}]},
        ]
    )

    pruned, stats = prune_responses_payload(body, keep_recent_image_items=2, max_body_bytes=130_000)
    data = json.loads(pruned)

    assert len(pruned) <= 130_000
    assert stats.removed_images == 1
    assert data["input"][0]["content"][0]["type"] == "input_text"
    assert data["input"][1]["content"][0]["type"] == "input_image"


def test_prunes_raw_image_generation_result_as_a_whole_item():
    raw = base64.b64encode(b"z" * 12_000).decode()
    body = response_body(
        [
            {"type": "image_generation_call", "id": "ig_1", "status": "completed", "result": raw},
            {"role": "user", "content": [{"type": "input_image", "image_url": image()}]},
        ]
    )

    pruned, stats = prune_responses_payload(body, keep_recent_image_items=1, max_body_bytes=10_000_000)
    data = json.loads(pruned)

    assert stats.removed_images == 1
    assert data["input"][0]["role"] == "user"
    assert data["input"][0]["content"][0]["text"] == PLACEHOLDER


def test_non_image_payload_is_unchanged_semantically():
    body = response_body([{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}])
    pruned, stats = prune_responses_payload(body)

    assert json.loads(pruned) == json.loads(body)
    assert stats.found_images == 0
    assert stats.removed_images == 0


def test_removes_orphan_function_output_but_keeps_paired_output():
    body = response_body(
        [
            {
                "type": "function_call_output",
                "id": "fco_orphan",
                "output": "delegated message",
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "example",
                "arguments": "{}",
            },
            {
                "type": "function_call_output",
                "id": "fco_1",
                "call_id": "call_1",
                "output": "ok",
            },
            {"role": "user", "content": [{"type": "input_text", "text": "continue"}]},
        ]
    )

    pruned, stats = prune_responses_payload(body)
    data = json.loads(pruned)

    assert stats.removed_orphan_outputs == 1
    assert [item.get("id") for item in data["input"] if isinstance(item, dict)] == [
        "fc_1",
        "fco_1",
        None,
    ]
