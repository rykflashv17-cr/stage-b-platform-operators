# 固定容器样例

使用合成 Marvin `clean.mcap`，由当前 B01 5.0.0 → B02 5.0.0 → B03 5.0.0 真实业务函数生成；不包含采集业务数据。
统计与质量 ZIP 绑定同一完整 cohort，质量摘要同时绑定 B02 `context.json` 摘要。
每个容器测试只读取这里的固定输入，不会临时调用上游算子。
已验证真实 MCAP 解码、统计拟合、质量通过及交付原件字节一致；SDK 入口和容器运行仍需单独验收。

```json
{
  "clean.mcap": "7aadf41327f2732f08c3ca821002dcef91ba6d476c2cd56cbddce5f434567ae0",
  "episode_quality_result.zip": "fc7656a68ff70b6a0fbc3003caefd26993712cdda82a3a8fc506a8c83f6d26c8"
}
```
