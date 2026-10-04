"""不安全航次执行改期（POST /api/trips/{id}/reschedule）测试。

覆盖：事务原子性（作废原行程 + 确认新快照 + 等额冲销）、逐段出发分钟方案
比对、修订/档案失效拒绝、当前管制/狭窄水道组/泊位日历/其他有效占用复算、
幂等同键重放与异内容冲突（即使修订又变化也先重放）、拒绝不改变状态。
"""
from app.database import connect

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _plan_confirm(client, rev, draft=1.0, height=0.0, margin=0.0,
                  start=0, window=20, origin="A", destination="B",
                  extra=None, ship_no=None):
    body = {
        "origin": origin, "destination": destination,
        "start_minute": start, "window_minutes": window,
        "revision": rev,
    }
    if ship_no is not None:
        body["ship_no"] = ship_no
    else:
        body.update({"draft": draft, "height": height, "margin": margin})
    if extra:
        body.update(extra)
    r = client.post("/api/trips/plan", json=body)
    assert r.status_code == 200, r.text
    planned = r.json()
    # 按船号规划时确认体须去掉三项参数（船号与 draft/height/margin 互斥）
    confirm_body = dict(planned)
    if ship_no is not None:
        confirm_body.pop("draft", None)
        confirm_body.pop("height", None)
        confirm_body.pop("margin", None)
    r = client.post("/api/trips/confirm", json=confirm_body)
    assert r.status_code == 201, r.text
    return r.json()


def _correction(client, levels, base=None):
    body = {"corrections": {str(m): v for m, v in levels.items()}}
    if base is not None:
        body["base_revision"] = base
    r = client.post("/api/tide/corrections", json=body)
    assert r.status_code == 201, r.text
    return r.json()["revision"]


def _review_reschedule(client, trip_id, rev):
    r = client.post(f"/api/trips/{trip_id}/review",
                    json={"target_revision": rev})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["safe"] is False
    assert data["no_solution"] is False
    return data["reschedule"]


def _execute(client, trip_id, rev, departures, key="key-1"):
    return client.post(f"/api/trips/{trip_id}/reschedule", json={
        "target_revision": rev,
        "departures": departures,
        "idempotency_key": key,
    })


def _make_unsafe_single_leg(client, low_to=19):
    """单段 A->B（航时3、深4）：rev1 下 5 出发占 5,6,7；
    更正 5..7 低潮、8 起恢复 -> 不安全，改期 8 出发、11 抵达。"""
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, low_to, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    assert [l["start_minute"] for l in trip["itinerary"]] == [5]
    rev2 = _correction(client, {m: 0.0 for m in range(5, 8)})
    rs = _review_reschedule(client, trip["trip_id"], rev2)
    assert [l["start_minute"] for l in rs["itinerary"]] == [8]
    return trip, rev2, rs


# ---------------------------------------------------------------------------
# 1. 成功路径：旧行程作废 + 新快照确认 + 占用切换；无账单时 reversal=null
# ---------------------------------------------------------------------------

def test_execute_reschedule_success(client):
    trip, rev2, rs = _make_unsafe_single_leg(client)
    old_id = trip["trip_id"]

    r = _execute(client, old_id, rev2, [8])
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["replayed"] is False
    assert data["old_trip_id"] == old_id
    assert data["new_trip_id"] != old_id
    assert data["target_revision"] == rev2
    assert data["recalculated"]["arrival_minute"] == 11
    assert [l["start_minute"] for l in data["recalculated"]["itinerary"]] == [8]
    assert data["reversal"] is None
    assert data["old_void"]["trip_id"] == old_id
    assert str(new_id := data["new_trip_id"])

    # 旧行程：快照原样可查但已作废，占用失效
    old = client.get(f"/api/trips/{old_id}").json()
    assert old["voided_at"] is not None
    assert old["itinerary"] == trip["itinerary"]
    vr = client.get(f"/api/trips/{old_id}/void").json()
    assert vr["reason"] == data["old_void"]["reason"]
    assert "改期" in vr["reason"]

    # 新行程：有效快照，修订为目标修订，参数沿用原快照
    new = client.get(f"/api/trips/{data['new_trip_id']}").json()
    assert new["voided_at"] is None
    assert new["revision"] == rev2
    assert new["arrival_minute"] == 11
    assert [l["start_minute"] for l in new["itinerary"]] == [8]
    assert new["draft"] == trip["draft"]
    assert new["origin"] == trip["origin"]


def test_execute_swaps_occupancy(client):
    # 改期后原占用（5,6,7）释放、新占用（8,9,10）生效：第二艘船规划结果随之变化
    trip, rev2, _ = _make_unsafe_single_leg(client)
    assert _execute(client, trip["trip_id"], rev2, [8]).status_code == 201

    # rev2 的 5..19 全部置为高潮（消除更正造成的低潮缺口，便于纯粹观察占用）
    _correction(client, {m: 3.0 for m in range(5, 20)})
    rev3 = client.get("/api/tide/revisions").json()[-1]["revision"]

    plan = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 6.0, "height": 0.0, "margin": 0.5, "revision": rev3,
    }).json()
    # 旧占用 5..7 已随旧行程作废释放；新快照占 8..10 -> 第二艘最早 5 进入
    assert plan["itinerary"][0]["start_minute"] == 5


# ---------------------------------------------------------------------------
# 2. 幂等：同键同内容重放首次结果（即使修订此后又变化）；异内容冲突
# ---------------------------------------------------------------------------

def test_execute_idempotent_replay_even_after_new_revision(client):
    trip, rev2, _ = _make_unsafe_single_leg(client)
    old_id = trip["trip_id"]

    r1 = _execute(client, old_id, rev2, [8], key="k-1")
    assert r1.status_code == 201
    first_new_id = r1.json()["new_trip_id"]

    # 此后又产生更新的修订（rev3 全程低潮、本会无解）；同键同内容仍重放
    _correction(client, {m: 0.0 for m in range(0, 20)})
    r2 = _execute(client, old_id, rev2, [8], key="k-1")
    assert r2.status_code == 200, r2.text
    data = r2.json()
    assert data["replayed"] is True
    assert data["new_trip_id"] == first_new_id
    assert data["old_trip_id"] == old_id

    # 重放不产生第二条新快照
    trips = client.get("/api/trips").json()
    assert len(trips) == 2


def test_execute_same_key_different_content_conflict(client):
    trip, rev2, _ = _make_unsafe_single_leg(client)
    assert _execute(client, trip["trip_id"], rev2, [8], key="k-2").status_code == 201

    # 同键不同出发分钟 -> 冲突
    r = _execute(client, trip["trip_id"], rev2, [9], key="k-2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESCHEDULE_KEY_CONFLICT"

    # 已作废的原行程同键再用也仍是冲突（重放优先于作废校验，但内容不同）
    r = _execute(client, trip["trip_id"], rev2, [8], key="other-key")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TRIP_ALREADY_VOIDED"


def test_execute_replays_when_trip_already_voided_same_content(client):
    trip, rev2, _ = _make_unsafe_single_leg(client)
    _execute(client, trip["trip_id"], rev2, [8], key="k-3")
    # 即使原行程已作废，同键同内容仍返回首次结果
    r = _execute(client, trip["trip_id"], rev2, [8], key="k-3")
    assert r.status_code == 200 and r.json()["replayed"] is True


# ---------------------------------------------------------------------------
# 3. 拒绝口径
# ---------------------------------------------------------------------------

def test_execute_unknown_trip(client):
    r = _execute(client, 9999, 1, [0])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TRIP_NOT_FOUND"


def test_execute_unknown_revision(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1)
    r = _execute(client, trip["trip_id"], 4242, [0])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TIDE_REVISION_NOT_FOUND"


def test_execute_stale_revision(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1)
    # 无影响更正产生 rev2；用旧修订号 rev1 执行 -> REVISION_STALE
    rev2 = _correction(client, {15: 3.0})
    r = _execute(client, trip["trip_id"], rev1, [0])
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "REVISION_STALE"
    assert err["details"]["current_revision"] == rev2
    # 状态不变
    g = client.get(f"/api/trips/{trip['trip_id']}").json()
    assert g["voided_at"] is None


def test_execute_rejects_safe_trip(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1)
    # 无影响更正
    rev2 = _correction(client, {15: 3.0})
    r = _execute(client, trip["trip_id"], rev2, [0])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TRIP_SAFE"
    assert client.get(f"/api/trips/{trip['trip_id']}").json()["voided_at"] is None


def test_execute_rejects_when_no_solution(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    # 全窗口低潮 -> 无改期解
    rev2 = _correction(client, {m: 0.0 for m in range(0, 20)})
    r = _execute(client, trip["trip_id"], rev2, [8])
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "PLAN_MISMATCH"
    assert err["details"]["reason"] == "no_feasible_itinerary"
    # 状态不变
    assert client.get(f"/api/trips/{trip['trip_id']}").json()["voided_at"] is None
    assert len(client.get("/api/trips").json()) == 1


def test_execute_rejects_departure_mismatch(client):
    trip, rev2, _ = _make_unsafe_single_leg(client)
    # 提交错误的出发分钟（复算最优为 8）
    r = _execute(client, trip["trip_id"], rev2, [9])
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "PLAN_MISMATCH"
    assert err["details"]["reason"] == "itinerary_mismatch"
    assert err["details"]["submitted_departures"] == [9]
    assert err["details"]["recalculated_departures"] == [8]
    # 拒绝不改变状态
    g = client.get(f"/api/trips/{trip['trip_id']}").json()
    assert g["voided_at"] is None
    assert len(client.get("/api/trips").json()) == 1


def test_execute_rejects_departure_count_mismatch(client):
    for n in ["A", "B", "C"]:
        add_node(client, n)
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    add_segment(client, "B", "C", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5, destination="C")
    # 更正使两段原占用（0,1 与 2,3）全部低潮，4 起恢复：
    # 改期最优为 s1 4 出发、s2 6 出发（各占连续 2 分钟）
    rev2 = _correction(client, {m: 0.0 for m in range(0, 4)})
    rs = _review_reschedule(client, trip["trip_id"], rev2)
    assert len(rs["itinerary"]) == 2
    assert [l["start_minute"] for l in rs["itinerary"]] == [4, 6]
    # 只回填一段
    r = _execute(client, trip["trip_id"], rev2,
                 [rs["itinerary"][0]["start_minute"]])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"
    assert client.get(f"/api/trips/{trip['trip_id']}").json()["voided_at"] is None


def test_execute_already_voided_trip_rejected(client):
    trip, rev2, _ = _make_unsafe_single_leg(client)
    client.post(f"/api/trips/{trip['trip_id']}/void",
                json={"reason": "调度员手工作废"})
    r = _execute(client, trip["trip_id"], rev2, [8], key="fresh-key")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TRIP_ALREADY_VOIDED"


# ---------------------------------------------------------------------------
# 4. 入参校验
# ---------------------------------------------------------------------------

def test_execute_validation_errors(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1)

    # 缺幂等键 / 空键
    r = client.post(f"/api/trips/{trip['trip_id']}/reschedule",
                    json={"target_revision": rev1, "departures": [0]})
    assert r.status_code == 422
    r = client.post(f"/api/trips/{trip['trip_id']}/reschedule",
                    json={"target_revision": rev1, "departures": [0],
                          "idempotency_key": ""})
    assert r.status_code == 422
    # 负出发分钟
    r = client.post(f"/api/trips/{trip['trip_id']}/reschedule",
                    json={"target_revision": rev1, "departures": [-1],
                          "idempotency_key": "k"})
    assert r.status_code == 422
    # 幂等键过长
    r = client.post(f"/api/trips/{trip['trip_id']}/reschedule",
                    json={"target_revision": rev1, "departures": [0],
                          "idempotency_key": "x" * 129})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 5. 原账单等额冲销一次；新行程待另行开单
# ---------------------------------------------------------------------------

def test_execute_reverses_existing_bill_once(client):
    trip, rev2, _ = _make_unsafe_single_leg(client)
    old_id = trip["trip_id"]

    # 原行程开单（先确保存在费率版本；seg1 是本测试库第一条航段）
    seg1 = client.get("/api/segments").json()[0]["seg_id"]
    client.put(f"/api/segments/{seg1}/toll-rates", json={
        "rates": [{"start_minute": 0, "end_minute": 1440,
                   "rate_cents_per_minute": 10}],
    })
    b = client.post(f"/api/trips/{old_id}/bill")
    assert b.status_code == 201
    bill = b.json()
    # 原航时 3 分钟、进入分钟 5 -> 30 分
    assert bill["total_cents"] == 30

    r = _execute(client, old_id, rev2, [8], key="bill-key")
    assert r.status_code == 201, r.text
    data = r.json()
    rev = data["reversal"]
    assert rev is not None
    assert rev["trip_id"] == old_id
    assert rev["total_cents"] == -30
    assert rev["segments"][0]["amount_cents"] == -30
    assert rev["version"] == bill["version"]

    # 账单查询内嵌同一张冲销单
    bo = client.get(f"/api/trips/{old_id}/bill").json()
    assert bo["reversal"]["total_cents"] == -30
    assert bo["reversal"]["reversed_at"] == rev["reversed_at"]
    rv = client.get(f"/api/trips/{old_id}/reversal").json()
    assert rv["total_cents"] == -30

    # 新行程不自动开单（待另行开单）
    nb = client.get(f"/api/trips/{data['new_trip_id']}/bill")
    assert nb.status_code == 404
    assert nb.json()["error"]["code"] == "BILL_NOT_FOUND"
    # 新行程可以另行开单（按当时最新费率版本）
    nb2 = client.post(f"/api/trips/{data['new_trip_id']}/bill")
    assert nb2.status_code == 201
    # 新快照进入分钟 8、固化航时 3
    assert nb2.json()["total_cents"] == 30

    # 同键重放：冲销状态原样返回，不重复冲销（库里仍只有一张冲销单）
    conn = connect()
    cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM toll_reversals WHERE bill_trip_id = ?",
        (old_id,),
    ).fetchone()["c"]
    conn.close()
    assert cnt == 1
    rp = _execute(client, old_id, rev2, [8], key="bill-key")
    assert rp.status_code == 200
    assert rp.json()["reversal"]["total_cents"] == -30


# ---------------------------------------------------------------------------
# 6. 关联船舶档案：停用 / 版本已变拒绝（不改变状态）；档案未变则成功并沿用引用
# ---------------------------------------------------------------------------

def test_execute_ship_disabled_rejected(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    client.post("/api/ships", json={
        "ship_no": "S1", "draft": 6.0, "height": 0.0, "margin": 0.5,
    })
    trip = _plan_confirm(client, rev1, ship_no="S1")
    assert trip["ship_version"] == 1
    rev2 = _correction(client, {m: 0.0 for m in range(5, 8)})

    client.put("/api/ships/S1", json={"enabled": False})
    r = _execute(client, trip["trip_id"], rev2, [8], key="ship-key")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SHIP_DISABLED"
    assert client.get(f"/api/trips/{trip['trip_id']}").json()["voided_at"] is None


def test_execute_ship_version_stale_rejected(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    client.post("/api/ships", json={
        "ship_no": "S2", "draft": 6.0, "height": 0.0, "margin": 0.5,
    })
    trip = _plan_confirm(client, rev1, ship_no="S2")
    rev2 = _correction(client, {m: 0.0 for m in range(5, 8)})

    # 有效变更（吃水变化）-> 版本 2；执行改期须版本未变
    client.put("/api/ships/S2", json={"draft": 6.2})
    r = _execute(client, trip["trip_id"], rev2, [8], key="ship-key2")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "SHIP_VERSION_STALE"
    assert err["details"]["provided_version"] == 1
    assert err["details"]["current_version"] == 2
    assert client.get(f"/api/trips/{trip['trip_id']}").json()["voided_at"] is None


def test_execute_keeps_ship_reference_on_success(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    client.post("/api/ships", json={
        "ship_no": "S3", "draft": 6.0, "height": 0.0, "margin": 0.5,
    })
    trip = _plan_confirm(client, rev1, ship_no="S3")
    rev2 = _correction(client, {m: 0.0 for m in range(5, 8)})

    r = _execute(client, trip["trip_id"], rev2, [8], key="ship-key3")
    assert r.status_code == 201, r.text
    new_id = r.json()["new_trip_id"]
    new = client.get(f"/api/trips/{new_id}").json()
    # 新快照沿用档案引用与快照参数（即使档案后来变化也按快照口径）
    assert new["ship_no"] == "S3"
    assert new["ship_version"] == 1
    assert new["draft"] == 6.0


# ---------------------------------------------------------------------------
# 7. 当前航时管制参与复算
# ---------------------------------------------------------------------------

def test_execute_uses_current_travel_controls(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    rev2 = _correction(client, {m: 0.0 for m in range(5, 8)})

    # 改期执行前设置管制：8 之后进入航时变为 4 -> 建议为 8 出发、12 抵达
    client.put("/api/segments/1/travel-controls", json={
        "controls": [{"start_minute": 0, "end_minute": 8, "travel_time": 3},
                     {"start_minute": 8, "end_minute": 1440, "travel_time": 4}],
    })
    rs = _review_reschedule(client, trip["trip_id"], rev2)
    leg = rs["itinerary"][0]
    assert (leg["start_minute"], leg["end_minute"], leg["travel_time"]) == (8, 12, 4)

    r = _execute(client, trip["trip_id"], rev2,
                 [leg["start_minute"]], key="ctl-key")
    assert r.status_code == 201, r.text
    new = client.get(f"/api/trips/{r.json()['new_trip_id']}").json()
    assert new["arrival_minute"] == 12
    assert new["itinerary"][0]["travel_time"] == 4


def test_execute_control_pushes_departure(client):
    # 管制令 8 起进入的航程越窗，任何出发都无法容纳 -> 无解（执行拒绝）。
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    # 窗口 20：管制 [8,20) 航时 13 -> 8+13>20 越窗；8 之前低潮不可行
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    rev2 = _correction(client, {m: 0.0 for m in range(5, 8)})
    # 无管制时建议 8 出发
    rs = _review_reschedule(client, trip["trip_id"], rev2)
    assert [l["start_minute"] for l in rs["itinerary"]] == [8]
    # 执行前管制变化：8 进入航时 13 越窗 -> review 无解
    client.put("/api/segments/1/travel-controls", json={
        "controls": [{"start_minute": 8, "end_minute": 20, "travel_time": 13}],
    })
    review = client.post(f"/api/trips/{trip['trip_id']}/review",
                         json={"target_revision": rev2}).json()
    assert review["no_solution"] is True
    r = _execute(client, trip["trip_id"], rev2, [8], key="ctl-push")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "PLAN_MISMATCH"
    assert err["details"]["reason"] == "no_feasible_itinerary"
    assert client.get(f"/api/trips/{trip['trip_id']}").json()["voided_at"] is None


# ---------------------------------------------------------------------------
# 8. 狭窄水道组：执行复算按当前组关系；他船组占用挡住则无解
# ---------------------------------------------------------------------------

def test_execute_respects_channel_groups(client):
    for n in ["A", "B", "C"]:
        add_node(client, n)
    s1 = add_segment(client, "A", "B", travel_time=3, depth=4.0)
    s2 = add_segment(client, "B", "C", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 29, 3.0))
    # 原行程：A->C，吃水 6.0 + 余量 0.5（需潮位 >=2.5），0 出发、3 出发，6 抵达
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5,
                         destination="C", window=30)
    assert [(l["seg_id"], l["start_minute"]) for l in trip["itinerary"]] == \
        [(s1, 0), (s2, 3)]

    # 更正使原行程不安全：0..7 低潮；8 之后恢复
    rev2 = _correction(client, {m: 0.0 for m in range(0, 8)})

    # 组建 {1,2}：无他船时改期成功（自身连续两段的组展开不误伤自身）
    client.post("/api/channel-groups", json={"segments": [s1, s2]})
    rs = _review_reschedule(client, trip["trip_id"], rev2)
    deps = [l["start_minute"] for l in rs["itinerary"]]
    assert deps == [8, 11]
    r = _execute(client, trip["trip_id"], rev2, deps, key="grp-ok")
    assert r.status_code == 201, r.text
    new = client.get(f"/api/trips/{r.json()['new_trip_id']}").json()
    # s1 最早 8 进入（占 8,9,10），s2 11 进入
    assert [(l["seg_id"], l["start_minute"]) for l in new["itinerary"]] == \
        [(s1, 8), (s2, 11)]


def test_execute_group_occupancy_blocks_reschedule(client):
    for n in ["A", "B", "C"]:
        add_node(client, n)
    s1 = add_segment(client, "A", "B", travel_time=3, depth=4.0)
    s2 = add_segment(client, "B", "C", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 49, 3.0))
    # 原行程 A->C（大吃水，低潮即违规）：s1 0 出发、s2 3 出发，6 抵达
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5,
                         destination="C", window=50)
    # 更正使原行程不安全：0..14 低潮，15 起恢复
    rev2 = _correction(client, {m: 0.0 for m in range(0, 15)})
    client.post("/api/channel-groups", json={"segments": [s1, s2]})

    # 无他船时建议 s1 15 出发、s2 18 出发
    review = client.post(f"/api/trips/{trip['trip_id']}/review",
                         json={"target_revision": rev2}).json()
    assert review["safe"] is False and review["no_solution"] is False
    deps = [l["start_minute"] for l in review["reschedule"]["itinerary"]]
    assert deps == [15, 18]

    # 原行程自身占 s1 0..2、s2 3..5，经组展开已封锁整组 0..5。此后每艘
    # 小吃水他船（低潮也可通行）A->B 占 3 分钟并继续展开封锁整组：
    # 第 j 艘于 6+3j 进入；14 艘连续占住 6..47 后，48 进入会越窗（48+3>50），
    # 窗口 [0,50) 被全部封死 -> 大吃水改期无解
    n_others = 14
    for j in range(n_others):
        pr = client.post("/api/trips/plan", json={
            "origin": "A", "destination": "B", "start_minute": 0,
            "window_minutes": 50, "draft": 1.0, "height": 0.0, "margin": 0.0,
            "revision": rev2,
        })
        assert pr.status_code == 200, (j, pr.text)
        planned = pr.json()
        assert planned["itinerary"][0]["start_minute"] == 6 + 3 * j
        cr = client.post("/api/trips/confirm", json=planned)
        assert cr.status_code == 201, (j, cr.text)
    # 再增一艘已无可行分钟（6..47 被占、48 进入越窗）
    pr = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 50, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev2,
    })
    assert pr.status_code == 404

    # 大吃水原行程即便只走 s1 也无空间（s1+s2 更不可能）-> 改期无解
    review2 = client.post(f"/api/trips/{trip['trip_id']}/review",
                          json={"target_revision": rev2}).json()
    assert review2["no_solution"] is True

    r = _execute(client, trip["trip_id"], rev2, deps, key="grp-block")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "PLAN_MISMATCH"
    assert err["details"]["reason"] == "no_feasible_itinerary"
    # 拒绝不改变状态：原行程仍有效、没有新增快照
    assert client.get(f"/api/trips/{trip['trip_id']}").json()["voided_at"] is None
    assert len(client.get("/api/trips").json()) == 1 + n_others  # 原行程 + 他船


# ---------------------------------------------------------------------------
# 9. 泊位：新快照沿用原泊位与作业分钟，按当前日历/占用复算作业区间
# ---------------------------------------------------------------------------

def test_execute_with_berth(client):
    for n in ["A", "B"]:
        add_node(client, n)
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    client.post("/api/nodes/B/berths", json={"berth_id": "B1"})
    client.put("/api/berths/B1/calendar", json={
        "intervals": [{"start_minute": 0, "end_minute": 30}],
    })
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 29, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5,
                         extra={"berth_id": "B1", "work_minutes": 2})
    assert trip["work_start_minute"] == 8 and trip["work_end_minute"] == 10
    rev2 = _correction(client, {m: 0.0 for m in range(5, 8)})

    rs = _review_reschedule(client, trip["trip_id"], rev2)
    # 改期 8 出发、11 抵达；作业 [11,13) 仍完整落在开放区间 [0,30)
    assert rs["berth_id"] == "B1"
    assert (rs["work_start_minute"], rs["work_end_minute"]) == (11, 13)

    r = _execute(client, trip["trip_id"], rev2,
                 [l["start_minute"] for l in rs["itinerary"]], key="berth-ok")
    assert r.status_code == 201, r.text
    new = client.get(f"/api/trips/{r.json()['new_trip_id']}").json()
    assert new["berth_id"] == "B1"
    assert new["work_minutes"] == 2
    assert (new["work_start_minute"], new["work_end_minute"]) == (11, 13)
    # 旧泊位作业占用随旧行程作废释放（新快照占用 11,12）


def test_execute_berth_calendar_tightened_no_solution(client):
    for n in ["A", "B"]:
        add_node(client, n)
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    client.post("/api/nodes/B/berths", json={"berth_id": "B9"})
    client.put("/api/berths/B9/calendar", json={
        "intervals": [{"start_minute": 0, "end_minute": 30}],
    })
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 29, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5,
                         extra={"berth_id": "B9", "work_minutes": 2})
    rev2 = _correction(client, {m: 0.0 for m in range(5, 8)})
    rs = _review_reschedule(client, trip["trip_id"], rev2)
    deps = [l["start_minute"] for l in rs["itinerary"]]

    # 执行前收紧日历：只开放到 11，改期抵达 11 时作业 [11,13) 无法容纳 -> 无解
    client.put("/api/berths/B9/calendar", json={
        "intervals": [{"start_minute": 0, "end_minute": 11}],
    })
    r = _execute(client, trip["trip_id"], rev2, deps, key="berth-tight")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "PLAN_MISMATCH"
    assert err["details"]["reason"] == "no_feasible_itinerary"
    # 不改变状态
    g = client.get(f"/api/trips/{trip['trip_id']}").json()
    assert g["voided_at"] is None
    assert (g["work_start_minute"], g["work_end_minute"]) == (8, 10)
