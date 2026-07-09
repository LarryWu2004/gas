# 燃气调压器健康诊断系统

这是一个面向燃气调压器运行压力数据的本地诊断项目。系统支持导入 CSV / Excel 数据，自动识别站点、时间和压力字段，围绕三项核心性能指标完成诊断，并生成可视化页面和 HTML 报告。

## 一、项目目标

项目目标是把已有运行数据、技术要求和诊断规则整理成一个可运行的本地 Web 系统，使用户导入新的调压器运行数据后，可以得到：

- 调压器当前健康等级
- 三项核心性能指标判定
- 压力曲线与异常片段定位
- 辅助模型和规则证据
- 简明诊断结论与处置建议
- 可导出的 HTML 诊断报告

## 二、核心诊断指标

系统报告以三项核心性能指标为主：

1. **P01 调压器稳压性能**
   - 根据运行压力高点、低点和设定压力计算实际 AC 表现。
   - 用于判断调压器在不同流量工况下保持出口压力稳定的能力。

2. **P02 调压器关闭压力性能**
   - 根据关闭压力字段或低流量片段的锁闭压力表现估算实际 SG 表现。
   - 用于判断调压器在低流量或关闭工况下压力是否偏高。

3. **P03 调压器阀座密封性能**
   - 优先使用泄漏量字段；没有泄漏量字段时，使用低流量片段压力爬升特征作为辅助判断。
   - 用于判断阀座密封状态是否存在风险。

## 三、辅助诊断能力

除核心指标外，系统还提供辅助证据：

- **规则触发**：稳压超限、关闭压力偏高、阀座泄漏、喘振波动、趋势漂移、夜间异常、波动率异常、静压漂移等。
- **Isolation Forest**：基于健康基线样本的无监督异常检测。
- **KNN 近邻异常分**：比较当前样本与历史健康样本的距离。
- **健康基线偏离**：比较当前统计特征与健康基线分布。
- **趋势与波动特征**：识别长期漂移、压力波动、异常片段。
- **DeepSeek 智能分析**：可选生成“结论 + 建议”的简短分析意见，不参与最终等级判定。

## 四、可视化与交互

前端页面包含：

- 健康等级和综合风险分
- P01 / P02 / P03 指标卡片
- 每个核心指标的可展开计算详情
- 压力运行曲线
- AC / SG 阈值线
- Pmax / Pmin 样本点
- 异常片段列表
- 点击异常片段后联动定位到曲线位置
- 辅助证据页
- 导入识别页
- HTML 报告导出
- 批量报告下载

## 五、项目结构

```text
D:\code\gas
├─ gas_diagnosis/                 核心源码
│  ├─ ai.py                       Isolation Forest、KNN、健康基线相关逻辑
│  ├─ cli.py                      命令行诊断入口和诊断流程封装
│  ├─ config.py                   特征字段、阈值和等级配置
│  ├─ data_loader.py              CSV / Excel 数据读取和字段识别
│  ├─ desktop_app.py              桌面入口辅助
│  ├─ features.py                 压力统计特征、趋势特征、曲线采样
│  ├─ performance.py              P01 / P02 / P03 核心性能指标计算
│  ├─ report.py                   HTML / JSON 报告生成
│  ├─ rules.py                    规则触发与健康等级融合
│  ├─ web_app.py                  本地 Web 服务和 API
│  └─ static/
│     ├─ index.html               前端主页面
│     └─ rules.html               判定规则说明页
├─ models/
│  └─ baseline_healthy.json       当前默认健康基线
├─ 调压器实验压力数据/             演示用压力数据
├─ 实验数据/                       历史压力数据
├─ README.md                      项目说明
├─ requirements.txt               Python 依赖
├─ start_server.bat               Windows 一键启动脚本
├─ deepseek_api_key.txt           本地 DeepSeek Key，已被 .gitignore 忽略
└─ deepseek_model.txt             本地 DeepSeek 模型名，已被 .gitignore 忽略
```

`outputs/`、`build/`、`dist/`、缓存文件和本地密钥文件不会提交到 GitHub。

## 六、运行方式

### 方式 1：双击启动

在 Windows 下双击：

```text
start_server.bat
```

启动后访问：

```text
http://127.0.0.1:8765
```

### 方式 2：命令行启动

```powershell
python -m gas_diagnosis.web_app --host 127.0.0.1 --port 8765
```

## 七、安装依赖

```powershell
pip install -r requirements.txt
```

依赖包括：

- pandas
- numpy
- openpyxl
- xlrd
- scikit-learn

## 八、DeepSeek 接入方式

系统已支持 DeepSeek。推荐使用本地文件配置，不把 API Key 写入源码：

```text
deepseek_api_key.txt
deepseek_model.txt
```

`deepseek_api_key.txt` 内容为 DeepSeek API Key。  
`deepseek_model.txt` 内容默认为：

```text
deepseek-v4-flash
```

也可以通过环境变量配置：

```powershell
$env:DEEPSEEK_API_KEY="你的 DeepSeek API Key"
$env:DEEPSEEK_MODEL="deepseek-v4-flash"
python -m gas_diagnosis.web_app
```

如果未配置 DeepSeek，系统会自动使用本地模板生成简短分析，不影响诊断结果。

## 九、清理后的保留内容

当前项目已删除：

- 构建产物 `build/`
- 打包产物 `dist/`
- 运行输出 `outputs/`
- Python 缓存 `__pycache__/`
- 旧版基线模型
- 早期验证脚本和临时报告
- 重复的 PPT 副本

当前保留的是运行本系统需要的源码、当前模型、数据、文档和启动入口。
