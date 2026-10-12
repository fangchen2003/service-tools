"""公平调度（DRR）：等成本下挑最近服务最少的 Key；狂刷的 Key 不能垄断；影子对比统计。"""
import time

from app.scheduling import FairScheduler, drr_pick


def test_drr_picks_least_served_then_arrival_order():
    # A 已出 3 张、B 出 1 张、C 没出过：C 先，其次 B
    served = {1: 3, 2: 1, 3: 0}
    assert drr_pick([1, 2, 3], served) == 3
    assert drr_pick([1, 2], served) == 2
    # 并列看到达顺序
    assert drr_pick([2, 3], {2: 0, 3: 0}) == 2
    assert drr_pick([], {}) is None
    # quantum=2：出 0/1 张算同一轮，按到达顺序
    assert drr_pick([1, 2], {1: 1, 2: 0}, quantum=2) == 1


def test_hammering_key_does_not_monopolize_under_drr():
    # 一把狂刷的 Key(9) 和三个正常 Key，交替请求：DRR 轮流，不会让 9 连出
    sched = FairScheduler()
    now = 1_000_000.0
    order = []
    waiters = [9, 9, 9, 9, 9, 1, 2, 3]            # 9 排了 5 张，其他各 1 张
    served = {}
    for _ in range(8):
        pick = drr_pick([w for w in waiters], served, quantum=1)
        order.append(pick)
        served[pick] = served.get(pick, 0) + 1
        waiters.remove(pick)
    # 9 不会在 1/2/3 都还没出过时连出：前 4 个里 1,2,3 都应出现
    assert set(order[:4]) >= {1, 2, 3}


def test_shadow_counts_unfairness_without_changing_order():
    sched = FairScheduler()
    now = 1_000_000.0
    for i in range(3):
        sched.on_serve(9, now + i)                # key 9 连出 3 张
    sched.on_serve(1, now + 3)                    # key 1 出 1 张
    # FIFO 此刻服务 9，但 key 1 还在等、服务更少 → DRR 会挑 1 → 记为不公平
    sched.observe_pick(9, [9, 1], now + 4)
    sched.observe_pick(1, [1, 9], now + 5)        # 这次 FIFO 服务 1，与 DRR 一致，公平
    r = sched.report(now + 6)
    assert r["shadow_decisions"] == 2 and r["unfair_rate"] == 0.5
    assert r["active_keys"] == 2 and r["skew"] == 2 and r["monopoly"] == 0.75


def test_drr_anti_starvation_overrides_fairness():
    from app.scheduling import drr_pick
    # key 9 服务最少(本该它先)，但 key 1 的图已经等了 70 秒 → 防饥饿强制 key 1
    served = {1: 5, 9: 0}
    ages = {1: 70, 9: 2}
    assert drr_pick([9, 1], served, ages=ages, starvation=60) == 1
    # 没人超时 → 回到公平(服务少的先)
    assert drr_pick([9, 1], {1: 5, 9: 0}, ages={1: 10, 9: 2}, starvation=60) == 9
