# 修复跨时区算力日配额错位基础平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `fixtures/`：离线验收使用的结构化协议与测点；
- `tests/`：核心规则、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m compute_fabric.acceptance --workspace .
PYTHONPATH=src python3 -m accelerator_lab.acceptance --workspace .
PYTHONPATH=src python3 -m silicon_qualification.acceptance
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析和芯片准入流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
```

服务均提供 `GET /health`，其余接口使用 JSON。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。

## 服务日容量边界

服务日（`service_date`）按互联通道**起点机房**的 IANA 时区解释为当地日历日半开区间 `[00:00, 次日 00:00)`，维护窗口以绝对 UTC 时刻登记后统一裁剪到该边界，因此机房时区、线路维护窗口和租户服务日共用同一套口径：

- 跨午夜（跨 UTC 自然日）维护只按与服务日真正重叠的时长加权扣减，并自动拆分到两个服务日；
- 开放式维护（不填 `ends_at`）从生效时刻起持续覆盖之后每一个服务日；
- 同一时段叠加多段维护时容量百分比相乘，时间片按 `outage_id` 固定切分，重复计算结果稳定；
- 事后补登记维护只改变之后的解释与分配结果，`allocation_runs`（含 `explanation_json`）和历史预约落库状态不会被静默重算或改写。

后台可通过以下接口解释任意服务日的原始容量、每段降容的重叠时长/比例、时间片明细与最终可分配量（planner/dispatcher/risk/auditor 均可查询）：

```bash
curl -H 'X-Actor-Id: audit' \
  'http://127.0.0.1:8080/routes/<route_id>/capacity?service_date=2026-09-25'
```

