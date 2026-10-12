"""实验室（离线回测 / 蒙特卡洛 / 每日统计报告）。

只读数据库「副本」，与线上网关完全隔离：网关（app/main.py 等）不 import 本包，网关镜像也不装 numpy / matplotlib。
运行：python -m app.lab.run --db <副本路径> --out data/lab
  data.py        读事件流（含 ver / 请求特征）、部署与规则版本、重试识别与重试模型、每日计数、成员
  replay.py      确定性回放（新需求 + 显式重试模型；单服务台 max(耗时, 间隔+抖动)；各项上限）与分段校准
  montecarlo.py  两阶段 block bootstrap（先抽天、再抽当天成员）+ 嵌套不确定性，两维度失控判定，理论容量交叉印证
  backtest.py    事先登记网格 → 7 训练 / 3 验证 / 4 样本外（只跑一次）→ 滚动前推 → 预测区间覆盖率
  stats.py       cluster bootstrap、ICC / 设计效应、Wilson、BCa、MWU / Cliff's δ / Holm（回测用）、样本量门槛
  report.py      日报 2×2 图 + 容量图 + 中文摘要 + results.json（只描述与区间，不做今天 vs 昨天检验）
  synth.py       合成数据（只能做自洽检验，不能当验证）
"""
