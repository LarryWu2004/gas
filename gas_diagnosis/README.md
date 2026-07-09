# gas_diagnosis 模块说明

该目录保存燃气调压器健康诊断系统的核心代码。

## 模块职责

- `data_loader.py`：读取 CSV / Excel，识别站点、时间列、压力列。
- `features.py`：计算压力统计特征、趋势特征和曲线采样。
- `performance.py`：计算三项核心性能指标 P01 / P02 / P03。
- `rules.py`：执行规则触发和健康等级融合。
- `ai.py`：实现健康基线、Isolation Forest、KNN 辅助异常检测。
- `report.py`：生成 HTML / JSON 诊断报告。
- `web_app.py`：提供本地 Web 服务、上传诊断、批量报告和 DeepSeek 分析接口。
- `cli.py`：提供命令行诊断和基线构建能力。
- `static/index.html`：主前端页面。
- `static/rules.html`：判定规则说明页面。

## 诊断主线

1. 导入压力数据。
2. 自动识别站点、时间、压力字段。
3. 提取压力统计特征和曲线特征。
4. 计算 P01 稳压性能、P02 关闭压力性能、P03 阀座密封性能。
5. 计算规则触发、基线偏离、IF、KNN 和趋势证据。
6. 融合得到健康等级。
7. 输出前端可视化结果和 HTML 报告。

## DeepSeek 分析

`web_app.py` 中的 `/api/llm_analysis` 接口会读取：

- 环境变量 `DEEPSEEK_API_KEY`
- 或项目根目录 `deepseek_api_key.txt`

读取到 Key 后调用 DeepSeek 生成简短诊断结论和建议；没有 Key 时使用本地模板。
