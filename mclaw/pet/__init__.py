# Copyright © 2026 Shenzhen Kaihong Digital Industry Development Co., Ltd.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Animated desktop pet integration for M-Claw."""

from mclaw.pet.controller import PetController
from mclaw.pet.events import PetEvent, PetEventType, PetState

__all__ = ["PetController", "PetEvent", "PetEventType", "PetState"]
