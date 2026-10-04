"""培养方案达成课表：HTTP API 集成测试（独立临时数据库）。"""
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    from app.main import app
    with TestClient(app) as c:
        yield c


def _equiv_catalog(client, groups=(["CS301", "CS302"], ["CS401", "CS402"])):
    current = client.get("/catalog/current").json()
    r = client.post("/catalog/versions", json={
        "note": "互认目录", "courses": current["courses"], "equivalence_groups": list(groups)})
    assert r.status_code == 201, r.text
    return r.json()["version_id"]


def _register(client, program, vid, required=None, electives=None):
    body = {
        "program_code": program, "catalog_version_id": vid,
        "required_courses": required if required is not None else
        ["CS101", "CS201", "CS301", "CS401"],
        "elective_groups": electives if electives is not None else [
            {"name": "专业选修", "candidates": ["CS303", "CS402"], "min_credits": 3},
            {"name": "基础拓展", "candidates": ["MATH101", "MATH102", "MATH103"], "min_credits": 6},
        ],
    }
    r = client.post(f"/programs/{program}/requirement-versions", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _payload(**kw):
    body = {
        "completed_courses": ["CS101", "CS101L", "CS201", "MATH103"],
        "start_semester": "2026-SPRING",
        "max_credits_per_semester": 12,
        "num_semesters": 6,
    }
    body.update(kw)
    return body


# ---------------------------------------------------------------- 基本流程

def test_completion_plan_created_and_persisted(client):
    _register(client, "CS", 1)
    r = client.post("/requirement-versions/CS:v1/completion-plans", json=_payload())
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["status"] == "proposed"
    assert d["plan_kind"] == "completion"
    assert d["catalog_version"] == 1
    assert d["bound_catalog_version"] == 1
    assert d["requirement_version_id"] == "CS:v1"
    sems = [[c["code"] for c in s["courses"]] for s in d["schedule"]["semesters"]]
    assert sems == [["CS301", "CS303"], ["CS401", "MATH101"]]
    assert d["schedule"]["completion_semester"] == "2026-FALL"
    assert d["projected_attribution"]["achieved"] is True
    assert d["projected_attribution"]["unsatisfied_required"] == []
    # 回显 request 不含拟修课被当作已修
    assert d["request"]["completed_courses"] == ["CS101", "CS101L", "CS201", "MATH103"]
    # 可回看
    got = client.get(f"/plans/{d['plan_id']}").json()
    assert got["plan_kind"] == "completion"
    assert got["projected_attribution"]["achieved"] is True
    # 列表带标记
    listed = [p for p in client.get("/plans").json() if p["plan_id"] == d["plan_id"]][0]
    assert listed["plan_kind"] == "completion"
    assert listed["requirement_version_id"] == "CS:v1"
    assert listed["completion_semester"] == "2026-FALL"
    # 待确认不占席位
    seat = client.get("/seat-caps/CS401/2026-FALL").json()
    assert seat["occupied"] == 0


def test_already_achieved_returns_empty_schedule(client):
    _register(client, "CS", 1)
    done = ["CS101", "CS201", "CS301", "CS401", "CS303", "MATH101", "MATH103"]
    r = client.post("/requirement-versions/CS:v1/completion-plans",
                    json=_payload(completed_courses=done))
    assert r.status_code == 201
    d = r.json()
    assert d["schedule"]["semesters"] == []
    assert d["schedule"]["completion_semester"] is None
    assert d["achieved"] is True and d["projected_attribution"]["achieved"] is True
    # 同样保存待确认记录，但无任何占位
    assert client.post(f"/plans/{d['plan_id']}/confirm").json()["status"] == "confirmed"


# ---------------------------------------------------------------- 校验与错误

def test_unknown_requirement_version_404(client):
    r = client.post("/requirement-versions/NOPE:v1/completion-plans", json=_payload())
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NOT_FOUND"


def test_unknown_completed_course_422_and_not_saved(client):
    _register(client, "CS", 1)
    r = client.post("/requirement-versions/CS:v1/completion-plans",
                    json=_payload(completed_courses=["UNKNOWN_COURSE"]))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "COURSE_MISSING"
    assert client.get("/plans").json() == []


def test_invalid_inputs_rejected(client):
    _register(client, "CS", 1)
    assert client.post("/requirement-versions/CS:v1/completion-plans",
                       json=_payload(max_credits_per_semester=0)).status_code == 422
    assert client.post("/requirement-versions/CS:v1/completion-plans",
                       json=_payload(num_semesters=0)).status_code == 422
    r = client.post("/requirement-versions/CS:v1/completion-plans",
                    json=_payload(start_semester="2026春"))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_INPUT"


def test_no_solution_within_horizon_422_and_not_saved(client):
    _register(client, "CS", 1)
    r = client.post("/requirement-versions/CS:v1/completion-plans",
                    json=_payload(num_semesters=1))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "NO_SOLUTION"
    assert client.get("/plans").json() == []


# ---------------------------------------------------------------- 双目录语义

def test_bound_catalog_credits_and_groups_used(client):
    vid = _equiv_catalog(client)
    _register(client, "CS", vid)
    # 已修 CS302（绑定互认满足 CS301）；拟修 CS402（春，互认满足 CS401）+ CS303（选修）
    done = ["CS101", "CS101L", "CS201", "MATH101", "MATH102", "MATH103", "CS302"]
    r = client.post("/requirement-versions/CS:v1/completion-plans",
                    json=_payload(completed_courses=done))
    d = r.json()
    assert r.status_code == 201, r.text
    pa = d["projected_attribution"]
    assert pa["achieved"]
    r301 = next(x for x in pa["required"] if x["code"] == "CS301")
    r401 = next(x for x in pa["required"] if x["code"] == "CS401")
    assert r301["attributed_course"] == {"code": "CS302", "credits": 3, "via": "equivalence"}
    assert r401["attributed_course"]["code"] == "CS402"
    assert r401["attributed_course"]["via"] == "equivalence"
    # CS402 已归属必修：专业选修由 CS303 计入（一门只归属一项）
    elective = pa["elective_groups"][0]
    assert [c["code"] for c in elective["attributed_courses"]] == ["CS303"]


def test_current_only_course_listed_but_not_counted(client):
    # 当前目录 v2 新增 NEW299（两版共有判定失败），可作 CS301 先修组选项
    current = client.get("/catalog/current").json()["courses"]
    cs301 = next(x for x in current if x["code"] == "CS301")
    cs301["prereq_groups"] = [["CS201", "CS202", "NEW299"], ["MATH103"]]
    current.append({"code": "NEW299", "name": "前沿导论", "credits": 2,
                    "semesters": ["SPRING"], "prereq_groups": [], "coreqs": []})
    assert client.post("/catalog/versions",
                       json={"note": "v2", "courses": current}).status_code == 201
    _register(client, "Q", 1, required=["CS301"], electives=[])
    r = client.post("/requirement-versions/Q:v1/completion-plans",
                    json=_payload(completed_courses=["MATH103"]))
    assert r.status_code == 201, r.text
    d = r.json()
    assert [x["code"] for x in d["current_only_courses"]] == ["NEW299"]
    pa = d["projected_attribution"]
    assert pa["achieved"]
    # NEW299 不在绑定目录 -> 不出现在任何归属中
    attributed = {x["attributed_course"]["code"] for x in pa["required"] if x["satisfied"]}
    attributed |= {c["code"] for g in pa["elective_groups"]
                   for c in g["attributed_courses"]}
    assert "NEW299" not in attributed


def test_completed_course_need_not_exist_in_current_catalog(client):
    # 当前目录删除 CS303；已修 CS303（绑定目录 v1 有）仍可计入选修
    current = [c for c in client.get("/catalog/current").json()["courses"]
               if c["code"] != "CS303"]
    assert client.post("/catalog/versions",
                       json={"note": "v2", "courses": current}).status_code == 201
    _register(client, "CS", 1)
    done = ["CS101", "CS101L", "CS201", "MATH101", "MATH102", "MATH103",
            "CS301", "CS401", "CS303"]
    r = client.post("/requirement-versions/CS:v1/completion-plans",
                    json=_payload(completed_courses=done))
    assert r.status_code == 201, r.text
    pa = r.json()["projected_attribution"]
    assert pa["achieved"]


# ---------------------------------------------------------------- 席位与确认

def test_plan_respects_seats_and_confirm_is_atomic(client):
    vid = _equiv_catalog(client)
    _register(client, "CS", vid)
    done = ["CS101", "CS101L", "CS201", "MATH101", "MATH102", "MATH103", "CS302"]
    # 封掉 CS402@2026-SPRING（互认满足 CS401 的最早学期）-> 顺延秋季 CS401
    assert client.put("/seat-caps/CS402/2026-SPRING", json={"cap": 0}).status_code == 200
    r = client.post("/requirement-versions/CS:v1/completion-plans",
                    json=_payload(completed_courses=done))
    assert r.status_code == 201
    d = r.json()
    assert d["schedule"]["completion_semester"] == "2026-FALL"
    pid = d["plan_id"]
    # 再封 CS401@2026-FALL：确认时原子复核失败
    assert client.put("/seat-caps/CS401/2026-FALL", json={"cap": 0}).status_code == 200
    rc = client.post(f"/plans/{pid}/confirm")
    assert rc.status_code == 409
    assert rc.json()["detail"]["code"] == "SEAT_CONFLICT"
    assert client.get(f"/plans/{pid}").json()["status"] == "proposed"
    # 放开 -> 确认成功并实际占位
    assert client.put("/seat-caps/CS401/2026-FALL", json={"cap": 5}).status_code == 200
    ok = client.post(f"/plans/{pid}/confirm")
    assert ok.status_code == 200 and ok.json()["status"] == "confirmed"
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 1
    # 幂等
    assert client.post(f"/plans/{pid}/confirm").json()["status"] == "confirmed"
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 1


def test_confirm_rejected_after_catalog_revision(client):
    _register(client, "CS", 1)
    d = client.post("/requirement-versions/CS:v1/completion-plans",
                    json=_payload()).json()
    client.post("/catalog/versions",
                json={"note": "rev", "courses": client.get("/catalog/current").json()["courses"]})
    rc = client.post(f"/plans/{d['plan_id']}/confirm")
    assert rc.status_code == 409
    assert rc.json()["detail"]["code"] == "CATALOG_CHANGED"


def test_replan_not_supported_for_completion_plan(client):
    _register(client, "CS", 1)
    d = client.post("/requirement-versions/CS:v1/completion-plans",
                    json=_payload()).json()
    client.post(f"/plans/{d['plan_id']}/confirm")
    r = client.post(f"/plans/{d['plan_id']}/replan",
                    json={"elapsed_semesters": 1,
                          "completed_courses": ["CS101", "CS101L", "CS201", "MATH103"]})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_INPUT"


def test_classic_plans_still_listed_alongside(client):
    # 既有 /plans 与达成课表共存，互不影响
    classic = client.post("/plans", json={
        "completed_courses": [], "target_courses": ["CS303"],
        "start_semester": "2026-SPRING", "max_credits_per_semester": 12,
        "num_semesters": 4}).json()
    _register(client, "CS", 1)
    completion = client.post("/requirement-versions/CS:v1/completion-plans",
                             json=_payload()).json()
    plans = {p["plan_id"]: p for p in client.get("/plans").json()}
    assert "plan_kind" not in plans[classic["plan_id"]]
    assert plans[completion["plan_id"]]["plan_kind"] == "completion"
