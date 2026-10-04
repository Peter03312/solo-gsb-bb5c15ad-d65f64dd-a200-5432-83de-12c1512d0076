"""培养方案达成课表引擎（app.completion）单元测试。"""
from __future__ import annotations

import pytest

from app.completion import build_completion_plan
from app.planner import Course, PlanError


def _course(code, credits=3, seasons=("SPRING", "FALL"), prereq_groups=(), coreqs=()):
    return Course(code=code, name=code, credits=credits, seasons=frozenset(seasons),
                  prereq_groups=tuple(prereq_groups), coreqs=tuple(coreqs))


SEED_LIKE = {
    c.code: c for c in [
        _course("MATH101", 4), _course("MATH102", 4, prereq_groups=(("MATH101",),)),
        _course("MATH103", 3),
        _course("CS101", 3, coreqs=("CS101L",)), _course("CS101L", 1),
        _course("CS201", 3, prereq_groups=(("CS101",),)),
        _course("CS301", 3, prereq_groups=(("CS201", "CS202"), ("MATH103",))),
        _course("CS302", 3, ("FALL",), prereq_groups=(("CS201",),)),
        _course("CS303", 3, ("SPRING",), prereq_groups=(("CS201",),)),
        _course("CS401", 3, ("FALL",), prereq_groups=(("CS301",),)),
        _course("CS402", 3, ("SPRING",), prereq_groups=(("CS301",), ("MATH102",))),
        _course("CS202", 3, prereq_groups=(("MATH101",),)),
    ]
}
BOUND_CREDITS = {code: c.credits for code, c in SEED_LIKE.items()}
REQUIRED = ["CS101", "CS201", "CS301", "CS401"]
ELECTIVES = [
    {"name": "专业选修", "candidates": ["CS303", "CS402"], "min_credits": 3},
    {"name": "基础拓展", "candidates": ["MATH101", "MATH102", "MATH103"], "min_credits": 6},
]


def _build(**kw):
    args = dict(
        current_catalog=SEED_LIKE, current_equiv_groups=[],
        bound_credits=BOUND_CREDITS, bound_equiv_groups=[],
        required=REQUIRED, elective_groups=ELECTIVES,
        completed=set(), start_semester="2026-SPRING",
        max_credits=12, num_semesters=6)
    args.update(kw)
    return build_completion_plan(**args)


def _semester_codes(outcome):
    return [[c["code"] for c in s["courses"]] for s in outcome["schedule"]["semesters"]]


# ---------------------------------------------------------------- 基本达成

def test_full_plan_from_scratch():
    out = _build(completed={"CS101", "CS101L", "CS201", "MATH103"})
    assert out["achieved"] is True
    assert out["schedule"]["completion_semester"] == "2026-FALL"
    assert _semester_codes(out) == [["CS301", "CS303"], ["CS401", "MATH101"]]
    pa = out["projected_attribution"]
    assert pa["achieved"] and pa["unsatisfied_required"] == []
    # MATH103 已修且归属基础拓展组；MATH101 拟修后与 MATH103 合计 7 -> 计入封顶 6
    basic = next(g for g in pa["elective_groups"] if g["name"] == "基础拓展")
    assert basic["counted_credits"] == 6 and basic["credit_gap"] == 0
    assert {c["code"] for c in basic["attributed_courses"]} == {"MATH101", "MATH103"}
    assert out["current_only_courses"] == []


def test_already_achieved_returns_empty_schedule():
    done = {"CS101", "CS201", "CS301", "CS401", "CS303", "MATH101", "MATH103"}
    out = _build(completed=done)
    assert out["achieved"] is True
    assert out["schedule"]["semesters"] == []
    assert out["schedule"]["completion_semester"] is None
    assert out["projected_attribution"]["achieved"] is True


def test_unknown_completed_course_rejected():
    with pytest.raises(PlanError) as exc:
        _build(completed={"UNKNOWN"})
    assert exc.value.code == "COURSE_MISSING"


def test_no_solution_within_horizon():
    with pytest.raises(PlanError) as exc:
        _build(completed={"CS101", "CS101L", "CS201", "MATH103"}, num_semesters=1)
    assert exc.value.code == "NO_SOLUTION"


# ---------------------------------------------------------------- 互认（绑定目录）

def test_equivalence_attribution_from_bound_catalog():
    out = _build(
        bound_equiv_groups=[["CS301", "CS302"], ["CS401", "CS402"]],
        current_equiv_groups=[["CS301", "CS302"], ["CS401", "CS402"]],
        completed={"CS101", "CS101L", "CS201", "MATH101", "MATH102", "MATH103", "CS302"})
    pa = out["projected_attribution"]
    assert pa["achieved"]
    r301 = next(x for x in pa["required"] if x["code"] == "CS301")
    r401 = next(x for x in pa["required"] if x["code"] == "CS401")
    assert r301["attributed_course"] == {"code": "CS302", "credits": 3, "via": "equivalence"}
    # CS402 拟修：完成必修 CS401（互认）+ 不需要选修时由 CS303 计入选修
    assert r401["attributed_course"]["code"] == "CS402"
    assert r401["attributed_course"]["via"] == "equivalence"


def test_two_required_in_same_bound_group_structurally_conflicts():
    with pytest.raises(PlanError) as exc:
        _build(bound_equiv_groups=[["CS301", "CS302"]],
               required=["CS301", "CS302"], elective_groups=[])
    assert exc.value.code == "REQUIREMENT_CONFLICT"


# ---------------------------------------------------------------- 双目录差异

def test_current_only_course_serves_prereq_but_not_counted():
    current = dict(SEED_LIKE)
    current["NEW299"] = _course("NEW299", 2, ("SPRING",))
    current["CS301"] = _course("CS301", 3, prereq_groups=(("CS201", "CS202", "NEW299"),
                                                          ("MATH103",)))
    out = _build(current_catalog=current, required=["CS301"], elective_groups=[],
                 completed={"MATH103"})
    codes = [c for sem in _semester_codes(out) for c in sem]
    assert "NEW299" in codes and "CS301" in codes
    assert [x["code"] for x in out["current_only_courses"]] == ["NEW299"]
    pa = out["projected_attribution"]
    assert pa["achieved"]
    # NEW299 不在绑定目录：不进入归属，学分不按要求计算
    all_attributed = {x["attributed_course"]["code"] for x in pa["required"] if x["satisfied"]}
    all_attributed |= {c["code"] for g in pa["elective_groups"]
                       for c in g["attributed_courses"]}
    assert "NEW299" not in all_attributed


def test_bound_only_course_cannot_be_planned_but_completed_counts():
    # 绑定目录含 CS301；当前目录删除 CS301。已修 CS301 仍可满足必修
    current = {c: v for c, v in SEED_LIKE.items() if c not in {"CS301"}}
    out = _build(current_catalog=current, required=["CS301", "CS401"],
                 elective_groups=[],
                 completed={"CS101", "CS101L", "CS201", "CS301", "MATH103", "MATH101", "MATH102"})
    pa = out["projected_attribution"]
    # CS301 由已修满足；CS401 是当前目录课程但不在绑定目录……需要补一个绑定：
    # 绑定目录仍含 CS401（BOUND_CREDITS 未变），CS401 可拟修
    assert pa["achieved"]
    r301 = next(x for x in pa["required"] if x["code"] == "CS301")
    assert r301["attributed_course"]["code"] == "CS301"


def test_required_unsatisfiable_when_only_in_bound_catalog():
    # CS301 仅绑定目录有、当前目录无，且未修 -> 无法拟修，结构冲突
    current = {c: v for c, v in SEED_LIKE.items() if c != "CS301"}
    with pytest.raises(PlanError) as exc:
        _build(current_catalog=current, required=["CS301"], elective_groups=[])
    assert exc.value.code == "REQUIREMENT_CONFLICT"


# ---------------------------------------------------------------- 最早完成 + 字典序

def test_earliest_completion_and_lex_tiebreak():
    # 只要求 CS303（春季）：有 CS201 先修。最早 2027-SPRING；字典序沿用既有规划语义
    # （不影响完成学期时课程尽量后置，空学期序列更小）
    out = _build(required=["CS303"], elective_groups=[],
                 completed={"CS101", "CS101L"})
    assert out["schedule"]["completion_semester"] == "2027-SPRING"
    assert _semester_codes(out) == [[], ["CS201"], ["CS303"]]


def test_credits_from_bound_catalog_used_for_threshold():
    # 绑定目录 CS303 学分 6（当前目录仍 3）；门槛 6 时，拟修 CS303 一门即达
    bound = dict(BOUND_CREDITS)
    bound["CS303"] = 6
    out = _build(bound_credits=bound, required=[],
                 elective_groups=[{"name": "g", "candidates": ["CS303"], "min_credits": 6}],
                 completed={"CS101", "CS101L", "CS201"})
    g = out["projected_attribution"]["elective_groups"][0]
    assert g["satisfied"] and g["counted_credits"] == 6
    # 排课学分取当前目录（CS303 仍为 3 学分）
    cs303_sem = next(s for s in out["schedule"]["semesters"]
                     if any(c["code"] == "CS303" for c in s["courses"]))
    assert cs303_sem["total_credits"] == 3


# ---------------------------------------------------------------- 席位

def test_seats_delay_plan():
    out = _build(
        bound_equiv_groups=[["CS401", "CS402"]],
        current_equiv_groups=[["CS401", "CS402"]],
        required=["CS401"], elective_groups=[],
        completed={"CS101", "CS101L", "CS201", "MATH101", "MATH102", "MATH103", "CS301"},
        seat_caps={("CS402", "2026-SPRING"): 0})
    # CS402 春季封死 -> 只能秋季 CS401，顺延一学期
    assert out["schedule"]["completion_semester"] == "2026-FALL"
    codes = [c for sem in _semester_codes(out) for c in sem]
    assert "CS401" in codes


def test_seats_exhausted_no_solution():
    with pytest.raises(PlanError) as exc:
        _build(
            bound_equiv_groups=[["CS401", "CS402"]],
            current_equiv_groups=[["CS401", "CS402"]],
            required=["CS401"], elective_groups=[],
            completed={"CS101", "CS101L", "CS201", "MATH101", "MATH102", "MATH103", "CS301"},
            num_semesters=1, start_semester="2026-SPRING",
            seat_caps={("CS402", "2026-SPRING"): 0})
    assert exc.value.code == "NO_SOLUTION"
