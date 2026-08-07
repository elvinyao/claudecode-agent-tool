# 教学示例来源

本目录中的 Trivy JSON 是为演示本项目而整理的 **adapted educational fixtures**，不是在
2026 年重新扫描镜像得到的结果，也不能代表漏洞或版本在今天仍处于相同状态。为使示例短小、
可重复，只保留了 Trivy native JSON v2 中与解析、建议和渲染有关的字段。

## Spring Boot / Spring4Shell

- 来源：[Trivy Modules 文档中的 Spring4Shell 输出示例](https://trivy.dev/docs/v0.66/advanced/modules/)
- 采用的教学字段：`org.springframework.boot:spring-boot` 2.6.3、
  CVE-2022-22965、FixedVersion `2.5.12, 2.6.6`，以及模块在示例运行条件下给出的 LOW 严重度。
- 改编：将终端表格整理为最小 Trivy JSON v2；描述文字为摘要。

## Alpine EOSL

- 来源：[Trivy “Exit on EOL” 文档示例](https://trivy.dev/docs/v0.58/configuration/others/)
- 采用的教学字段：Alpine 3.10.9、`EOSL: true`、`apk-tools` 2.10.6-r0、
  CVE-2021-36159、FixedVersion 2.10.7-r0。
- 改编：将终端表格整理为最小 Trivy JSON v2；描述文字为摘要。

来源核对日期：**2026-08-05**。

`analysis-*.json` 是手工准备的固定结构化响应，用于在没有 Codex / Claude 凭据、没有网络的
情况下稳定生成 HTML。它们没有触发真实 Agent 调用，也没有执行联网增强；对应 HTML 会醒目标注
这一点。完成任何整改后，都应使用最新 Trivy 数据库重新扫描，并结合厂商公告与兼容性测试确认。
