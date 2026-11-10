"""Feedback Agent（质量检查、问题追溯与经验积累模块）。

职责边界（红线）：
  - 不重新计算地质灾害风险，不重新决定预警等级；
  - 不修改 Data / Risk / Simulation / Warning 的任何原始输出；
  - 只输出结构化反馈（feedback_result），由 Manager 决定是否重跑上游 Agent。
"""
