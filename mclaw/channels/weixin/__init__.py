# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Weixin private-chat channel runtime package for M-Claw."""

__all__ = ["WeixinRuntime", "qr_login"]


def __getattr__(name):
    if name == "WeixinRuntime":
        from mclaw.channels.weixin.runtime import WeixinRuntime
        return WeixinRuntime
    if name == "qr_login":
        from mclaw.channels.weixin.qr_login import qr_login
        return qr_login
    raise AttributeError(name)
