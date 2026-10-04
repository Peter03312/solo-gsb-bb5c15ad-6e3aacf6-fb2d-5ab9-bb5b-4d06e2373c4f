"""不安全航次的执行改期（在只读复核建议之上落库生效）。

调度员凭**已确认行程号、目标潮位修订号、复核建议中的逐段出发分钟和非空
幂等键**，把在目标修订下**不安全**的原航次改期为新的确认快照。整个过程
在单个 ``BEGIN IMMEDIATE`` 事务内完成，全部通过才提交，任一拒绝均回滚、
不改变任何状态。

事务内校验与复算顺序：

0. 幂等键命中：同内容直接返回首次结果（``replayed=true``，**优先于一切**，
   即使此后修订又变化也先重放）；异内容 -> ``RESCHEDULE_KEY_CONFLICT``；
1. 未知行程 -> ``TRIP_NOT_FOUND``；原行程已作废 -> ``TRIP_ALREADY_VOIDED``；
2. 快照关联船舶档案时，档案须**仍启用且版本未变**（停用 ``SHIP_DISABLED``、
   版本已变 ``SHIP_VERSION_STALE``；直接提交参数确认的快照无档案引用，跳过）；
3. 目标修订不存在 -> ``TIDE_REVISION_NOT_FOUND``；目标修订非最新 ->
   ``REVISION_STALE``；
4. 复用只读复核机器（``reschedule.review_and_reschedule``）按**当前航时
   管制、狭窄水道组、泊位日历与其他有效占用（排除原行程）**复算：
   窗口缺潮位 ``MISSING_TIDE_DATA``；原快照在目标修订下仍安全 ->
   ``TRIP_SAFE``（无需改期）；窗口内无解 -> ``PLAN_MISMATCH``；
5. 提交的逐段出发分钟与复算最优建议逐段不一致（段数不同或任一段不同）->
   ``PLAN_MISMATCH``（``reason=itinerary_mismatch``，details 附复算建议）。

全部通过后在**同一事务**内：

* 按复算行程写入**新不可变快照** ``confirmed_trips``（修订为目标修订，
  船舶参数、起终点、起始分钟/窗口、档案引用与泊位作业信息沿用原快照口径，
  各段航时/占用按当前管制与复算结果固化）；
* 作废原行程（写入 ``voided_trips``：原占用——含狭窄水道组展开与泊位作业
  占用——即时失效，新占用随新快照生效）；
* 原账单若存在，**等额冲销一次**（复用作废事务内冲销：逐段金额与总额取
  账单相反数、版本沿用账单版本）；未开单则不产生冲销。新行程**不自动
  开单**，待另行调用 ``POST /api/trips/{新行程号}/bill``；
* 登记改期幂等记录后提交。

返回新旧行程号、复算行程（与只读复核的 ``reschedule`` 同形）与冲销状态。
原快照、批次幂等记录及现有接口契约均不变。
"""
import hashlib
import json
import sqlite3
from typing import Optional

from ..errors import ApiError
from .reschedule import review_and_reschedule
from .ships import resolve_for_confirm
from .toll import bill_exists, create_reversal_in_void, get_reversal
from .voids import get_void_record


# 作废原行程时写入 voided_trips 的系统原因（原作废接口仍由调度员给出原因，
# 二者不冲突；改期是系统执行的原子作废+再确认）。
def _void_reason(old_trip_id: int, new_trip_id: int) -> str:
    return (f"执行不安全航次改期：原行程 #{old_trip_id} 在目标潮位修订下不安全，"
            f"已由改期新行程 #{new_trip_id} 替代，原占用失效")


def _content_hash(trip_id: int, target_revision: int,
                  departures: list[int]) -> str:
    """提交内容（幂等键以外的全部判别字段）的规范化 SHA-256。"""
    payload = {
        "trip_id": trip_id,
        "target_revision": target_revision,
        "departures": list(departures),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                     sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _reversal_status(conn, trip_id: int) -> Optional[dict]:
    """冲销状态：原账单存在时返回等额冲销单视图（金额为账单相反数），
    原行程未开单时为 None。冲销单行在本事务内刚插入，故 get_reversal 必能读到。"""
    if not bill_exists(conn, trip_id):
        return None
    return get_reversal(conn, trip_id)


def execute_reschedule(conn, trip_id: int, target_revision: int,
                       departures: list[int], idempotency_key: str
                       ) -> tuple[dict, bool]:
    """事务内执行不安全航次改期。返回 (响应体, replayed)。

    首次成功 -> (响应, False)（HTTP 201）；同键同内容重试 ->
    (首次响应, True)（HTTP 200）；其余情况抛 ApiError 并整体回滚。
    """
    digest = _content_hash(trip_id, target_revision, departures)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 0) 幂等重放优先于一切（未知行程/修订过期等均不影响重放，即使
        #    此后修订又变化，也先返回首次结果）
        existing = conn.execute(
            "SELECT trip_id, content_hash, result FROM reschedule_records"
            " WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != digest or existing["trip_id"] != trip_id:
                raise ApiError(
                    409, "RESCHEDULE_KEY_CONFLICT",
                    "同一改期幂等键已用于不同的提交内容（不同行程号/目标修订号/"
                    "逐段出发分钟），拒绝执行",
                    {"idempotency_key": idempotency_key,
                     "existing_trip_id": existing["trip_id"]},
                )
            conn.execute("COMMIT")
            replay = json.loads(existing["result"])
            replay["replayed"] = True
            return replay, True

        # 1) 原行程必须存在且仍有效（未作废）
        snap_row = conn.execute(
            "SELECT * FROM confirmed_trips WHERE trip_id = ?", (trip_id,)
        ).fetchone()
        if snap_row is None:
            raise ApiError(404, "TRIP_NOT_FOUND",
                           f"已确认行程不存在: {trip_id}", {"trip_id": trip_id})
        snap = dict(snap_row)
        if conn.execute(
            "SELECT 1 FROM voided_trips WHERE trip_id = ?", (trip_id,)
        ).fetchone() is not None:
            raise ApiError(
                409, "TRIP_ALREADY_VOIDED",
                f"原行程 {trip_id} 已作废，不能再执行改期",
                {"trip_id": trip_id},
            )

        # 2) 关联船舶档案：仍启用且版本未变（直接提交参数确认的快照跳过）
        if snap["ship_no"] is not None:
            # 未知船号 SHIP_NOT_FOUND / 停用 SHIP_DISABLED / 版本已变
            # SHIP_VERSION_STALE，均拒绝且不改状态
            resolve_for_confirm(conn, snap["ship_no"], snap["ship_version"])

        # 3) 目标修订存在且仍为最新
        if conn.execute(
            "SELECT 1 FROM tide_revisions WHERE revision = ?", (target_revision,)
        ).fetchone() is None:
            raise ApiError(404, "TIDE_REVISION_NOT_FOUND",
                           f"潮位修订不存在: {target_revision}",
                           {"revision": target_revision})
        latest = conn.execute(
            "SELECT revision FROM tide_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if latest is None or latest["revision"] != target_revision:
            raise ApiError(
                409, "REVISION_STALE",
                f"潮位已更新到修订 #{latest['revision'] if latest else None}，"
                f"目标修订 #{target_revision} 已过期，请重新复核改期",
                {"provided_revision": target_revision,
                 "current_revision": latest["revision"] if latest else None},
            )

        # 4) 事务内按当前管制/组关系/泊位日历/其他有效占用（排除原行程）复算
        review = review_and_reschedule(conn, trip_id, target_revision)

        if review["safe"]:
            # 零航段行程恒安全，也在此被拒（无需改期）
            raise ApiError(
                409, "TRIP_SAFE",
                f"原行程 {trip_id} 在目标修订 #{target_revision} 下仍安全，"
                "无需改期，拒绝执行",
                {"trip_id": trip_id, "target_revision": target_revision},
            )
        recalculated = review["reschedule"]
        if recalculated is None:
            # 原快照不安全但窗口内无改期解（潮位可行但被占满/泊位无即时作业
            # 区间同样无解）：拒绝且不改变状态
            raise ApiError(
                409, "PLAN_MISMATCH",
                "窗口内不存在可行改期行程（含其他有效占用占满或原泊位当前日历"
                "无可即时作业区间），拒绝执行改期",
                {"reason": "no_feasible_itinerary",
                 "first_violation": review["first_violation"]},
            )

        # 5) 提交的逐段出发分钟须与复算最优建议一致
        canonical_deps = [
            leg["start_minute"] for leg in recalculated["itinerary"]
        ]
        if list(departures) != canonical_deps:
            raise ApiError(
                409, "PLAN_MISMATCH",
                "提交的逐段出发分钟与事务内按当前管制/组/泊位日历/占用复算的"
                "最优改期建议不一致（请重新调用复核接口取建议后再执行）",
                {"reason": "itinerary_mismatch",
                 "submitted_departures": list(departures),
                 "recalculated_departures": canonical_deps,
                 "recalculated": recalculated},
            )

        # 6) 写入新不可变快照（同一事务）。参数、档案引用沿用原快照；修订为
        #    目标修订；各段航时/占用与泊位作业区间按复算结果固化。
        cur = conn.execute(
            "INSERT INTO confirmed_trips"
            " (revision, origin, destination, start_minute, window_minutes,"
            "  draft, height, margin, arrival_minute, itinerary,"
            "  ship_no, ship_version, berth_id, work_minutes,"
            "  work_start_minute, work_end_minute)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (target_revision, snap["origin"], snap["destination"],
             snap["start_minute"], snap["window_minutes"],
             snap["draft"], snap["height"], snap["margin"],
             recalculated["arrival_minute"],
             json.dumps(recalculated["itinerary"]),
             snap["ship_no"], snap["ship_version"],
             recalculated["berth_id"], recalculated["work_minutes"],
             recalculated["work_start_minute"],
             recalculated["work_end_minute"]),
        )
        new_trip_id = cur.lastrowid

        # 7) 同事务作废原行程（原占用即时失效；历史快照仍可查）。直接插入
        #    voided_trips（上面已核验未作废；主键兜底并发），并在同事务生成
        #    等额冲销单（原账单存在时，恰一次）。
        try:
            conn.execute(
                "INSERT INTO voided_trips(trip_id, reason) VALUES (?, ?)",
                (trip_id, _void_reason(trip_id, new_trip_id)),
            )
        except sqlite3.IntegrityError:
            raise ApiError(
                409, "TRIP_ALREADY_VOIDED",
                f"原行程 {trip_id} 已作废，不能再执行改期",
                {"trip_id": trip_id},
            )
        create_reversal_in_void(conn, trip_id)
        void_record = get_void_record(conn, trip_id)
        reversal = _reversal_status(conn, trip_id)

        result = {
            "old_trip_id": trip_id,
            "new_trip_id": new_trip_id,
            "target_revision": target_revision,
            "recalculated": recalculated,
            "old_void": {
                "trip_id": trip_id,
                "reason": void_record["reason"],
                "voided_at": void_record["voided_at"],
            },
            "reversal": reversal,
            "replayed": False,
        }

        # 8) 登记改期幂等记录（唯一键兜底并发同键竞争）
        try:
            conn.execute(
                "INSERT INTO reschedule_records"
                " (idempotency_key, trip_id, new_trip_id, target_revision,"
                "  content_hash, result)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (idempotency_key, trip_id, new_trip_id, target_revision,
                 digest, json.dumps(result, ensure_ascii=False)),
            )
        except sqlite3.IntegrityError:
            # 并发同键竞争：回滚后按已存在记录重判重放/冲突（不保存本事务
            # 任何写入——新快照与作废随之撤销）
            conn.execute("ROLLBACK")
            row = conn.execute(
                "SELECT trip_id, content_hash, result FROM reschedule_records"
                " WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise ApiError(409, "RESCHEDULE_KEY_CONFLICT",
                               "改期幂等键并发冲突，请换用新的幂等键重试",
                               {"idempotency_key": idempotency_key})
            if row["content_hash"] != digest or row["trip_id"] != trip_id:
                raise ApiError(
                    409, "RESCHEDULE_KEY_CONFLICT",
                    "同一改期幂等键已用于不同的提交内容，拒绝执行",
                    {"idempotency_key": idempotency_key,
                     "existing_trip_id": row["trip_id"]},
                )
            replay = json.loads(row["result"])
            replay["replayed"] = True
            return replay, True

        conn.execute("COMMIT")
        return result, False
    except ApiError:
        _safe_rollback(conn)
        raise
    except Exception:
        _safe_rollback(conn)
        raise


def _safe_rollback(conn) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        # 竞争分支可能已自行回滚/提交
        pass
