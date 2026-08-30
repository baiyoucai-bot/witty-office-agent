"""线索仲裁：分句内比字面长短，分句间取并集，加 `+`/`-` 两个算子。

改前 `_match_cues` 让每一格各自扫全句，命中就算，谁也不让谁。两个后果：
  一句占几格 —— `下次不要每条都加铺垫` 同时进 prefs 和 followups，一条偏好吃两格预算
                （`working_set` 每格 12 条）。
  记错格     —— `五防校验不能通过是因为双位置不一致` 被 `不能` 捞进 constraints；那是
                常驻格、豁免衰减，错进去就不出来。
真实位置实测：调参集 12/23 记错或多占（2 条污染常驻格），留出集 7/15（2 条污染）。

分句是关键，也是这一份的第一批断言：`我是自动化专责，下次的点表核对归我` 真是两件事，
两格都该进；而 `以后都叫我老王，联系我走内网邮箱` 里的 `联系` 只是动词，跟 people 无关。
线索只在自己那个分句里较量，两种情形就自然分开——单分句的句子行为与改前完全一致。

字面长短当「有多具体」的度量，不是拍脑袋：配置里 `下次不要`（prefs）本来就比
`下次`（followups）长，长的那条是写给更窄的情形的。平手则都进——硬挑一个是瞎猜。

**机制断言用合成线索表**（`dataclasses.replace`），这样改配置词不会连带弄红一堆机制测试；
**四处收窄单独用真配置断言**，那几处是这一轮的成果，得有测试盯着别被改回去。

放宽方向才是危险方向（放宽 = 凭空多记 + 重新污染常驻格），所以每条否决都单独压住：
算子两侧要有实字、护词要同分句、忌词一出现就不算、槽位之间不比长短。
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from witty_agent.memory import _bullets, topic_body
from witty_agent.memory_config import load_memory_settings
from witty_agent.memory_harvest import (
    _clauses,
    _cue_weight,
    _match_cues,
    _split_cue,
    _winning_cells,
    harvest_assistant_notes,
    harvest_user_text,
)
from witty_agent.memory_prefs import parse_pref_line

CELLS = (
    "who",
    "goals",
    "constraints",
    "prefs",
    "domain",
    "assets",
    "people",
    "decisions",
    "followups",
)


class SplitCueTest(unittest.TestCase):
    """算子解析。两侧都要有实字，否则按字面——`sk-` 这类线索不能被当成算子。"""

    def test_plain_cue_has_no_operator(self) -> None:
        self.assertEqual(_split_cue("下次"), ("下次", "", ""))

    def test_require_and_forbid_are_parsed(self) -> None:
        self.assertEqual(_split_cue("我在+做|负责"), ("我在", "+", "做|负责"))
        self.assertEqual(_split_cue("不能-是因为"), ("不能", "-", "是因为"))

    def test_trailing_operator_stays_literal(self) -> None:
        """`sk-` 是别处真实存在的线索形状；以算子字符收尾时必须按字面读。"""
        self.assertEqual(_split_cue("sk-"), ("sk-", "", ""))
        self.assertEqual(_split_cue("-开头"), ("-开头", "", ""))

    def test_only_the_first_operator_counts(self) -> None:
        """一条线索只支持一个算子；后面的算子字符归给参数部分。"""
        self.assertEqual(_split_cue("核心+甲-乙"), ("核心", "+", "甲-乙"))


class CueWeightTest(unittest.TestCase):
    """权重就是匹配到的字面长度；0 表示不算命中。"""

    def test_plain_cue_weighs_its_length(self) -> None:
        self.assertEqual(_cue_weight("下次", "下次把点表补全"), 2)
        self.assertEqual(_cue_weight("下次不要", "下次不要加铺垫"), 4)

    def test_missing_cue_weighs_zero(self) -> None:
        self.assertEqual(_cue_weight("下次", "把点表补全"), 0)

    def test_require_needs_the_guard_word(self) -> None:
        self.assertEqual(_cue_weight("我在+负责", "我在等厂家回话"), 0)
        self.assertEqual(_cue_weight("我在+负责", "我在地调负责远动"), 2 + 2)

    def test_require_counts_the_longest_guard(self) -> None:
        """护词算进长度里，`我在+专责` 才压得住只有 2 字的对手。"""
        self.assertEqual(_cue_weight("我在+管|专责", "我在自动化科室管远动"), 2 + 1)
        self.assertEqual(_cue_weight("我在+管|专责", "我在地调做自动化专责"), 2 + 2)

    def test_forbid_blocks_on_any_listed_word(self) -> None:
        self.assertEqual(_cue_weight("不能-是因为|一般是", "遥控出口不能动"), 2)
        self.assertEqual(_cue_weight("不能-是因为|一般是", "校验不能通过是因为双位置不一致"), 0)
        self.assertEqual(_cue_weight("不能-是因为|一般是", "遥测不能刷新一般是通道断了"), 0)

    def test_empty_core_never_matches(self) -> None:
        self.assertEqual(_cue_weight("", "随便一句话"), 0)


class ClauseTest(unittest.TestCase):
    def test_splits_on_commas_and_semicolons(self) -> None:
        self.assertEqual(_clauses("我是专责，管远动；也管五防"), ["我是专责", "管远动", "也管五防"])

    def test_unsplittable_line_returns_itself(self) -> None:
        self.assertEqual(_clauses("我在地调负责远动运维"), ["我在地调负责远动运维"])

    def test_a_line_of_only_separators_returns_itself(self) -> None:
        """全是逗号时 `parts` 是空的，不能返回空列表——那会让整句悄悄消失。"""
        self.assertEqual(_clauses("，，"), ["，，"])


class WinningCellsTest(unittest.TestCase):
    """仲裁本身。用合成线索表，免得配置改词把机制测试带红。"""

    def setUp(self) -> None:
        self.settings = replace(
            load_memory_settings(),
            cues={
                "prefs": ("下次不要",),
                "followups": ("下次",),
                "people": ("联系人", "班组"),
                "who": ("我是", "我在+管|负责|班组"),
                "goals": ("这次要",),
            },
        )

    def won(self, line: str) -> list[str]:
        return sorted(_winning_cells(line, self.settings))

    def test_longer_cue_wins_inside_one_clause(self) -> None:
        """`下次不要` 比 `下次` 长，prefs 独得——改前两格各记一条。"""
        self.assertEqual(self.won("下次不要每条都加铺垫"), ["prefs"])

    def test_short_cue_still_wins_when_alone(self) -> None:
        self.assertEqual(self.won("下次把点表差异导成 csv"), ["followups"])

    def test_a_cue_cannot_claim_a_neighbouring_clause(self) -> None:
        """`我是专责` 在前半句，`下次…` 在后半句——两件事，两格都该进。"""
        self.assertEqual(self.won("我是自动化专责，下次的点表核对归我"), ["followups", "who"])

    def test_losing_cue_in_another_clause_is_not_revived(self) -> None:
        """后半句自己没赢家的时候，别把前半句的赢家算成后半句的。"""
        self.assertEqual(self.won("下次不要加铺垫，直接给结论"), ["prefs"])

    def test_equal_weights_both_win(self) -> None:
        """`我是`(2) 和 `这次要`(3) 不同分句，各自赢各自那半句。"""
        self.assertEqual(self.won("这次要核对点表，我是负责人"), ["goals", "who"])

    def test_equal_weights_inside_one_clause_both_win(self) -> None:
        """同一分句里平手也都进——真配置里这种句子到得了。

        `班组下次汇报带附件` 里 people 的 `班组` 和 followups 的 `下次` 都是 2 字，
        谁也不比谁具体。这时挑一个是瞎猜，而挑错的那半是常驻格就更贵。
        """
        settings = replace(self.settings, cues={"people": ("班组",), "followups": ("下次",)})
        self.assertEqual(sorted(_winning_cells("班组下次汇报带附件", settings)), ["followups", "people"])

    def test_guard_word_lets_a_short_cue_outweigh_a_rival(self) -> None:
        """`我在+班组` 记 2+2=4，压过同分句里 people 的 `班组`(2)。"""
        self.assertEqual(self.won("我在班组里排值班表"), ["who"])

    def test_no_cue_means_no_cell(self) -> None:
        self.assertEqual(self.won("遥信抖动一般是接点接触不良"), [])


class MatchCuesShapeTest(unittest.TestCase):
    """`_match_cues` 的返回形状是下游 `cells_hit` 顺序的来源，重写时不能变。"""

    def setUp(self) -> None:
        self.settings = replace(
            load_memory_settings(),
            cues={"who": ("我是",), "followups": ("下次",), "people": ("联系人",)},
        )

    def test_cell_keys_follow_config_order_not_win_order(self) -> None:
        """先出现的是 followups，但配置里 who 在前——键序必须跟配置。"""
        matched = _match_cues(["下次把五防库补全", "我是远动专责"], self.settings)
        self.assertEqual(list(matched), ["who", "followups"])

    def test_lines_keep_sentence_order_and_duplicates(self) -> None:
        """同一句说两遍就该留两条：重写成按句子遍历时最容易在这儿悄悄去重。"""
        matched = _match_cues(["下次把五防库补全", "下次把五防库补全"], self.settings)
        self.assertEqual(matched["followups"], ["下次把五防库补全"] * 2)

    def test_empty_input_yields_empty_mapping(self) -> None:
        self.assertEqual(_match_cues([], self.settings), {})

    def test_prefs_keeps_its_worth_keeping_exemption(self) -> None:
        """prefs 短句过不了碎片下限，这条豁免是既有设计，仲裁不能顺手抹掉。"""
        settings = replace(self.settings, cues={"prefs": ("叫我",), "who": ("我是",)})
        matched = _match_cues(["叫我老王"], settings)
        self.assertEqual(list(matched), ["prefs"])


class ConfiguredNarrowingTest(unittest.TestCase):
    """四处收窄读真配置。这几处是这一轮的成果，得有测试盯着别被改回去。"""

    def setUp(self) -> None:
        self.settings = load_memory_settings()

    def won(self, line: str) -> list[str]:
        return sorted(_winning_cells(line, self.settings))

    def test_bare_role_words_are_no_longer_cues_on_their_own(self) -> None:
        """收窄的四条线索都不能再以裸词形式待在表里。"""
        self.assertNotIn("我在", self.settings.cues["who"])
        self.assertNotIn("不能", self.settings.cues["constraints"])
        self.assertNotIn("联系", self.settings.cues["people"])
        self.assertNotIn("就用这个", self.settings.cues["decisions"])

    def test_wo_zai_needs_a_role_word(self) -> None:
        for line in ("我在等厂家回话", "我在城东变现场", "我在共享盘上放了一份台账"):
            with self.subTest(line=line):
                self.assertEqual(self.won(line), [], line)
        self.assertEqual(self.won("我在地调负责远动运维"), ["who"])
        self.assertEqual(self.won("我在地区调度中心做自动化专责"), ["who"])

    def test_bu_neng_with_a_cause_is_not_a_red_line(self) -> None:
        self.assertEqual(self.won("五防校验不能通过是因为双位置不一致"), [])
        self.assertEqual(self.won("遥测不能刷新一般是通道断了"), [])
        self.assertEqual(self.won("生产环境的遥控出口不能动"), ["constraints"])
        self.assertEqual(self.won("改定值不能跳过调度许可"), ["constraints"])

    def test_lianxi_as_a_verb_is_not_a_contact(self) -> None:
        for line in ("这事要联系厂家确认", "联系了厂家还没回", "联系不上现场就先记着"):
            with self.subTest(line=line):
                self.assertNotIn("people", self.won(line), line)
        self.assertEqual(self.won("运维班的联系人是老陈"), ["people"])

    def test_jiu_yong_no_longer_needs_the_demonstrative(self) -> None:
        self.assertEqual(self.won("就用 104 规约"), ["decisions"])
        self.assertEqual(self.won("点表就用共享盘那份"), ["decisions"])
        self.assertEqual(self.won("就用这个模板"), ["decisions"])

    def test_no_configured_cue_carries_a_stray_operator_char(self) -> None:
        """算子是新语法：现有线索里要是本来就带 `+`/`-`，含义会被悄悄改写。"""
        for table in (self.settings.cues, self.settings.assistant_cues):
            for cell_id, cues in table.items():
                for cue in cues:
                    core, op, extra = _split_cue(cue)
                    if not op:
                        continue
                    with self.subTest(cell=cell_id, cue=cue):
                        self.assertTrue(core.strip(), cue)
                        self.assertTrue(
                            all(word.strip() for word in extra.split("|")),
                            f"{cell_id} 的 {cue} 里有空的护词/忌词，那一项永远不命中",
                        )


class PrefSlotShadowTest(unittest.TestCase):
    """`pref_retract` 的 `别再` 是 `pref_slots.avoid` 里 `别再用` 的前缀。

    改前先抠作废词再找槽位，`别再用自动生成的点表` 被拆成「无槽位的作废」——而无槽位作废在
    `upsert_pref_bullets` 里既不写入（`if retract: continue`）也删不掉（没有同槽可比），
    整条偏好凭空消失。同样的意思换个说法 `下次不要用…` 却能进 avoid 槽。
    """

    def setUp(self) -> None:
        self.settings = load_memory_settings()

    def parse(self, line: str) -> tuple[str, bool]:
        slot, retract, _value = parse_pref_line(line, self.settings)
        return slot, retract

    def test_avoid_cue_beats_the_retract_prefix_it_contains(self) -> None:
        self.assertEqual(self.parse("别再用自动生成的点表"), ("avoid", False))
        self.assertEqual(self.parse("下次别再用自动生成的点表"), ("avoid", False))

    def test_same_meaning_two_wordings_land_in_the_same_slot(self) -> None:
        """这两句是同一条指示，改前一条进 avoid、一条消失。"""
        self.assertEqual(
            self.parse("别再用自动生成的点表"), self.parse("下次不要用自动生成的点表")
        )

    def test_retract_plus_slot_still_replaces_the_old_value(self) -> None:
        """`改成` 不是任何槽位线索的子串，所以这句仍是「作废旧值 + 写新值」。"""
        self.assertEqual(self.parse("以后改成叫我老李"), ("address", True))

    def test_slotless_retract_is_untouched(self) -> None:
        self.assertEqual(self.parse("我不吃辣了"), ("", True))
        self.assertEqual(self.parse("我不喜欢长表格"), ("", True))

    def test_slots_are_ranked_by_config_order_not_length(self) -> None:
        """`以后都`(3) 比 `叫我`(2) 长，但这句说的是怎么称呼。

        槽位之间的先后是配置作者定的优先级，不能拿字面长度盖过去；长短只在同一槽位内部用。
        """
        self.assertEqual(self.parse("以后都叫我老王"), ("address", False))
        self.assertEqual(self.parse("请简称我老王"), ("address", False))
        self.assertEqual(self.parse("请用 Markdown 排表格"), ("habit", False))

    def test_longest_wins_inside_one_slot(self) -> None:
        """同槽内部取最长。真配置里同槽还没有互相包含的词，所以用合成表断言机制。

        变异测试里「同槽不取最长」漏网，就是因为真配置目前撞不上——那不是测试写松了，
        是这条规矩眼下没有生产用例。日后往同槽加了更细的词，这条断言就开始兜着了。
        """
        settings = replace(
            load_memory_settings(),
            pref_slots={"avoid": ("别再", "别再用"), "like": ("喜欢",)},
            pref_retract=("不再",),
        )
        _slot, _retract, value = parse_pref_line("别再用自动生成的点表", settings)
        self.assertEqual(value, "自动生成的点表")

    def test_no_retract_cue_is_nested_inside_another(self) -> None:
        """作废词表内部没有互相包含的项——有的话，`_longest_hit` 取长会改变现有判定。"""
        cues = load_memory_settings().pref_retract
        nested = [(a, b) for a in cues for b in cues if a != b and a in b]
        self.assertEqual(nested, [], f"作废词互相包含：{nested}")


class AssistantCueOperatorTest(unittest.TestCase):
    """算子语法必须两张线索表都认。

    `assistant_cues` 现在一条算子都没有，所以「助手侧退回字面匹配」这个变异漏网了。
    那不是测试写松：那条路眼下没有生产用例。但语法一旦是共用的，哪天在助手表里写了
    `核心+护词`，字面匹配会去找 `核心+护词` 这七个字，永远不命中，而且不报错。
    所以这里直接对着助手侧那张表断言算子生效。
    """

    def setUp(self) -> None:
        self.settings = replace(
            load_memory_settings(),
            assistant_cues={"decisions": ("就这样定+规约|范围",), "followups": ("待跟进",)},
        )

    def notes(self, text: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            harvest_assistant_notes(directory, text, settings=self.settings)
            return sorted(
                cell
                for cell in ("decisions", "followups")
                if any(bullet for bullet in _bullets(topic_body(directory, cell)))
            )

    def test_require_operator_works_on_the_assistant_table(self) -> None:
        self.assertEqual(self.notes("规约就这样定了，不再讨论。"), ["decisions"])

    def test_require_operator_also_withholds_on_the_assistant_table(self) -> None:
        """护词不在，就不该命中——字面匹配会在这一条上和算子版给出同样结果，
        所以真正区分两者的是上面那条：字面匹配根本找不到 `就这样定+规约|范围`。"""
        self.assertEqual(self.notes("这事就这样定了。"), [])


class ArbitrationEndToEndTest(unittest.TestCase):
    """真 `harvest_user_text`，判官不可用（这个部署没有 key，确定性路径就是生产行为）。"""

    def setUp(self) -> None:
        self.settings = load_memory_settings()

    def landed(self, text: str) -> list[str]:
        needle = text.rstrip("。！？")[:12]
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp)
            harvest_user_text(user_dir, text, settings=self.settings, judge_fn=None)
            return sorted(
                cell
                for cell in CELLS
                if any(needle in bullet for bullet in _bullets(topic_body(user_dir, cell)))
            )

    def test_one_preference_no_longer_occupies_two_cells(self) -> None:
        self.assertEqual(self.landed("下次不要每条都加铺垫。"), ["prefs"])

    def test_the_vanished_preference_is_stored_again(self) -> None:
        """收窄仲裁把 followups 那格让开之后，这句露出了 `parse_pref_line` 的老毛病。"""
        self.assertEqual(self.landed("下次别再用自动生成的点表。"), ["prefs"])

    def test_a_domain_fact_reaches_domain_instead_of_the_standing_cell(self) -> None:
        """`不能` 让开之后，这两句没被任何 cue 命中，交给结构判据认 domain。"""
        self.assertEqual(self.landed("五防校验不能通过是因为双位置不一致。"), ["domain"])
        self.assertEqual(self.landed("遥测不能刷新一般是通道断了。"), ["domain"])

    def test_real_red_lines_still_reach_constraints(self) -> None:
        for line in ("生产环境的遥控出口不能动。", "未经批准不能接入外网。", "改定值不能跳过调度许可。"):
            with self.subTest(line=line):
                self.assertEqual(self.landed(line), ["constraints"], line)

    def test_two_facts_in_one_sentence_still_reach_both_cells(self) -> None:
        """仲裁不是「只准进一格」：这两句确实各说了两件事。"""
        self.assertEqual(
            self.landed("我是远动专责，红线是不动遥控出口。"), ["constraints", "who"]
        )
        self.assertEqual(
            self.landed("下次核对完记得跟二次班对一遍，联系人是老陈。"),
            ["followups", "people"],
        )

    def test_verb_sentences_no_longer_land_anywhere(self) -> None:
        for line in ("这事要联系厂家确认。", "联系了厂家还没回。", "我在等厂家回话。", "我在城东变现场。"):
            with self.subTest(line=line):
                self.assertEqual(self.landed(line), [], line)

    def test_widened_decision_cue_catches_real_decisions(self) -> None:
        for line in ("就用 104 规约，不再讨论。", "规约就用 101，不改了。", "点表就用共享盘那份。"):
            with self.subTest(line=line):
                self.assertEqual(self.landed(line), ["decisions"], line)

    def test_structural_path_is_unaffected(self) -> None:
        """上一轮的 domain/assets 入口不受仲裁影响——它只看没被 cue 命中的句子。"""
        self.assertEqual(self.landed("遥信抖动一般是接点接触不良。"), ["domain"])
        self.assertEqual(self.landed("点表台账放在 //nas/dispatch/points/ 下面。"), ["assets"])


class KnownResidualTest(unittest.TestCase):
    """还没解决的两处，锁下来免得日后误以为已经修好。"""

    def setUp(self) -> None:
        self.settings = load_memory_settings()

    def won(self, line: str) -> list[str]:
        return sorted(_winning_cells(line, self.settings))

    def test_preferences_without_a_prefs_cue_still_land_in_followups(self) -> None:
        """`下次回答别用…` / `下次汇报按…` 是偏好，可 prefs 表里没有能命中的词。

        试过给 prefs 加 `别用`：调参集 +1，留出集 0，却把 `老台账别用了` /
        `别用生产环境试`（都不是偏好）拽进常驻格。词表是有限枚举，一个词换不来净收益
        就是白送的误收，所以没加。
        """
        self.assertEqual(self.won("下次回答别用自动生成的表格"), ["followups"])
        self.assertEqual(self.won("下次汇报按 Markdown 排"), ["followups"])

    def test_bu_neng_is_still_too_wide_without_a_cause_marker(self) -> None:
        """`老站的图纸不能跟现场对上` 是领域事实，但没有因果标记，忌词表拦不住。

        剩下的路是「否定 + 达成类补语」这种句型判据，不是往忌词表里加词。
        """
        self.assertEqual(self.won("老站的图纸不能跟现场对上"), ["constraints"])


if __name__ == "__main__":
    unittest.main()
