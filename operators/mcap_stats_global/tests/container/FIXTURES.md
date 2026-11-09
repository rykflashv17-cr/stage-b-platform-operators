# 固定容器样例

使用合成 Marvin `clean.mcap`，由当前 B01 5.0.0 → B02 5.0.0 → B03 5.0.0 真实业务函数生成；不包含采集业务数据。
统计与质量 ZIP 绑定同一完整 cohort，质量摘要同时绑定 B02 `context.json` 摘要。
每个容器测试只读取这里的固定输入，不会临时调用上游算子。
已验证真实 MCAP 解码、统计拟合、质量通过及交付原件字节一致；SDK 入口和容器运行仍需单独验收。

```json
{
  "normalized_episode_ref.zip": "e35fa40eeec1c06dffaeef3db92060de0328a34fc7cb105ba32f2a4a7d7155d7"
}
```
