# 炉温冷档服务（Furnace Cold Archive）

把炉次逐分钟炉温压缩成受“整炉留点上限”约束的冷档，写入 SQLite，并按炉次与分钟区间查询复原炉温和连续处于工艺温区内的时段。

温度、误差、温区上下限全部用**十分之一摄氏度**的整数表示（例：`7800` = 780.0 °C）。
温区上下限**计入**温区（闭区间）。

## 快速启动

```bash
docker compose up --build
```

- 接口文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- 数据卷：`furnace-data`（SQLite 文件在容器内 `/data/furnace_archive.sqlite3`）

本地直接运行（需要 Python 3.11+）：

```bash
pip install -r requirements.txt
FURNACE_DB_PATH=./data/furnace_archive.sqlite3 \
  uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/archives` | 压缩一个炉次并整炉入档；无解或写入失败返回原因 |
| `POST` | `/api/v1/archives/{run_id}/split` | 把一个已归档炉次按分钟边界拆成两个独立炉次 |
| `GET` | `/api/v1/archives` | 列出已归档炉次（最新在前） |
| `GET` | `/api/v1/archives/{run_id}` | 归档元数据与留点数 |
| `GET` | `/api/v1/archives/{run_id}/minutes?start_minute_utc=&end_minute_utc=` | 区间内的复原炉温、缺测分钟、温区内连续时段 |
| `GET` | `/health` | 数据库可达性 |

### 入档示例

```bash
curl -s -X POST http://localhost:8000/api/v1/archives \
  -H 'Content-Type: application/json' -d '{
    "run_id": "HT-2026-0042",
    "start_minute_utc": 29800000,
    "readings": [250, 4800, 7000, 7800, 7990, 8000, 8000, 8010, 8000, 8000,
                 7990, 8000, 8000, 8000, 8000, 7990, 7800, 6000, 4000],
    "band_low_tenths_c": 7800,
    "band_high_tenths_c": 8200,
    "tolerance_tenths_c": 5,
    "max_retained_points": 13
  }'
```

`readings` 是最多 360 个逐分钟值，`null` 表示缺测。成功返回 `201` 和归档摘要（含留点数、缺测分钟数、分段数、内容校验和）。

### 查询示例

```bash
curl -s 'http://localhost:8000/api/v1/archives/HT-2026-0042/minutes?start_minute_utc=29800000&end_minute_utc=29800018'
```

返回：

- `minutes[]`：每个分钟的 `value_tenths_c`（复原值）、`missing`、`band_state`（`below` / `in_band` / `above`，缺测为 `unknown`）、`value_source`（`measured` 表示该分钟是留点且值为原实测值，`reconstructed` 表示值来自折线插值——含拆档时新引入的留点，缺测为 `unknown`）、`retained`（该分钟是否为留点）；
- `missing_minutes[]`：区间内缺测分钟号；
- `in_band_intervals[]`：连续处于温区内的时段（含 `duration_minutes`）。

区间内的 `in_band_intervals` 返回**所有与查询区间相交**的完整时段，不会被查询边界截断，因此可以直接读出一次保温持续了多久；缺测分钟会打断时段。
查询区间必须落在该炉次范围内，否则返回 `416`。

### 拆档示例

两炉记录误用了同一个炉次号时，质检员按分钟边界把一个归档拆成两个独立炉次。
前炉沿用原炉次号，后炉使用新炉次号，两段各自沿用原温区、误差上限和留点上限：

```bash
# 先取当前归档摘要（GET /archives/{run_id} 的返回体整体作为 expected_summary）
SUMMARY=$(curl -s http://localhost:8000/api/v1/archives/HT-2026-0042)
curl -s -X POST http://localhost:8000/api/v1/archives/HT-2026-0042/split \
  -H 'Content-Type: application/json' -d "{
    \"expected_summary\": $SUMMARY,
    \"back_run_start_minute_utc\": 29800007,
    \"new_run_id\": \"HT-2026-0043\"
  }"
```

请求字段：

- `expected_summary`：`GET /archives/{run_id}` 返回的完整摘要（含 `payload_sha256`）。与当前档案任一字段不符即整次拆档退回（`409 summary_mismatch`）。
- `back_run_start_minute_utc`：后炉开始的 UTC 分钟，必须严格落在原炉次范围内；该分钟之前归前炉，从该分钟起归后炉。
- `new_run_id`：后炉新炉次号，不能与任何在档炉次号相同（`409 run_already_archived`），也不能与原号相同。

拆档保证：

1. **两段都须有实测分钟**：切点若让某一段只剩缺测分钟，返回 `422 no_feasible_split`，原档案不变。
2. **逐分钟完全一致**：拆分前后每个分钟的复原值、缺测标记和温区判定都一致。新留点只在切点处按需引入，并用精确动态规划保证新折线对当前复原序列逐分钟**精确**复现，不重新做误差压缩。
3. **留点只取本段内的分钟**：缺测不跨段桥接；每段连续实测段的首尾仍须留点。
4. **独立可查才生效**：每段都必须在继承的留点上限内独立成档，否则整次拆档退回。
5. **来源可追溯**：原实测留点始终标记为 `measured`；拆档新引入的插值留点标记为 `reconstructed`，再次拆档也保持该区别。查档时 `value_source` 明确区分每个留点用的是原实测值还是复原值。
6. **原子生效**：删除原档与写入两段在同一个 `BEGIN IMMEDIATE` 事务内完成；摘要不符、新号占用、拆分无解或写入失败都会完整回滚，已有档案照常可查。

成功返回 `200` 与 `front_archive`、`back_archive` 两段摘要（含各自的 `measured_anchor_count` / `reconstructed_anchor_count`）。

## 压缩规则与保证

1. **每段连续实测数据保留首尾**：缺测会把炉次切成若干连续实测段，每段首、尾采样点必须留点。
2. **只用原采样点连成折线**：留点全部来自原采样点，保留值即原读数；不存任何合成点。相邻留点之间按直线插值（四舍五入到整数十分之一度），缺测分钟不插值、不跨段连接。
3. **误差约束**：每个实测分钟的复原值与原读数之差不超过 `tolerance_tenths_c`。
4. **温区判定一致**：每个实测分钟复原值的 `below / in_band / above` 判定必须与原读数一致，上下限计入温区（`low ≤ v ≤ high` 为 `in_band`）。
5. **留点上限**：整炉留点数不得超过 `max_retained_points`。规划器用精确动态规划求出**最小留点数**：
   `cost[0]=1`，`cost[b]=1+min{cost[a] : a<b 且 a、b 之间直线可行}`；
   各连续段相互独立，最小留点数相加即为整炉最小值。若最小值超过上限则返回 `422`，并给出所需最小留点数与上限。
6. **整炉入档**：任意一段无解即整炉不入档；成功时整炉一次性写入。
7. **只存压缩档**：SQLite 中只保存留点（分钟偏移 + 原值）、缺测区间、参数与 SHA-256 校验和，不保存原始逐分钟序列。
8. **写入原子性**：使用 `BEGIN IMMEDIATE` 事务，失败完整回滚；已归档数据不受影响，照常可查。
9. **读档完整性**：每次读取会校验 SHA-256、留点数、段首尾留点与可重建性；被篡改的归档返回 `500 archive_corrupt`，不会影响其他归档的查询。
10. **留点来源与拆档**：每个留点带 `measured`（原实测值）/ `reconstructed`（拆档引入的插值）来源标记，来源标记纳入 v2 校验和。旧版（v1）数据库启动时自动加列迁移，旧档案的留点一律视为 `measured`，其原有校验和保持不变。

## 错误与状态码

| 状态码 | `reason_code` | 含义 |
| --- | --- | --- |
| `201` | — | 整炉已入档 |
| `200` | — | 拆档成功，两段同时生效 |
| `409` | `run_already_archived` | 炉次已归档，或拆档新炉次号已被占用；原归档保持不变 |
| `409` | `summary_mismatch` | 拆档提交的摘要与当前档案不符（含被并发修改/篡改），整次退回 |
| `422` | `no_feasible_plan` | 入档无可行方案（附 `minimum_retained_points`、`max_retained_points`） |
| `422` | `no_feasible_split` | 拆档无解：切点越界、某段无实测分钟或某段超出继承的留点上限 |
| `422` | `invalid_request` / `invalid_range` | 请求校验失败 / 分钟区间颠倒 |
| `404` | `archive_not_found` | 该炉次无归档 |
| `416` | `range_outside_run` | 查询区间超出该炉次范围 |
| `503` | `archive_write_failed` | 写入失败（`retryable` 指示可否重试） |
| `503` | `archive_unavailable` | 数据库不可达/不可打开 |
| `500` | `archive_corrupt` | 归档校验失败 |

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

测试覆盖：规划器与穷举最优解对比、误差/温区/首尾留点约束、缺测不桥接、上限剔除原因、入库只存压缩档、重复炉次、写入失败后旧归档仍可查、篡改检测、区间与缺测标记；拆档逐分钟复原值/温区/缺测一致、实测与复原留点来源区分及再拆档保持、两段独立可查、摘要不符/新号占用/无解/写入失败整次回滚、v1 库迁移，以及 25 组随机档案的拆档不变量。

## 结构

```
app/planner.py   压缩规划（精确最小留点）、复原、温区时段
app/split.py     拆档规划（精确复原链、留点来源继承、分段上限校验）
app/db.py        SQLite 冷档存储、完整性校验、原子拆档事务与 v1→v2 迁移
app/models.py    请求/响应模型与字段校验
app/main.py      FastAPI 路由与统一错误响应
tests/           规划器、归档 API 与拆档测试
Dockerfile, docker-compose.yml
```
