"""usage_ai 的 feature 摘要智能提取单元测试。

覆盖 _feature_insight_summary (喂给 team 分析的 feature 摘要提取):
- 正常 markdown 5 段 answer: 只提取「结论」+「建议动作」两段
- 降级占位文案 (超时/失败): 回退到原 260 字截断, 不返回空
- 空 answer: 返回空串
- 提取结果不含流程状态/细节复盘 (这些 team 不需要, 且会浪费 token)
"""
from __future__ import annotations

from eval.usage_ai import _feature_insight_summary


def test_extracts_conclusion_and_action_from_markdown_sections() -> None:
    """真实 feature answer (markdown 5 段) 应只提取结论 + 建议动作。"""
    answer = (
        "## 1. 结论\n\n需求已跑通，6 个阶段全部完成。最大问题：review 未通过。\n\n"
        "## 2. 流程状态\n\n工作流完整，归档完成。\n\n"
        "## 3. 完成效率\n\n总耗时 80 分钟。\n\n"
        "## 4. 细节复盘\n\nreview 首次失败，28 个 findings。\n\n"
        "## 5. 建议动作\n\n1. 排查 session 定位。2. 前置静态规范。\n"
    )
    summary = _feature_insight_summary(answer)
    # 核心两段都在
    assert "结论" in summary
    assert "建议动作" in summary
    assert "需求已跑通" in summary  # 结论正文
    assert "排查 session 定位" in summary  # 建议动作正文
    # team 不需要的章节不应出现 (信息密度优化)
    assert "流程状态" not in summary
    assert "细节复盘" not in summary


def test_falls_back_to_clip_on_degraded_placeholder() -> None:
    """降级占位文案 (超时跳过) 无可解析章节, 应回退到硬截断保留原文。"""
    placeholder = "[DeepSeek 在限定时间内未完成, 跳过]"
    summary = _feature_insight_summary(placeholder)
    assert summary == placeholder  # 原文短于 260, 原样返回
    assert "结论" not in summary  # 不应被强行套上章节标签


def test_empty_answer_returns_empty() -> None:
    assert _feature_insight_summary("") == ""
    assert _feature_insight_summary(None) == ""  # type: ignore[arg-type]


def test_summary_is_more_compact_than_raw_clipped() -> None:
    """正常 5 段 answer: 智能摘要应比 260 字硬截断更短 (省 token)。"""
    answer = (
        "## 1. 结论\n\n" + "需求跑通。" * 10 + "\n\n"
        "## 2. 流程状态\n\n" + "工作流完整。" * 30 + "\n\n"
        "## 3. 完成效率\n\n" + "耗时正常。" * 30 + "\n\n"
        "## 4. 细节复盘\n\n" + "review 失败。" * 30 + "\n\n"
        "## 5. 建议动作\n\n" + "排查根因。" * 10 + "\n"
    )
    summary = _feature_insight_summary(answer)
    # 智能提取只保留结论+动作两段, 远小于 260
    assert len(summary) <= 260
    # 但仍包含两段核心信息
    assert "结论" in summary
    assert "建议动作" in summary
