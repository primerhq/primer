"""Unit tests for the char-heuristic token counter."""

from __future__ import annotations

from primer.llm._tokenizer.char_fallback import count_tokens_char_fallback
from primer.model.chat import (
    ImagePart,
    Message,
    TextPart,
    Tool,
    ToolCallPart,
    ToolResultPart,
)


class TestCountTokensCharFallback:
    def test_text_only(self) -> None:
        msgs = [Message(role="user", parts=[TextPart(text="hello world")])]
        # 8 overhead + ceil(11/4)=3 = 11
        assert count_tokens_char_fallback(messages=msgs, tools=None) == 11

    def test_multi_message(self) -> None:
        msgs = [
            Message(role="user", parts=[TextPart(text="abcd")]),
            Message(role="assistant", parts=[TextPart(text="abcdefgh")]),
        ]
        assert count_tokens_char_fallback(messages=msgs, tools=None) == 19

    def test_tool_call_part(self) -> None:
        msgs = [
            Message(
                role="assistant",
                parts=[ToolCallPart(id="c1", name="ls", arguments={"path": "/"})],
            )
        ]
        # 8 + (50 + 2 + ceil(13/4)=4) = 64
        assert count_tokens_char_fallback(messages=msgs, tools=None) == 64

    def test_tool_result_part(self) -> None:
        msgs = [
            Message(
                role="tool",
                parts=[ToolResultPart(id="c1", output="ok", error=False)],
            )
        ]
        # 8 + (20 + 1) = 29
        assert count_tokens_char_fallback(messages=msgs, tools=None) == 29

    def test_image_part(self) -> None:
        msgs = [
            Message(
                role="user",
                parts=[ImagePart(mime_type="image/png", data=b"\x00\x01")],
            )
        ]
        # 8 + 1000 = 1008
        assert count_tokens_char_fallback(messages=msgs, tools=None) == 1008

    def test_tools_serialised_into_total(self) -> None:
        msgs = [Message(role="user", parts=[TextPart(text="hi")])]
        tools = [
            Tool(
                id="ls",
                description="list dir",
                toolset_id="x",
                args_schema={"type": "object", "properties": {}},
            )
        ]
        result = count_tokens_char_fallback(messages=msgs, tools=tools)
        assert result > 9


class TestScriptAwareEstimate:
    """GI-19: chars/4 undercounted CJK about 1.8x, so the 0.90 compaction trigger fired after the window was passed."""

    ZH = "我们需要在下周之前完成这个项目的所有测试，并把结果发给团队里的每一个人。"
    JA = "来週までにこのプロジェクトのすべてのテストを終わらせてください"
    KO = "다음 주까지 이 프로젝트의 모든 테스트를 끝내야 합니다"
    RU = "Нам нужно закончить все тесты этого проекта до следующей недели."

    @staticmethod
    def _text(text: str) -> list[Message]:
        return [Message(role="user", parts=[TextPart(text=text)])]

    def test_cjk_counts_at_least_one_token_per_character(self) -> None:
        from primer.agent.compaction import CompactionStrategy

        for line in (self.ZH, self.JA, self.KO):
            cjk = sum(1 for c in line if "\u3040" <= c <= "\u30ff" or "\u4e00" <= c <= "\u9fff" or "\uac00" <= c <= "\ud7af")
            msgs = self._text(line * 50)
            assert count_tokens_char_fallback(messages=msgs) >= cjk * 50
            assert CompactionStrategy._estimate_tokens(msgs) == count_tokens_char_fallback(messages=msgs)

    def test_latin_and_cyrillic_stay_at_a_quarter_token_per_character(self) -> None:
        for line in ("hello world", self.RU):
            assert count_tokens_char_fallback(messages=self._text(line)) == 8 + -(-len(line) // 4)

    def test_mixed_text_counts_each_script_at_its_own_rate(self) -> None:
        # 4 CJK characters + 8 Latin characters: 4 + ceil(8 / 4) = 6
        assert count_tokens_char_fallback(messages=self._text("你好世界abcdefgh")) == 8 + 6

    def test_cjk_in_tool_results_calls_and_tool_schemas_counts_too(self) -> None:
        zh = "测试" * 100
        result = [Message(role="tool", parts=[ToolResultPart(id="c", output=zh, error=False)])]
        assert count_tokens_char_fallback(messages=result) == 8 + 20 + 200
        call = [Message(role="assistant", parts=[ToolCallPart(id="c", name="t", arguments={"q": zh})])]
        assert count_tokens_char_fallback(messages=call) >= 8 + 50 + 200
        tool = Tool(id="t", description=zh, toolset_id="x", args_schema={"type": "object"})
        assert count_tokens_char_fallback(messages=[], tools=[tool]) >= 50 + 200
