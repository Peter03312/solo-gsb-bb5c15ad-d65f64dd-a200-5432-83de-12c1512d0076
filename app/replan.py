"""已确认方案执行中途的滚动重排。

输入原方案、已结束学期数与截至该时点的累计已修课程；沿用原方案的目标课程、
每学期学分上限，从原起始学期顺延 elapsed_semesters 个学期后的下一学期起，
按“当前目录”在剩余学期数内求最早完成的完整未来课表，并给出与原方案
剩余学期课表的课程差异。
"""
from __future__ import annotations

from .planner import SEASONS, PlanError, build_plan, parse_semester


def next_semester_label(start_semester, elapsed_semesters):
    """原起始学期顺延 elapsed_semesters 个学期后的下一学期标签。"""
    year, season = parse_semester(start_semester)
    idx = SEASONS.index(season)
    year += (idx + elapsed_semesters) // 2
    season = SEASONS[(idx + elapsed_semesters) % 2]
    return f"{year}-{season}"


def _sort_key(label):
    y, s = label.split("-")
    return int(y), SEASONS.index(s)


def remaining_diff(original_result, new_result, elapsed_semesters):
    """新方案与原方案“剩余学期”（跳过前 elapsed_semesters 个已结束学期）的课程差异。

    原方案按学期顺序跳过前 elapsed_semesters 个学期后，与新课表按学期标签对齐，
    分别给出每学期的新增/移除课程，并汇总去重后的新增/移除课程。
    """
    by_semester = {}
    for s in original_result.get("semesters", [])[elapsed_semesters:]:
        by_semester.setdefault(s["semester"], {})["old"] = {c["code"] for c in s["courses"]}
    for s in new_result.get("semesters", []):
        by_semester.setdefault(s["semester"], {})["new"] = {c["code"] for c in s["courses"]}

    semesters, added_all, removed_all = [], set(), set()
    for label in sorted(by_semester, key=_sort_key):
        old = by_semester[label].get("old", set())
        new = by_semester[label].get("new", set())
        added, removed = sorted(new - old), sorted(old - new)
        if not added and not removed:
            continue
        added_all.update(added)
        removed_all.update(removed)
        semesters.append({"semester": label, "added_courses": added, "removed_courses": removed})

    return {
        "semesters": semesters,
        "added_courses": sorted(added_all),
        "removed_courses": sorted(removed_all),
    }


def replan_plan(catalog, original_request, original_result, elapsed_semesters, completed,
                equiv_groups=None, seat_caps=None, occupied=None):
    """执行滚动重排，返回 (effective_request, result, diff)；失败抛出 PlanError。

    equiv_groups 为重排所使用目录版本声明的课程互认组（按当前目录判断互认）。
    seat_caps/occupied 为席位上限与重排时点的占用余量（调用方应已剔除来源方案
    尚未结束学期的自身占位，那些占位确认时将被新课表替换）。
    """
    if elapsed_semesters >= int(original_request["num_semesters"]):
        raise PlanError(
            "INVALID_INPUT",
            f"已结束学期数 {elapsed_semesters} 必须小于原规划期限 "
            f"{original_request['num_semesters']}",
        )

    declared = set(original_request.get("completed_courses", []))
    revoked = sorted(declared - set(completed))
    if revoked:
        raise PlanError(
            "COMPLETED_REVOKED",
            "累计已修课程不得撤销原方案创建时声明的已修课程，缺少：" + "、".join(revoked),
        )

    remaining = int(original_request["num_semesters"]) - elapsed_semesters
    start_label = next_semester_label(original_request["start_semester"], elapsed_semesters)
    effective_request = {
        "completed_courses": sorted(completed),
        "target_courses": list(original_request["target_courses"]),
        "start_semester": start_label,
        "max_credits_per_semester": int(original_request["max_credits_per_semester"]),
        "num_semesters": remaining,
    }
    result = build_plan(
        catalog,
        completed=set(completed),
        targets=original_request["target_courses"],
        start_semester=start_label,
        max_credits=int(original_request["max_credits_per_semester"]),
        num_semesters=remaining,
        equiv_groups=equiv_groups,
        seat_caps=seat_caps,
        occupied=occupied,
    )
    diff = remaining_diff(original_result, result, elapsed_semesters)
    return effective_request, result, diff
