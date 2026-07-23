# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Secret request tool for scoped runtime credentials."""

from __future__ import annotations

import json

from mclaw.runtime.secrets import SecretRequestError, secret_request_many
from mclaw.tools.cancellation import cancellation_checkpoint
from mclaw.tools.interrupt import get_interrupt_event
from mclaw.tools.registry import registry


SECRET_REQUEST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "secret_request_many",
        "description": (
            "Configure or authorize one or more scoped environment-variable secrets. "
            "This tool may prompt the user through the CLI, writes accepted values only to "
            "the M-Claw home .env file, updates the scoped allowlist, and never returns plaintext secret values. "
            "After this tool succeeds, any terminal command that reads or uses these env vars "
            "must pass the same required_for scope."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "required_for": {
                    "type": "string",
                    "pattern": "^(skill|tool|runtime|channel):[^\\s:]+$",
                    "description": (
                        "Scope that needs the secret, in the form skill:<name>, tool:<name>, "
                        "runtime:<name>, or channel:<name>. Use this exact same value later as "
                        "terminal(required_for=...) when a command reads or uses these env vars."
                    ),
                },
                "secrets": {
                    "type": "array",
                    "minItems": 1,
                    "description": (
                        "Environment variable names required by this scope, not plaintext secret values. "
                        "Extract names from SKILL.md, dependency_hints, docs, or error messages "
                        "(for example MINIMAX_API_KEY). Each item may be an env var string or an object."
                    ),
                    "items": {
                        "oneOf": [
                            {
                                "type": "string",
                                "pattern": "^[A-Z_][A-Z0-9_]{0,63}$",
                                "description": "Uppercase environment variable name, for example MINIMAX_API_KEY.",
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "env_var": {
                                        "type": "string",
                                        "pattern": "^[A-Z_][A-Z0-9_]{0,63}$",
                                        "description": "Uppercase environment variable name, for example MINIMAX_API_KEY.",
                                    },
                                    "provider": {"type": "string", "description": "Optional provider/service name."},
                                    "purpose": {"type": "string", "description": "Why this scope needs the secret."},
                                },
                                "required": ["env_var"],
                            },
                        ],
                    },
                },
                "force_refresh": {
                    "type": "boolean",
                    "description": (
                        "Set true only after an API or CLI reports authentication failure so the user "
                        "can re-enter and overwrite already configured secrets. Plaintext is still never returned."
                    ),
                },
            },
            "required": ["required_for", "secrets"],
        },
    },
}


def _handle_secret_request_many(args: dict, **kwargs) -> str:
    """Bridge model tool calls to the scoped secret authorization service."""
    cancel_event = get_interrupt_event()
    parent_agent = kwargs.get("parent_agent")
    callback = getattr(parent_agent, "secret_request_callback", None) if parent_agent is not None else None
    if not callable(callback):
        callback = None
    try:
        cancellation_checkpoint(cancel_event)
        result = secret_request_many(
            str(args.get("required_for") or ""),
            list(args.get("secrets") or []),
            prompt_callback=callback,
            force_refresh=bool(args.get("force_refresh", False)),
            cancel_event=cancel_event,
        )
    except InterruptedError as exc:
        result = {
            "success": False,
            "error": str(exc),
            "status": "cancelled",
            "interrupted": True,
        }
    except SecretRequestError as exc:
        result = {"success": False, "error": str(exc)}
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="secret_request_many",
    toolset="credentials",
    schema=SECRET_REQUEST_SCHEMA,
    handler=_handle_secret_request_many,
    description="Request scoped secret authorization",
    emoji="🔐",
)
