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

## 服务日容量口径

线路容量按**起点机房时区的自然日**（当地 00:00 到次日 00:00，半开区间）作为租户服务日统一边界：维护窗口、机房时区和提名的 `service_date` 使用同一套 UTC 区间。维护只按与服务日真正重叠的时长折算；跨午夜维护拆分到相邻两个服务日，开放式维护（无 `ends_at`）持续生效；同一时段多段维护按 `capacity_percent` 连乘，计算结果与登记顺序无关。

查询某服务日的原始容量、每段降容的重叠时长与损失贡献、分段明细和最终可分配量：

```bash
curl -H 'X-Actor-Id: audit' \
  'http://127.0.0.1:8080/routes/fabric-wlmq-east/capacity?service_date=2026-09-25'
```

重复执行 `POST /routes/{route_id}/allocate` 且输入不变时返回已存储的同一结果（`replayed: true`），不新增运行记录、不改写历史预约；只有出现新的待分配提名导致输入变化时才会产生新的分配运行。

