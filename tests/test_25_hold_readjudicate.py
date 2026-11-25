"""第25号 挂起重裁修复 —— 闭环必须同事务重裁毛重（甲路径，重裁修癸）。

三红夹具：
  甲：闭环后毛重未变 → 盯“挂起已闭、毛重仍超”的假绿；
  乙：影子裁定未晋升被当已裁放行 → 揪出“已闭但毛重仍超即放行”；
  争用：双会话同挂起抢闭环 → 须抢挂起锁，后到只记争用痕。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t25.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin", password: str = "admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _open_hold(c: TestClient, h: dict, ticket_code: str, axles: list[dict]) -> dict:
    """过磅一张超重票并取其开放挂起。"""
    lanes = c.get("/api/lanes", headers=h).json()["items"]
    lane = next(x for x in lanes if x["lane_code"] == "LN-N-01-IN")
    ticket = c.post(
        "/api/tickets",
        headers=h,
        json={
            "lane_id": lane["id"],
            "bl_ref": "BL-2026-0101",
            "vehicle_plate": "测重·00025",
            "axle_weights": axles,
            "ticket_code": ticket_code,
        },
    ).json()
    assert ticket["status"] == "旁路挂起"
    holds = c.get("/api/overloads?status=开放", headers=h).json()["items"]
    hold = next(x for x in holds if x["ticket_code"] == ticket_code)
    return {"ticket": ticket, "hold": hold}


# ---------------------------------------------------------------------------
# 失败测甲：闭环同事务重裁——挂起已闭，毛重不得仍超；重裁仍超须整笔回滚
# ---------------------------------------------------------------------------
def test_甲_close_hold_readjudicates_gross_in_same_tx():
    c = TestClient(app)
    h = _auth(c)
    opened = _open_hold(
        c, h, "WT-RJ25-甲",
        [{"axle": 1, "w": 13.0}, {"axle": 2, "w": 13.0},
         {"axle": 3, "w": 13.0}, {"axle": 4, "w": 13.0}],  # 52t > 阈值49
    )
    ticket_id = opened["ticket"]["id"]
    hold_id = opened["hold"]["id"]

    # 先递一版仍超重的复磅：闭环必须拒绝且整笔回滚，挂起仍开放、毛重不变
    bad = c.post(
        "/api/overloads/close",
        headers=h,
        json={
            "hold_id": hold_id,
            "note": "驳载不足",
            "reweigh_axle_weights": [
                {"axle": 1, "w": 12.6}, {"axle": 2, "w": 12.6},
                {"axle": 3, "w": 12.6}, {"axle": 4, "w": 12.6},
            ],  # 50.4t 仍 > 49
        },
    )
    assert bad.status_code == 422, bad.text
    with connect() as conn:
        hold_row = conn.execute(
            "SELECT status FROM overload_holds WHERE id=?", (hold_id,)
        ).fetchone()
        ticket_row = conn.execute(
            "SELECT gross_t,status FROM weigh_tickets WHERE id=?", (ticket_id,)
        ).fetchone()
    assert hold_row["status"] == "开放"          # 整笔回滚：挂起未闭
    assert ticket_row["status"] == "旁路挂起"    # 票态未动
    assert round(float(ticket_row["gross_t"]), 3) == 52.0  # 毛重未被污染

    # 再递合规复磅：同事务重裁毛重、写新裁定，闭环成立
    ok = c.post(
        "/api/overloads/close",
        headers=h,
        json={
            "hold_id": hold_id,
            "note": "驳载后复磅合规",
            "reweigh_axle_weights": [
                {"axle": 1, "w": 12.0}, {"axle": 2, "w": 12.0},
                {"axle": 3, "w": 12.0}, {"axle": 4, "w": 12.0},
            ],  # 48t <= 49
        },
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["reweigh"] is True
    assert round(float(body["gross_t"]), 3) == 48.0
    assert body["readjudicated"] is True
    with connect() as conn:
        hold_row = conn.execute(
            "SELECT status FROM overload_holds WHERE id=?", (hold_id,)
        ).fetchone()
        ticket_row = conn.execute(
            "SELECT gross_t,status,adjudicated_at FROM weigh_tickets WHERE id=?",
            (ticket_id,),
        ).fetchone()
    assert hold_row["status"] == "已关闭"
    assert ticket_row["status"] == "已裁定"
    assert round(float(ticket_row["gross_t"]), 3) == 48.0  # 毛重已重写
    assert ticket_row["adjudicated_at"]                     # 新裁定已落


# ---------------------------------------------------------------------------
# 失败测乙：不得把“未晋升的影子裁定/已闭但毛重仍超”当已裁放行
# ---------------------------------------------------------------------------
def test_乙_stale_overweight_gross_must_not_release_as_adjudicated():
    c = TestClient(app)
    h = _auth(c)
    opened = _open_hold(
        c, h, "WT-RJ25-乙",
        [{"axle": 1, "w": 13.0}, {"axle": 2, "w": 13.0},
         {"axle": 3, "w": 13.0}, {"axle": 4, "w": 13.0}],  # 52t > 49
    )
    ticket_id = opened["ticket"]["id"]
    hold_id = opened["hold"]["id"]

    # 旁路伪闭环：只关挂起、毛重仍超（模拟埋雷态/影子裁定未晋升）
    with connect() as conn:
        conn.execute(
            "UPDATE overload_holds SET status='已关闭', closed_at=? WHERE id=?",
            ("2026-11-24T09:00:00", hold_id),
        )
        conn.execute(
            "UPDATE weigh_tickets SET status='已裁定', adjudicated_at=? WHERE id=?",
            ("2026-11-24T09:00:00", ticket_id),
        )
        conn.commit()

    # 放行服务须按重裁口径复核毛重，假绿一律 409，不得抬杆
    gate = c.post(
        "/api/gate/release",
        headers=h,
        json={"ticket_id": ticket_id, "officer": "验收员"},
    )
    assert gate.status_code == 409, gate.text

    # 对账侧同样不得把毛重假绿记为健全
    recon = c.get("/api/reconcile/BL-2026-0101", headers=h).json()
    bad_ticket = next(t for t in recon["tickets"] if t["ticket_code"] == "WT-RJ25-乙")
    assert bad_ticket["gross_sound"] is False
    assert recon["gross_sound"] is False

    # 证明页也须暴露该票毛重未过阈，coherent 不得为真
    proof = c.get("/api/proof/ticket/WT-RJ25-乙", headers=h).json()
    assert proof["gross_sound"] is False


# ---------------------------------------------------------------------------
# 争用失败测：双会话同挂起抢闭环，须抢挂起锁，后到只记争用痕
# 第25路：起真实 uvicorn，双线程并发 HTTP 打同一挂起闭环。
# ---------------------------------------------------------------------------
def test_争用_concurrent_close_same_hold_only_one_wins(monkeypatch):
    import socketserver
    import uvicorn
    import httpx
    import locks as locks_mod

    c = TestClient(app)
    h = _auth(c)
    opened = _open_hold(
        c, h, "WT-RJ25-争",
        [{"axle": 1, "w": 13.0}, {"axle": 2, "w": 13.0},
         {"axle": 3, "w": 13.0}, {"axle": 4, "w": 13.0}],  # 52t > 49
    )
    ticket_id = opened["ticket"]["id"]
    hold_id = opened["hold"]["id"]
    lock_key = f"ticket:{ticket_id}"
    reweigh = {
        "hold_id": hold_id,
        "note": "双会话抢闭环",
        "reweigh_axle_weights": [
            {"axle": 1, "w": 12.0}, {"axle": 2, "w": 12.0},
            {"axle": 3, "w": 12.0}, {"axle": 4, "w": 12.0},
        ],
    }

    # 阶段编排，避开 SQLite 单写锁的假死锁：
    #   1) 胜者先把挂起锁落库并提交（锁仍由其持有）；
    #   2) 负者随后抢同一锁，读到锁行、只记争用即 409；
    #   3) 胜者等争用痕出现后，才继续闭环业务事务。
    loser_done = threading.Event()
    winner_hold = threading.Event()
    call_count = {"n": 0}
    count_lock = threading.Lock()
    orig_try_acquire = locks_mod.try_acquire

    def patched_try_acquire(conn, key, holder):
        if key != lock_key:
            return orig_try_acquire(conn, key, holder)
        with count_lock:
            call_count["n"] += 1
            seq = call_count["n"]
        if seq == 1:
            # 胜者：插锁并提交，随后停住，让负者先撞锁
            ok = orig_try_acquire(conn, key, holder)
            conn.commit()
            winner_hold.set()
            assert loser_done.wait(timeout=15), "未等到争用方"
            return ok
        # 负者：等胜者锁行落库后再尝试
        assert winner_hold.wait(timeout=15), "胜者未持锁"
        ok = orig_try_acquire(conn, key, holder)
        loser_done.set()
        return ok

    monkeypatch.setattr(locks_mod, "try_acquire", patched_try_acquire)

    with socketserver.TCPServer(("127.0.0.1", 0), None) as probe:
        port = probe.server_address[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
        else:  # pragma: no cover - 防御启动失败
            raise RuntimeError("测试服务未就绪")

        def token(user: str) -> dict:
            r = httpx.post(
                base + "/api/login",
                json={"username": user, "password": f"{user}123"}, timeout=5,
            )
            return {"Authorization": "Bearer " + r.json()["token"]}

        h_admin = token("admin")
        h_writer = token("writer")
        start = threading.Event()
        results: list[int] = []
        errors: list[str] = []

        def worker(headers: dict) -> None:
            try:
                start.wait(timeout=10)
                r = httpx.post(
                    base + "/api/overloads/close",
                    headers=headers, json=reweigh, timeout=20,
                )
                results.append(r.status_code)
            except Exception as exc:  # pragma: no cover - 防御并发异常
                errors.append(repr(exc))

        t1 = threading.Thread(target=worker, args=(h_admin,))
        t2 = threading.Thread(target=worker, args=(h_writer,))
        t1.start(); t2.start()
        start.set()
        t1.join(); t2.join()
        assert not errors, errors
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    # 一路成功闭环；另一路必为争用 409
    assert sorted(results) == [200, 409], results
    with connect() as conn:
        closed = conn.execute(
            "SELECT COUNT(*) c FROM overload_holds WHERE id=? AND status='已关闭'",
            (hold_id,),
        ).fetchone()["c"]
        contention = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE lock_key=?", (lock_key,)
        ).fetchone()["c"]
        ticket = conn.execute(
            "SELECT gross_t,status FROM weigh_tickets WHERE id=?", (ticket_id,)
        ).fetchone()
    assert closed == 1                 # 只闭环一次
    assert contention >= 1            # 后到争用痕已落库（不随 409 回滚）
    assert ticket["status"] == "已裁定"
    assert round(float(ticket["gross_t"]), 3) == 48.0
