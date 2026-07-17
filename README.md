# 燃气调压器健康诊断系统

面向燃气调压器运行数据的批量诊断、核心性能评价、异常定位与报告交付系统。

当前版本：`1.0.0`

## 1. 系统定位

本系统通过浏览器提供统一的诊断工作台，接收 CSV、XLSX 或 XLS 格式的运行数据，自动完成字段识别、数据清洗、性能计算、辅助异常检测、压力曲线展示和 PDF 报告生成。

诊断结论以以下三项核心性能为主：

- P01 调压器稳压性能；
- P02 调压器关闭压力性能；
- P03 调压器阀座密封性能。

统计特征、运行规则、健康基线、Isolation Forest、KNN 和趋势特征作为辅助证据，不替代三项核心性能的等级判定。系统保留计算参数、公式、倍率、分级区间、风险分和校核证据，支持结果解释、人工复核及过程追溯。

## 2. 主要能力

### 2.1 数据导入与质量检查

- 支持 `.csv`、`.xlsx` 和 `.xls` 文件；
- 支持一次导入多份网点或设备数据；
- 支持为每份数据独立设置设定压力、出厂 AC、出厂 SG、极值点数和泄漏限值；
- 自动识别站点、时间、出口压力、关闭压力和泄漏量等字段；
- 兼容常见日期时间格式并过滤无效记录；
- 输出原始记录数、有效样本数、无效样本数、时间范围和字段来源。

### 2.2 核心性能评价

系统计算 P01、P02 和 P03，并依据实际结果与出厂指标或规定限值的倍率关系形成五级评价结果：优良、合格、轻度偏差、较大偏差和严重偏差。

### 2.3 辅助异常检测

- 运行规则检测：识别压力超限、关闭压力偏高、波动异常和趋势漂移；
- Isolation Forest：识别多维特征空间中偏离健康样本分布的数据；
- KNN：计算当前样本与邻近健康样本之间的距离；
- 健康基线：比较当前特征与当前版本健康基线的分布差异；
- 统计与趋势证据：计算均值、标准差、变异系数、极值和变化率等特征。

### 2.4 可视化与报告

- 展示压力曲线、运行设定压力、AC 允许范围和 SG 上限；
- 标注参与 Pmax/Pmin 计算的实际采样点；
- 展示异常片段的时间范围、持续时长、压力范围及触发规则；
- 支持异常片段列表与压力曲线联动；
- 支持展开查看三项核心性能的完整计算过程；
- 支持单份 PDF 报告导出；
- 支持将当前批次全部 PDF 报告打包为 ZIP 文件。

### 2.5 智能分析意见

系统可选接入 DeepSeek，根据已生成的结构化诊断结果形成简明的结论和处置建议。智能分析不参与性能数值计算、等级判定或风险分生成。未配置 DeepSeek 时，核心诊断和报告功能仍可独立运行。

## 3. 核心诊断方法

### 3.1 P01 调压器稳压性能

系统从日间有效压力数据中选取最高 `N` 个压力点和最低 `N` 个压力点，分别计算均值 `Pmax` 和 `Pmin`：

```text
实际 AC = max(Pmax - 设定压力, 设定压力 - Pmin, 0)
          / 设定压力 × 100%
```

实际 AC 与设备出厂 AC 的比值用于确定性能等级。报告同时保留最高点、最低点、正负偏差、压力超限比例和连续超限时长等校核证据。

### 3.2 P02 调压器关闭压力性能

数据包含独立关闭压力字段时，系统优先采用实测关闭压力：

```text
实际 SG = max(实测关闭压力 - 运行压力设定值, 0)
          / 运行压力设定值 × 100%
```

实际 SG 与设备出厂 SG 的比值用于确定等级。未导入独立关闭压力字段时，系统依据低流量或近关闭运行片段进行估算，并在报告中标明判定来源和置信度。

### 3.3 P03 调压器阀座密封性能

数据包含泄漏量字段时，系统按照实测泄漏量与规定限值的倍率分级。

未导入泄漏量字段时，系统根据低流量片段中的压力正向爬升、升压斜率和爬升窗口复现比例形成密封趋势估算。该结果用于运行筛查和复核排序，不等同于规定试验条件下的正式泄漏量检测。

### 3.4 综合结果

三项核心性能共同形成综合健康等级和风险分。辅助模型、规则和趋势证据用于解释与校核，不单独覆盖核心性能结论。

## 4. 技术架构

```text
浏览器
  │
  ▼
Nginx
  ├─ 反向代理
  ├─ 上传限制
  └─ 请求限流
  │
  ▼
Waitress + Flask
  ├─ 文件上传与字段识别
  ├─ P01 / P02 / P03 计算
  ├─ 规则及模型辅助诊断
  ├─ 曲线与异常片段数据生成
  ├─ DeepSeek 可选分析
  └─ HTML 排版与 PDF 渲染
  │
  ▼
持久化数据目录
  ├─ 上传文件
  ├─ 诊断结果
  └─ PDF 报告与历史汇总
```

| 层级 | 技术 | 用途 |
|---|---|---|
| 前端 | HTML、CSS、JavaScript | 数据导入、参数配置、结果与曲线展示 |
| Web 服务 | Flask | 路由、校验、诊断编排和报告响应 |
| 生产服务 | Waitress | WSGI 多线程服务 |
| 反向代理 | Nginx | 对外访问、限流和上传控制 |
| 数据处理 | pandas、NumPy | 数据清洗、统计和特征计算 |
| 异常检测 | scikit-learn | Isolation Forest 和 KNN |
| 文件解析 | openpyxl、xlrd | Excel 数据读取 |
| PDF 渲染 | Chromium | 将报告 HTML 转换为 PDF |
| 部署 | Docker、Docker Compose | 环境封装、服务编排和数据持久化 |

## 5. 数据文件要求

推荐的数据结构如下：

| 字段 | 必需性 | 说明 |
|---|---|---|
| 采集时间 | 必需 | 日期时间或可解析的日期、时间组合 |
| 出口压力 | 必需 | 默认按 kPa 处理 |
| 站点名称 | 可选 | 缺失时由文件名辅助识别 |
| 关闭压力 | 可选 | 存在时优先用于 P02 实测计算 |
| 泄漏量 | 可选 | 存在时优先用于 P03 正式口径计算 |

系统会尝试识别常见中文、英文和组合字段名。诊断前应确认压力单位、设备设定压力、出厂 AC、出厂 SG 和泄漏限值与实际设备一致。

## 6. 推荐部署方式

### 6.1 Docker Compose

推荐在 Linux 服务器使用 Docker Compose：

```bash
cp .env.example .env
docker compose up -d --build
```

默认访问地址：

```text
http://<服务器地址>:8080
```

状态检查：

```bash
docker compose ps
curl http://127.0.0.1:8080/healthz
docker compose logs -f app nginx
```

停止服务：

```bash
docker compose down
```

生产数据保存在 Docker 数据卷中。除非已完成备份，不得使用 `docker compose down -v`。

镜像已包含 Chromium 和中文字体，宿主服务器及访问终端无需另外安装浏览器。

### 6.2 非容器部署

运行环境要求 Python 3.10 或更高版本：

```bash
python -m venv .venv
python -m pip install -r requirements.txt
python -m gas_diagnosis.production
```

非容器部署必须在服务器上安装 Chromium、Chrome 或 Edge，也可以通过 `GAS_CHROMIUM_PATH` 指定浏览器可执行文件。

Windows 环境可以执行：

```text
start_server.bat
```

## 7. 环境变量

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `HTTP_PORT` | `8080` | Docker Compose 对外端口 |
| `GAS_DATA_DIR` | `/data` | 上传文件、报告和历史记录目录 |
| `GAS_DIAGNOSIS_HOST` | `0.0.0.0` | 服务监听地址 |
| `GAS_DIAGNOSIS_PORT` | `8080` | 应用服务端口 |
| `GAS_MAX_UPLOAD_MB` | `50` | 单次请求大小上限 |
| `GAS_SERVER_THREADS` | `8` | Waitress 工作线程数 |
| `GAS_RETENTION_DAYS` | `30` | 运行文件保留天数，`0` 表示不自动清理 |
| `GAS_LOG_LEVEL` | `INFO` | 日志等级 |
| `GAS_PDF_TIMEOUT_SECONDS` | `60` | 单份 PDF 最长渲染时间 |
| `GAS_CHROMIUM_PATH` | `/usr/bin/chromium` | PDF 渲染器路径 |
| `GAS_TRUST_PROXY` | `1` | 是否信任受控反向代理转发头 |
| `GAS_ENABLE_SERVER_FILE_BROWSER` | `0` | 是否启用服务器本地文件诊断接口 |
| `DEEPSEEK_API_KEY` | 空 | DeepSeek API 密钥 |
| `DEEPSEEK_API_KEY_FILE` | 空 | DeepSeek 密钥文件路径 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | DeepSeek 模型名称 |
| `DEEPSEEK_API_URL` | 官方接口地址 | DeepSeek 兼容接口地址 |

真实 API Key 不得写入源码、Compose 文件、示例环境文件或 Git 提交历史。生产环境应使用 Secret 管理能力或只读密钥文件。

## 8. 运行维护与安全

### 8.1 持久化内容

`GAS_DATA_DIR` 下保存：

```text
outputs/web_uploads/                 用户上传文件
outputs/web_diagnosis/               诊断结果及报告
outputs/web_diagnosis/upload_diagnosis_log.csv
```

应根据数据重要程度制定备份、保留和清理策略，并定期验证恢复流程。

### 8.2 已实施控制

- 上传扩展名和请求体大小限制；
- 上传文件随机化存储；
- 报告读取目录边界控制；
- 生产环境默认关闭服务器本地路径诊断；
- 请求 ID、安全响应头和基础限流；
- 容器以非 root 用户运行并启用 `no-new-privileges`；
- 接口响应不返回服务器绝对路径。

系统未内置用户认证和权限管理。正式部署应位于企业内网、VPN 或统一访问网关之后，并由外部平台提供 HTTPS、身份认证、访问授权和审计。

## 9. API 概览

| 方法 | 路径 | 功能 |
|---|---|---|
| `GET` | `/` | 诊断工作台 |
| `GET` | `/rules` | 判定规则页面 |
| `GET` | `/healthz` | 服务健康检查 |
| `GET` | `/api/summary` | 历史诊断汇总 |
| `POST` | `/api/upload` | 上传一组数据并执行诊断 |
| `POST` | `/api/reports_zip` | 打包当前批次 PDF 报告 |
| `POST` | `/api/llm_analysis` | 生成智能分析意见 |
| `GET` | `/file?path=...` | 下载允许目录内的报告 |

`GET /api/files` 和 `POST /api/diagnose` 用于受控环境中的服务器本地文件诊断，受 `GAS_ENABLE_SERVER_FILE_BROWSER` 控制，生产环境默认关闭。

## 10. 项目结构

```text
gas_diagnosis/
├─ ai.py                 健康基线、Isolation Forest 和 KNN
├─ cli.py                诊断流程编排及命令行入口
├─ config.py             特征和规则配置
├─ data_loader.py        文件读取、字段识别与清洗
├─ features.py           统计及趋势特征计算
├─ performance.py        P01、P02、P03 核心性能计算
├─ pdf_report.py         PDF 渲染器
├─ production.py         Waitress 生产入口
├─ report.py             报告内容与页面生成
├─ rules.py              运行规则及等级融合
├─ server.py             Flask 生产应用
├─ web_app.py            诊断服务函数
└─ static/               前端页面

models/baseline_healthy.json   当前使用的健康基线
deploy/nginx.conf              Nginx 配置
tests/                         服务端和前端约束测试
Dockerfile                     生产镜像
compose.yaml                   服务编排
.env.example                   环境变量示例
start_server.bat               Windows 启动入口
```

仓库不包含现场原始数据、过程性分析材料或需求文档。运行数据应通过系统上传，并保存在部署环境的数据目录中。

## 11. 验证

```bash
python -m unittest tests.test_server -v
node tests/check_frontend_bindings.js
python -m compileall -q gas_diagnosis tests
docker compose config --quiet
```

自动化测试覆盖健康检查、安全响应头、非法文件拒绝、报告目录边界、CSV 上传诊断、PDF 生成、PDF 下载、批量报告打包和前端关键交互绑定。

正式发布前还应在目标服务器完成一次真实文件诊断、单份报告导出、批量报告导出、服务重启和数据恢复验证。

## 12. 适用范围与限制

1. 诊断结果依赖输入字段、设备参数和数据质量，使用前必须确认参数与实际设备一致。
2. 缺少独立关闭压力或泄漏量字段时，P02 或 P03 使用运行数据片段估算，应结合现场检测复核。
3. 智能分析仅解释既有结果，不参与算法计算和等级判定。
4. 当前部署适用于单台服务器及中小规模内部应用；横向扩展时应引入共享数据库、对象存储和异步任务队列。
5. 本系统提供运行数据诊断和辅助决策依据，不替代国家标准、行业规范、设备制造商要求或法定现场检测程序。
