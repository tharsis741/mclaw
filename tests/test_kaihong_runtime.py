# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from mclaw.agent import prompt_builder
from mclaw.platform import detect as platform_detect
from mclaw.platform.detect import PlatformInfo
from mclaw.runtime import bootstrap, kaihong
from mclaw.runtime.bootstrap import BootstrapPathResolver
from mclaw.runtime.features import FeatureState
from mclaw.runtime.kaihong import KaihongRuntime


def _set_board_layout(monkeypatch, tmp_path: Path, parameters: str) -> None:
    param_file = tmp_path / "ohos.para"
    param_file.write_text(
        parameters + "\npersist.device.secure.KEY=not-for-runtime\n",
        encoding="utf-8",
    )
    run = tmp_path / "run"
    run.write_text("#!/bin/sh\n", encoding="utf-8")
    release = tmp_path / "release"
    release.mkdir()

    monkeypatch.setattr(bootstrap, "_OHOS_PARAMETER_FILES", (param_file,))
    monkeypatch.setattr(bootstrap, "_KAIHONG_RUN", run)
    monkeypatch.setattr(bootstrap, "_KAIHONG_RELEASE_ROOT", release)
    monkeypatch.setattr(bootstrap.sys, "platform", "linux")
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "aarch64")


@pytest.mark.parametrize(
    "parameters",
    (
        "\n".join(
            (
                "const.product.brand=Kaihong",
                "const.ohos.version=KaihongOS 5.0.1.52",
                "const.ohos.fullname=OpenHarmony-5.0.2.123",
            )
        ),
        "\n".join(
            (
                "const.product.brand=Kaihong",
                "const.product.software.version=M-Robots OS 6.1.0.04Stan",
                "const.ohos.version=KaihongOS 6.1.0.04",
                "const.ohos.fullname=OpenHarmony-6.1.0.31",
            )
        ),
        "const.ohos.fullname=OpenHarmony-compatible",
    ),
)
def test_kaihong_detection_uses_identity_or_openharmony_release_layout(
    monkeypatch,
    tmp_path: Path,
    parameters: str,
) -> None:
    _set_board_layout(monkeypatch, tmp_path, parameters)

    assert BootstrapPathResolver.is_kaihong_host()
    assert "persist.device.secure.KEY" not in BootstrapPathResolver.read_ohos_parameters()


def test_release_layout_alone_is_not_kaihong_identity(monkeypatch, tmp_path: Path) -> None:
    _set_board_layout(monkeypatch, tmp_path, "")
    missing_parameter_file = tmp_path / "missing.para"
    monkeypatch.setattr(bootstrap, "_OHOS_PARAMETER_FILES", (missing_parameter_file,))

    assert not BootstrapPathResolver.is_kaihong_host()


def test_kaihong_terminal_inherits_environment_and_uses_real_shell(monkeypatch) -> None:
    inherited = {
        "PATH": "/data/local/release/bin:/bin",
        "LD_LIBRARY_PATH": "/data/local/release/lib:/system/lib64",
        "LD_PRELOAD": "/data/local/release/usr/lib/libpython3.12.so.1.0",
        "PYTHONHOME": "/data/local/release/usr",
        "PYTHONPATH": "/data/local/release/usr/lib/python3.12/site-packages",
        "TMPDIR": "/data/local/tmp",
        "TERM": "xterm-256color",
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("RELEASE_ROOT", raising=False)
    monkeypatch.setattr(kaihong, "_pet_feature", lambda: FeatureState.AVAILABLE_WITH_CONFIG)
    monkeypatch.setattr(kaihong, "_browser_feature", lambda: FeatureState.AVAILABLE_WITH_INSTALL)
    monkeypatch.setattr(kaihong.shutil, "which", lambda _name: None)

    runtime = KaihongRuntime()
    environment = runtime.build_env()
    argv = runtime.shell.argv("python3 --version", "/data/local/tmp")

    assert runtime.shell.name == "sh"
    assert argv[:2] == ["/bin/sh", "-c"]
    assert "/bin/run" not in argv[-1]
    assert {name: environment[name] for name in inherited} == inherited
    assert "RELEASE_ROOT" not in environment


def test_kaihong_environment_prompt_matches_both_boards(monkeypatch) -> None:
    board_5 = PlatformInfo(
        os_name="Linux",
        os_release="5.10.110",
        architecture="aarch64",
        python_version="3.12.7",
        python_executable="/data/local/release/bin/python3",
        cwd="/data/local/tmp",
        shell_path="/bin/sh",
        shell_name="sh",
        shell_implementation="MirBSD mksh R59",
        kaihong_version="KaihongOS 5.0.1.52",
        base_system="OpenHarmony-5.0.2.123",
        distribution="KaihongOS 5.0.1.52",
        device_name="Kaihong BotBook",
        device_model="KHP-LC802",
        is_windows=False,
        is_linux=True,
        is_macos=False,
        is_wsl=False,
        gui_available=False,
        audio_input_available=True,
        runtime_mode="kaihong",
    )
    board_6 = replace(
        board_5,
        os_release="6.6.101",
        kaihong_version="KaihongOS 6.1.0.04",
        base_system="OpenHarmony-6.1.0.31",
        distribution="M-Robots OS 6.1.0.04Stan",
        device_name="KaihongBoard-3588S",
        device_model="ohos",
    )
    expected = (
        "OS: KaihongOS 5.0.1.52, based on OpenHarmony-5.0.2.123\n"
        "Device: Kaihong BotBook (KHP-LC802)\n"
        "Kernel: Linux 5.10.110 (aarch64)\n"
        "Shell: /bin/sh (MirBSD mksh R59)\n"
        "M-Claw Python: 3.12.7 (/data/local/release/bin/python3)\n"
        "Initial working directory: /data/local/tmp",
        "OS: KaihongOS 6.1.0.04, based on OpenHarmony-6.1.0.31\n"
        "Distribution: M-Robots OS 6.1.0.04Stan\n"
        "Device: KaihongBoard-3588S (ohos)\n"
        "Kernel: Linux 6.6.101 (aarch64)\n"
        "Shell: /bin/sh (MirBSD mksh R59)\n"
        "M-Claw Python: 3.12.7 (/data/local/release/bin/python3)\n"
        "Initial working directory: /data/local/tmp",
    )

    for info, output in zip((board_5, board_6), expected):
        monkeypatch.setattr(prompt_builder, "get_platform_info", lambda config=None, value=info: value)
        assert prompt_builder._build_platform_block(model="ignored") == output


def test_mksh_version_is_normalized(monkeypatch) -> None:
    monkeypatch.setattr(
        platform_detect.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="@(#)MIRBSD KSH R59 2020/10/31",
        ),
    )

    assert platform_detect._shell_implementation("/bin/sh") == "MirBSD mksh R59"


def test_parameter_probe_uses_system_linker_environment(monkeypatch) -> None:
    captured = {}
    monkeypatch.setenv("LD_LIBRARY_PATH", "/data/local/release/lib")
    monkeypatch.setenv("LD_PRELOAD", "/data/local/release/lib/example.so")

    def run(*_args, **kwargs):
        captured.update(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="OpenHarmony-5.0.2.123")

    monkeypatch.setattr(platform_detect.subprocess, "run", run)

    assert (
        platform_detect._read_live_parameter("/bin/param", "const.ohos.fullname")
        == "OpenHarmony-5.0.2.123"
    )
    assert "LD_LIBRARY_PATH" not in captured
    assert "LD_PRELOAD" not in captured
