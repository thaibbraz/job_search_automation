"""
daily_targets.py — people with their own daily job limit.

Everyone gets JOBBYO_TARGET_JOBS a day, except the older customers listed here,
whose plan gives them fewer. The search stops at their limit and the admin
doesn't show them as short. Keyed by uid.
"""

import os

USER_TARGETS = {
    "qhGnHvSJcuTTF9Ix9HijHY15SIA2": 5,
    "YyyohBmQ8WcDRQy7SOet2MBD4rW2": 5,
}


def for_user(uid):
    return USER_TARGETS.get(uid) or int(os.getenv("JOBBYO_TARGET_JOBS", "10"))
