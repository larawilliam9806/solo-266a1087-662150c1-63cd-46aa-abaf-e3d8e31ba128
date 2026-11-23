# 售后换件明细分包供数

FastAPI + SQLite 实现的按批次分包供数接口：同一批次的工单表与换件表按工单号
左连接，按工单号字典序整组分包，发布不可变清单版本，并按版本号与包号逐行下载
每个包的 CSV。

## 启动

```bash
docker compose up --build
```

- 服务地址：`http://localhost:8000`（`SUPPLY_PORT` 可改端口）
- 交互文档：`http://localhost:8000/docs`
- 健康检查：`http://localhost:8000/health`
- 数据文件：命名卷 `supply-data` 中的 `/data/supply.db`

本地直接运行：

```bash
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --port 8000
```

## 输入格式

工单 CSV（`tickets` 文件）：

```csv
ticket_no,station
W001,SP1
W002,SP2
```

换件 CSV（`parts` 文件）：

```csv
ticket_no,part_code
W001,PN-A
W002,PN-B
```

表头也接受中文列名 `工单号,送修点` 与 `工单号,零件码`。按 UTF-8 解码，允许
BOM；首尾空白会被去掉；空行忽略。工单号不能为空，重复行全部保留。

## 接口

### 1. 发布一批

```bash
curl -X POST http://localhost:8000/api/batches/2026-11-23/publish \
  -F tickets=@tickets.csv \
  -F parts=@parts.csv \
  -F row_limit=3
```

- 两表按工单号左连接：重复行照算（`n` 条工单行 × `m` 条换件行 = `n*m` 行），
  没有换件的工单行保留一条，`part_code` 为空。
- 按工单号字典序（字节序，与 SQLite 默认 `BINARY` 排序一致）装包。下一个工单号
  的整组明细放不下当前包时才开新包；同一工单号的行不会被拆到两个包。
- 任一工单号连接后行数超过 `row_limit` 时返回 `422`，响应给出该工单号与连接
  行数，本次不写库，本批原清单继续有效：

```json
{
  "detail": {
    "error": "ticket_row_limit_exceeded",
    "ticket_no": "W009",
    "row_count": 4,
    "row_limit": 3,
    "message": "ticket 'W009' joins to 4 rows, exceeding the per-package limit of 3"
  }
}
```

成功时返回 `201` 与清单（版本号形如 `<batch_id>.v0001`，每次成功发布递增）。
重交同批两表与上限即产生新版本，旧版本保持可下载。

### 2. 查看清单

```bash
curl http://localhost:8000/api/batches/2026-11-23/manifests          # 本批所有版本
curl http://localhost:8000/api/batches/2026-11-23/manifests/latest   # 本批最新版本
curl http://localhost:8000/api/manifests/2026-11-23.v0001            # 指定版本
curl http://localhost:8000/api/manifests/2026-11-23.v0001/manifest.csv
```

清单只列版本号与各包的首工单号、末工单号、行数，不展开连接明细：

```json
{
  "version": "2026-11-23.v0001",
  "batch_id": "2026-11-23",
  "row_limit": 3,
  "total_rows": 4,
  "ticket_count": 3,
  "package_count": 2,
  "created_at": "2026-11-23T17:59:24.784958Z",
  "packages": [
    {
      "package_no": 1,
      "first_ticket_no": "W001",
      "last_ticket_no": "W002",
      "row_count": 3,
      "download_url": "/api/manifests/2026-11-23.v0001/packages/1.csv"
    }
  ]
}
```

`manifest.csv` 的列为 `version,package_no,first_ticket_no,last_ticket_no,row_count`。

### 3. 逐包下载

```bash
curl -OJ http://localhost:8000/api/manifests/2026-11-23.v0001/packages/1.csv
```

输出列为 `ticket_no,station,part_code`，按工单号升序、换件行按提交顺序排列。
下载只读取该版本、该包工单号区间内的源记录，行数与清单中的 `row_count` 一致；
版本或包号不存在返回 `404`。

## 版本与并发

- 每个成功发布的版本是一份不可变快照（`tickets`、`parts` 按版本存放）。
  下载期间发布新版不会影响已发布清单的下载内容。
- 发布使用 `BEGIN IMMEDIATE` 串行化写入；SQLite 使用 WAL，下载读取不被写入阻塞。
- 发布失败（校验不通过或单工单超限）不产生任何写入。

## 数据表

`manifests`（版本、批次、上限、总行数、包数）、`manifest_packages`（每包首末
工单号与行数）、`tickets` / `parts`（按版本保存的原始两表，保留重复与顺序）。
连接明细在下载时按包区间在 SQL 中左连接，不落明细大表。

## 测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```
