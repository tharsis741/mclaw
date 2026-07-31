# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import pytest

from mclaw.cli.app import InteractiveChat
from mclaw.cli.main import _parse_secret_batch_values as parse_setup_secrets


@pytest.mark.parametrize(
    "parse",
    [InteractiveChat._parse_secret_batch_values, parse_setup_secrets],
)
def test_single_secret_input_is_literal_unless_it_names_the_requested_variable(parse) -> None:
    assert parse("YWJjZA==", ["DEMO_KEY"]) == ({"DEMO_KEY": "YWJjZA=="}, "")
    assert parse("DEMO_KEY=YWJjZA==", ["DEMO_KEY"]) == ({"DEMO_KEY": "YWJjZA=="}, "")
    json_secret = '{"type":"service_account"}'
    assert parse(json_secret, ["DEMO_KEY"]) == ({"DEMO_KEY": json_secret}, "")


@pytest.mark.parametrize(
    "parse",
    [InteractiveChat._parse_secret_batch_values, parse_setup_secrets],
)
def test_multiple_secret_json_requires_non_empty_string_values(parse) -> None:
    env_vars = ["FIRST_KEY", "SECOND_KEY"]
    assert parse('{"FIRST_KEY":"one","SECOND_KEY":"two"}', env_vars) == (
        {"FIRST_KEY": "one", "SECOND_KEY": "two"},
        "",
    )

    values, error = parse('{"FIRST_KEY":null,"SECOND_KEY":"two"}', env_vars)
    assert values == {}
    assert error


@pytest.mark.parametrize(
    "parse",
    [InteractiveChat._parse_secret_batch_values, parse_setup_secrets],
)
def test_multiple_secrets_require_names_or_json(parse) -> None:
    env_vars = ["FIRST_KEY", "SECOND_KEY"]

    assert parse("FIRST_KEY=YWJjZA==;SECOND_KEY=c2VjcmV0==", env_vars) == (
        {"FIRST_KEY": "YWJjZA==", "SECOND_KEY": "c2VjcmV0=="},
        "",
    )
    for positional in ("one;two", "YWJjZA==;c2VjcmV0=="):
        values, error = parse(positional, env_vars)
        assert values == {}
        assert error
