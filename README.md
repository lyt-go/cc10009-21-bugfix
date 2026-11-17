# 分布式网页抓取调度平台

面向分布式网页抓取任务调度、节点协同、请求分配、反爬策略编排、数据校验入库与抓取质量大盘的采集工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集节点 | `worker` | 工作节点 | 节点编号、主机地址、并发槽位数 |
| 请求分配 | `dispatch` | 分配策略 | 策略编号、目标域名、分配算法 |
| 抓取作业 | `crawljob` | 作业定义 | 作业编号、目标站点、起始URL |
| URL过滤 | `urlfilter` | 过滤规则 | 规则编号、包含模式、排除模式 |
| DNS解析 | `dnsresolver` | 解析配置 | 配置编号、域名模式、DNS服务器 |
| 内容提取 | `contentextract` | 提取器 | 提取器编号、内容类型、正文选择器 |
| 存储策略 | `storagestrategy` | 存储方案 | 方案编号、存储类型、连接参数 |
| 渲染任务 | `rendertask` | 渲染作业 | 作业编号、目标URL、浏览器版本 |
| 采集质量 | `qualityreport` | 质量报告 | 报告编号、作业编号、总URL数 |
| 带宽控制 | `bandwidth` | 带宽策略 | 策略编号、节点标识、下载带宽上限 |
| 重试队列 | `retryqueue` | 重试条目 | 条目编号、失败URL、失败原因码 |
| 翻页管理 | `pagemarker` | 翻页规则 | 规则编号、目标站点、翻页类型 |
| 增量标记 | `incrementmark` | 增量水位 | 水位编号、目标源、增量字段名 |
| 配置同步 | `configsync` | 同步任务 | 任务编号、源节点、目标节点 |
| 采集API | `scrapeapi` | API定义 | API编号、API名称、请求路径 |
| 数据抽样 | `datasample` | 抽样方案 | 方案编号、数据源、抽样方式 |
| 采集告警 | `alertpolicy` | 告警策略 | 策略编号、监控指标、触发条件 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.tsx`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
- 静态路径（如 `/export`）必须声明在动态路径 `/{entry_id}` 之前，否则会被动态路由吞掉。

## 采集质量链路

按「抓取作业 URL 结果 → 字段提取覆盖率 → 质量报告快照 → 确认/归档 → 回写字段提取侧」贯通：

1. `POST /api/qualityreport/url-results` 登记作业 URL 抓取结果（按 作业+版本+URL 幂等），
   新一轮抓取或失败 URL 更正以新的「作业版本」写入，历史版本保留。
2. `POST /api/qualityreport/{id}/actions` 执行「生成报告」：读取最新作业版本的 URL 结果与
   内容提取器的版本化字段覆盖率，校验 `成功 + 失败 = 总数` 后冻结指标，生成质量结论、
   写入 `quality_snapshot`（作业质量清单数据集）并把报告统计字段全部落库。
3. 状态机：草稿 → 生成中 → 已生成 → 已确认 → 已归档。未生成完成不能确认，未确认不能归档；
   确认后快照冻结，`POST /api/contentextract/{id}/coverage` 命中已确认提取版本会被拒绝。
4. 并发与幂等：同一报告并发生成由服务端锁串行化，同 `request_id` 或相同数据指纹的重复提交
   幂等返回，只产生一个结论；生成回调按 `callback_id` 幂等，重复到达不重复累计指标。
5. 版本冲突：确认后失败 URL 被更正并重试生成时，历史已确认结论优先保留，新一轮结果另建快照，
   两组作业版本依据并存，报告的「版本冲突」字段记录处置过程。
6. 复核与文件：`GET /api/qualityreport/{id}/snapshots/{version}` 复核来源 URL 与提取版本漂移；
   `GET /api/qualityreport/{id}/conclusion` 返回 JSON（可书签）或 `?format=txt` 下载附件；
   生成/确认两阶段的质量结论通过幂等流水回写到内容提取侧清单。

后端测试：`cd backend && .venv/bin/python -m unittest discover -s tests`

领域：数据工程
子方向：数据采集、爬虫与RPA
