"""Thin wrapper around the OpenAI SDK. No app/business logic lives here —
callers pass in tool schemas and a tool_runner callback; this module has no
idea what any given tool actually does (Gmail, OCR, or otherwise)."""

import base64
import json
from pathlib import Path

import openai
from openai import OpenAI

MAX_TOOL_ITERATIONS = 4


class OpenAIServiceError(Exception):
    """Raised when the OpenAI request fails, with a human-readable message."""


def _call_responses_api(client: OpenAI, *, model: str, input_items: list, tools: list[dict] | None, reasoning_effort: str | None = None):
    kwargs = {"model": model, "input": input_items, "store": False}
    if tools:
        kwargs["tools"] = tools
    try:
        if reasoning_effort:
            try:
                return client.responses.create(**kwargs, reasoning={"effort": reasoning_effort})
            except openai.BadRequestError as exc:      # this model doesn't accept the setting: carry on without it
                if "reasoning" not in str(exc).lower():
                    raise
        return client.responses.create(**kwargs)
    except openai.AuthenticationError as exc:
        raise OpenAIServiceError(
            "OpenAI rejected the API key. Check OPENAI_API_KEY in experiment DLYTE/.env."
        ) from exc
    except openai.RateLimitError as exc:
        raise OpenAIServiceError("OpenAI rate limit or quota exceeded. Try again shortly.") from exc
    except openai.APIConnectionError as exc:
        raise OpenAIServiceError("Could not reach OpenAI. Check your network connection.") from exc
    except openai.OpenAIError as exc:
        raise OpenAIServiceError(f"OpenAI request failed: {exc}") from exc


def generate_reply(
    messages: list[dict],
    *,
    api_key: str | None,
    model: str,
    tools: list[dict] | None = None,
    tool_runner=None,
    max_iterations: int = MAX_TOOL_ITERATIONS,
    final_summary: bool = False,
    reasoning_effort: str | None = None,
) -> str:
    if not api_key:
        raise OpenAIServiceError(
            "OpenAI API key is missing. Set OPENAI_API_KEY in experiment DLYTE/.env."
        )

    client = OpenAI(api_key=api_key)
    input_items: list = list(messages)

    for _ in range(max_iterations):
        response = _call_responses_api(client, model=model, input_items=input_items, tools=tools, reasoning_effort=reasoning_effort)

        function_calls = [item for item in response.output if getattr(item, "type", None) == "function_call"]
        if not function_calls or not tool_runner:
            return response.output_text

        for call in function_calls:
            # Echo back only the fields the input schema actually accepts —
            # response.output items include output-only fields (e.g.
            # "status") that the API rejects if replayed verbatim.
            input_items.append(
                {
                    "type": "function_call",
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
            )
            arguments = json.loads(call.arguments or "{}")
            result_str = tool_runner(call.name, arguments)
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": result_str,
                }
            )

    if final_summary:
        # Out of tool rounds: let the model write its reply from what the tools already did, with no more tool calls.
        response = _call_responses_api(client, model=model, input_items=input_items, tools=None, reasoning_effort=reasoning_effort)
    return response.output_text or "I wasn't able to finish that — try rephrasing your question."


def generate_text(
    prompt: str,
    *,
    api_key: str | None,
    model: str,
    instructions: str | None = None,
) -> str:
    if not api_key:
        raise OpenAIServiceError(
            "OpenAI API key is missing. Set OPENAI_API_KEY in experiment DLYTE/.env."
        )

    client = OpenAI(api_key=api_key)
    input_items: list[dict] = []
    if instructions:
      input_items.append({"role": "system", "content": instructions})
    input_items.append({"role": "user", "content": prompt})
    response = _call_responses_api(client, model=model, input_items=input_items, tools=None)
    return (response.output_text or "").strip()


def generate_text_from_images(
    prompt: str,
    image_paths: list[str],
    *,
    api_key: str | None,
    model: str,
    instructions: str | None = None,
) -> str:
    if not api_key:
        raise OpenAIServiceError(
            "OpenAI API key is missing. Set OPENAI_API_KEY in experiment DLYTE/.env."
        )

    content: list[dict] = [{"type": "input_text", "text": prompt}]
    for raw_path in image_paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        mime_type = "image/png"
        if path.suffix.lower() in {".jpg", ".jpeg"}:
            mime_type = "image/jpeg"
        if path.suffix.lower() == ".webp":
            mime_type = "image/webp"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:{mime_type};base64,{encoded}",
            }
        )

    client = OpenAI(api_key=api_key)
    input_items: list[dict] = []
    if instructions:
        input_items.append({"role": "system", "content": instructions})
    input_items.append({"role": "user", "content": content})
    response = _call_responses_api(client, model=model, input_items=input_items, tools=None)
    return (response.output_text or "").strip()
