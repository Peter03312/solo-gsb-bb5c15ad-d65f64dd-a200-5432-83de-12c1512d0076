"""培养方案达成课表引擎。

学生凭**培养方案要求版本、累计已修课程、起始学期、每学期学分上限与规划学期数**
申请达成课表。双目录语义：

- **绑定目录**（要求版本登记时绑定的目录版本）：决定互认关系与计入学分
  （学分、互认组均取该版本快照）；已修课程必须在绑定目录中存在。
- **当前目录**：决定未来拟修课程的开课学期、先修、同修、排课学分与席位余量。
- **仅两版目录中都存在的课程**拟修后才可计入要求；当前目录独有课程只可作为
  先修/同修等必要课程（不计入要求，单独列示）。

优化目标与既有规划一致：期限内**最早完成**全部必修及选修门槛；完成学期相同者，
按各学期课程代码序列（学期内升序、再按学期先后）的字典序取最小。
课表拟修课程不写成历史已修；席位按当时余量硬约束、待确认不占位。
"""
from __future__ import annotations

from itertools import product

from .audit import run_audit
from .planner import (PlanError, _block_requirements, _build_blocks,
                      _deadlocked_codes, _enumerate_sets, _equiv_map,
                      _schedule, _seat_bottlenecks, parse_semester,
                      semester_label)

# 达成课表“计要求课程选择”的组合数上限（必修选择 × 各组选修选择），防止指数爆炸。
SELECTION_CAP = 512


def _unit_map(equiv_groups):
    """互认组 -> {课程代码: 所在互认单元 frozenset}；未归组课程自成单元。"""
    mapping = {}
    for group in equiv_groups or []:
        members = frozenset(c.strip().upper() for c in group if c.strip())
        for code in members:
            mapping[code] = members
    return mapping


def _minimal_elective_subsets(pool_units, gap, threshold, bound_credits, counter):
    """枚举选修组“拟修即可补齐缺口”的最小课程子集（按绑定目录学分计）。

    pool_units: [(互认单元, [该单元内两版共有且未修的候选课程])]，同一互认单元
                至多择一（同组至多计入一门）。
    返回若干 frozenset（课程集合）：包含空集（缺口可能由必修拟修课或先修闭包中
    可计入的课程顺带补齐），以及“总学分 >= gap 且去掉任一门即 < gap”的最小子集
    （其总学分可能超过门槛，但至多超出一门课的学分；更大的子集被这些最小集支配）。
    """
    subsets = [frozenset()]

    def rec(ui, chosen, chosen_credits):
        counter[0] += 1
        if counter[0] > SELECTION_CAP:
            raise PlanError("TOO_COMPLEX", "达成课表的选修组合过多，超出复杂度限制")
        if ui == len(pool_units):
            return
        # 跳过该互认单元
        rec(ui + 1, chosen, chosen_credits)
        unit, codes = pool_units[ui]
        for code in codes:
            new_credits = chosen_credits + bound_credits[code]
            new_chosen = chosen | {code}
            # 最小性：该子集达到缺口，且去掉其中任一门后都低于缺口才保留
            if new_credits >= gap and all(
                new_credits - bound_credits[c] < gap for c in new_chosen
            ):
                subsets.append(frozenset(new_chosen))
            if new_credits < threshold:
                rec(ui + 1, new_chosen, new_credits)

    rec(0, frozenset(), 0)
    return subsets


def build_completion_plan(*, current_catalog, current_equiv_groups,
                          bound_credits, bound_equiv_groups,
                          required, elective_groups, completed,
                          start_semester, max_credits, num_semesters,
                          seat_caps=None, occupied=None):
    """生成达成课表；失败抛 PlanError（未知要求/已修课、结构不可达成、期限内无解）。

    current_catalog:    当前目录 {code: planner.Course}，决定拟修课的开课/先修/同修/学分
    current_equiv_groups: 当前目录版本声明的互认组，决定未来课程先修/同修的互认替代
    bound_credits:      绑定目录版本 {code: 学分}，决定计入学分
    bound_equiv_groups: 绑定目录版本声明的互认组，决定互认归属
    required:           必修课程代码列表（绑定目录语义、声明顺序）
    elective_groups:    [{"name","candidates","min_credits"}]（绑定目录语义、优先级顺序）
    completed:          累计已修课程（必须在绑定目录中存在；是否在当前目录不做要求）
    seat_caps/occupied: 席位上限与已占用（语义同 planner.build_plan）
    """
    start_year, start_season = parse_semester(start_semester)
    completed = {c.strip().upper() for c in completed}
    required = [r.strip().upper() for r in required]
    seat_caps = seat_caps or {}
    occupied = occupied or {}

    # ---- 已修课必须在要求绑定目录中存在（不要求在当前目录）----
    missing = sorted(c for c in completed if c not in bound_credits)
    if missing:
        raise PlanError(
            "COURSE_MISSING",
            f"已修课程缺失：{'、'.join(missing)} 不在要求版本绑定的目录版本中")

    current_equiv = _equiv_map(current_equiv_groups)
    bound_unit = _unit_map(bound_equiv_groups)

    def bunit(code):
        return bound_unit.get(code, frozenset({code}))

    current_codes = set(current_catalog)
    bound_codes = set(bound_credits)
    common_codes = current_codes & bound_codes  # 仅两版共有的课程拟修后才可计入要求

    # ---- 现状审核（只含已修）：决定已达成 / 必修缺口 / 各组学分缺口 ----
    initial = run_audit(
        credits=bound_credits, equiv_groups=bound_equiv_groups,
        required=required, elective_groups=elective_groups, completed=completed)

    empty_schedule = {
        "completion_semester": None,
        "total_credits": 0,
        "semesters": [],
        "message": "培养方案要求已全部达成，无需排课",
    }
    if initial["achieved"]:
        return {
            "schedule": empty_schedule,
            "projected_attribution": initial,
            "current_only_courses": [],
            "achieved": True,
        }

    # ---- 必修结构校验：同一互认单元含多项必修时一门课无法同时归属 ----
    for i, r1 in enumerate(required):
        for r2 in required[i + 1:]:
            if bunit(r1) == bunit(r2):
                raise PlanError(
                    "REQUIREMENT_CONFLICT",
                    f"必修课程 {r1} 与 {r2} 属于绑定目录的同一互认组"
                    f"（{'、'.join(sorted(bunit(r1)))}），一门课程至多归属一项，"
                    "全部必修在结构上无法同时达成")

    # ---- 未满足必修的拟修选项：互认单元内、两版共有、未修 ----
    unsatisfied = set(initial["unsatisfied_required"])
    req_options = []  # 每个未满足必修一项：[可选拟修课程（升序）]
    for r in required:
        if r not in unsatisfied:
            continue
        options = sorted(
            c for c in bunit(r)
            if c in common_codes and c not in completed)
        if not options:
            in_bound = sorted(bunit(r) & bound_codes)
            raise PlanError(
                "REQUIREMENT_CONFLICT",
                f"未满足必修 {r} 的互认单元（{'、'.join(in_bound)}）在当前目录版本中"
                "没有可拟修且计入要求的课程（仅两版目录均有的课程可计入要求），"
                "该必修无法达成")
        req_options.append(options)

    # ---- 各选修组：缺口与拟修最小子集 ----
    # 这些互认单元不可能再向选修计入：已含已修课（至多计入一门），或属必修单元
    # （已满足必修由已修课占用；未满足必修将由拟修课占用）。
    blocked_units = {bunit(c) for c in completed} | {bunit(r) for r in required}
    counter = [0]
    group_choices = []  # 每组一个 [frozenset 拟修课程, ...]
    for g in initial["elective_groups"]:
        gap = g["credit_gap"]
        if gap <= 0:
            group_choices.append([frozenset()])  # 现状已达门槛，新增课程只会更多
            continue
        cand_set = {c.strip().upper() for c in g["candidates"]}
        units = {}
        for c in common_codes - completed:
            if bunit(c) & cand_set and bunit(c) not in blocked_units:
                units.setdefault(bunit(c), []).append(c)
        pool_units = [(u, sorted(codes)) for u, codes in sorted(
            units.items(), key=lambda kv: tuple(sorted(kv[0])))]
        group_choices.append(
            _minimal_elective_subsets(pool_units, gap, g["min_credits"],
                                      bound_credits, counter))

    # ---- 枚举“必修拟修 × 各组选修拟修”组合，闭包先修/同修后排课 ----
    best = None  # (k, seq, leaf_courses, projected)
    conflicts = []
    notes = []
    sched_cache = {}
    combos = 0

    def evaluate(courses):
        """一个候选课程集合的排课与诊断；可行且审核达成时更新 best。返回状态。"""
        nonlocal best
        if courses in sched_cache:
            sched = sched_cache[courses]
            if sched is not None:
                _consider(courses, sched)
            return
        try:
            blocks = _build_blocks(current_catalog, courses)
            reqs = _block_requirements(
                current_catalog, completed, blocks, current_equiv)
        except PlanError as exc:
            conflicts.append(exc.message)
            sched_cache[courses] = None
            return
        if reqs is None:
            conflicts.append("先修条件无法满足，条件冲突")
            sched_cache[courses] = None
            return
        heavy = next((b for b in blocks if b.credits > max_credits), None)
        if heavy is not None:
            notes.append(
                f"同修课程 {'+'.join(heavy.codes)} 学分合计 {heavy.credits} "
                f"超过每学期学分上限 {max_credits}")
            sched_cache[courses] = None
            return
        seats = (seat_caps, occupied)
        sched = _schedule(
            blocks, reqs, num_semesters, start_year, start_season, max_credits,
            seats=seats)
        if sched is None:
            sched_cache[courses] = None
            _diagnose(blocks, reqs, seats)
            return
        sched_cache[courses] = sched
        _consider(courses, sched)

    def _consider(courses, sched):
        nonlocal best
        # 预计归属：合并已修与拟修；当前目录独有课程不可计入要求，不进入绑定目录审核
        merged = completed | {c for c in courses if c in bound_codes}
        projected = run_audit(
            credits=bound_credits, equiv_groups=bound_equiv_groups,
            required=required, elective_groups=elective_groups, completed=merged)
        if not projected["achieved"]:
            return
        k, seq = sched
        if best is None or (k, seq) < (best[0], best[1]):
            best = (k, seq, courses, projected)

    def _diagnose(blocks, reqs, seats):
        scan_k = max(num_semesters, 2 * len(blocks) + 2)
        seatless_same = _schedule(
            blocks, reqs, num_semesters, start_year, start_season,
            max_credits) if seat_caps else None
        if seatless_same is not None:
            _, seq0 = seatless_same
            details = []
            for i, codes0 in enumerate(seq0, start=1):
                label = semester_label(start_year, start_season, i)
                for c in codes0:
                    limit = seat_caps.get((c, label))
                    if limit is not None and occupied.get((c, label), 0) >= limit:
                        details.append(
                            f"{c} 在 {label} 席位已满（上限 {limit}，"
                            f"已占用 {occupied.get((c, label), 0)}）")
            notes.append(
                f"受席位余量限制，{num_semesters} 个学期内无法排完达成要求所需课程："
                + ("；".join(details[:3]) if details else "相关课程学期席位余量不足"))
            return
        relaxed = _schedule(
            blocks, reqs, scan_k, start_year, start_season,
            sum(b.credits for b in blocks), seats=seats)
        if relaxed is None:
            seatless = _schedule(
                blocks, reqs, scan_k, start_year, start_season,
                sum(b.credits for b in blocks))
            if seatless is not None and seat_caps:
                bottlenecks = _seat_bottlenecks(
                    blocks, {}, frozenset(), start_year, start_season, scan_k,
                    seat_caps, occupied)
                notes.append(
                    "受席位余量限制，放宽学期数与学分上限仍无法排出课表："
                    + "；".join(bottlenecks[:3]))
            else:
                dead = "、".join(_deadlocked_codes(blocks, reqs))
                conflicts.append(
                    f"先修/同修条件存在循环依赖或无法满足，涉及课程：{dead}"
                    if dead else "先修/同修条件冲突，无法生成达成课表")
        else:
            notes.append(
                f"受开课学期、先修顺序、同修安排或每学期学分上限 {max_credits} 限制，"
                f"{num_semesters} 个学期内无法修完达成要求所需课程")

    for req_pick_codes in product(*req_options):
        req_pick = frozenset(req_pick_codes)
        for elective_picks in product(*group_choices):
            combos += 1
            if combos > SELECTION_CAP:
                raise PlanError("TOO_COMPLEX",
                                "达成课表的课程选择组合过多，超出复杂度限制")
            counted = req_pick | frozenset().union(*elective_picks)
            leaves, leaf_conflicts = _enumerate_sets(
                current_catalog, completed, counted, frozenset(),
                current_equiv)
            conflicts.extend(leaf_conflicts)
            # 不按完成学期提前终止：同一最早完成学期仍须比较字典序
            for courses in leaves:
                evaluate(courses)

    if best is None:
        if notes:
            raise PlanError("NO_SOLUTION", notes[0])
        raise PlanError(
            "REQUIREMENT_CONFLICT",
            conflicts[0] if conflicts else "无法生成满足全部必修及选修门槛的达成课表")

    k, seq, leaf_courses, projected = best
    semesters = []
    for i, codes in enumerate(seq, start=1):
        courses_in_sem = [
            {"code": c, "name": current_catalog[c].name,
             "credits": current_catalog[c].credits}
            for c in codes
        ]
        semesters.append({
            "semester": semester_label(start_year, start_season, i),
            "courses": courses_in_sem,
            "total_credits": sum(c["credits"] for c in courses_in_sem),
        })
    schedule = {
        "completion_semester": semester_label(start_year, start_season, k),
        "total_credits": sum(s["total_credits"] for s in semesters),
        "semesters": semesters,
    }
    current_only = sorted(c for c in leaf_courses if c not in bound_codes)
    current_only_courses = [
        {"code": c, "name": current_catalog[c].name,
         "credits": current_catalog[c].credits,
         "reason": "当前目录独有课程，仅作先修/同修等必要课程，不计入要求"}
        for c in current_only
    ]
    return {
        "schedule": schedule,
        "projected_attribution": projected,
        "current_only_courses": current_only_courses,
        "achieved": True,
    }
