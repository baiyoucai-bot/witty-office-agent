"""判官跑不起来时，`domain` / `assets` 靠结构判据入格。

这两格是九宫格里唯一没有 cue 的（`config/memory.toml` 的 `[memory.cues]` 覆盖另外 7 格）。
不是遗漏——它们没有词法标记，硬编 `是` / `放在` 这类 cue 会让半个语料掉进 domain。原设计
让模型判官兜这两格，但判官要 API key；这个部署的端点返回 401，`_live_judge_allowed()`
恒假，于是 `_decide_leftover` 直接 `return []`，这两格在确定性路径上**一个入口都没有**。
实测 19 条领域/资产事实，进来 0 条。

结构判据钻的空子是位置：到得了 `_structural_leftover` 的句子
  · 已经过了 `_worth_keeping`（问句、派活、寒暄早没了）
  · 已经没被任何 cue 命中（不跟有 cue 的 7 格抢，错格问题碰不到）
剩下要分的只有「陈述事实」和「说该怎么做」：
  资产 = 有路径或文件名，且不在派活
  领域 = 泛化断言，或系动词断言（排掉话头词），且不在派活

留出集（先定判据、再看留出集，不回头调）：domain 认出 5/6、assets 4/4，误认各 0/14、0/16。
端到端跑真的 `harvest_user_text`：该进的 0/19 → 17/19。

这一份锁的重点是**否决项**——判据能放宽的方向都是往误收走，所以每条否决都得有测试压住：
话头词否决、义务否决、资产优先于领域、泛指词（`共享盘`）不算定位符。
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from witty_agent.memory import _bullets, ensure_lattice, topic_body
from witty_agent.memory_config import load_memory_settings
from witty_agent.memory_harvest import (
    _asserts_state,
    _decide_leftover,
    _is_imperative,
    _structural_leftover,
    harvest_user_text,
)

# 领域规律：泛化断言，或 `X 是 Y` 的状态断言。
DOMAIN_FACTS = (
    "五防校验失败通常是遥信双位置不一致。",
    "城东变的远动规约是 104。",
    "双位置遥信在 SOE 里是两条报文。",
    "遥测跳变一般是采样板的问题。",
    "定值区切换默认是第一区。",
    "遥信抖动一般是接点接触不良。",
    "线路保护的重合闸默认是一次。",
    "PT 断线告警通常伴随电压归零。",
    "调度主站的对时是 SNTP。",
)
# 资产：东西在哪、叫什么。都带路径或带扩展名的文件名。
ASSET_FACTS = (
    "点表台账放在共享盘 //nas/dispatch/points/ 下面。",
    "图纸都在 //nas/drawings/ 里。",
    "定值单模板叫 setting_v3.docx。",
    "定值单扫描件在 //nas/settings/2026/ 下。",
    "五防逻辑库导出来叫 fw_logic.db。",
    "遥信对照表放在 //nas/points/ 目录。",
)
# 话头词 + 是：给自己的判断或计划起头，长得像 `X 是 Y` 但不是领域规律。
TOPIC_LEAD_LINES = (
    "这次的重点是核对城东变的点表。",
    "结论是先按老规约跑一段。",
    "难点是老站没有电子版点表。",
    "我的想法是先把远动通道理清。",
    "眼下最急的是把五防库补全。",
    "打算是下周开始现场核对。",
    "问题是老站的图纸对不上。",
    "验收标准是遥信全对齐。",
    "现在最麻烦的是没人认那份老台账。",
)
# 派活句：义务落在「现在去做哪件事」上。有几条故意带路径或文件名，专门压资产判据。
TASK_LINES = (
    "月底前把 61850 的点表补全。",
    "这个月要把 //nas/dispatch/ 的点表核对完。",
    "第一步是把 //nas/points/ 那份导出来。",
    "把 fw_logic.db 备份一份放稳妥。",
    "这个月要把 61850 改造全部收尾。",
)
# 规程句：义务落在「事物该是什么样」上，是长期规矩，归红线。
# 这几条从前跟派活句挤在同一张表里、一起被扔掉——包括 `改 setting_v3.docx 必须两人复核`，
# 那条的旧注释自己写着「是红线不是资产」，可当时红线没有入口，于是整句丢了。
RULE_LINES = (
    "104 通道的遥控出口一律不许动。",
    "改 setting_v3.docx 必须两人复核。",
    "表格一律用 Markdown 排。",
    "定值单一律两人复核。",
)
# 一次性义务：带规程语气，说的却是这一回。红线是常驻格、不衰减，这类不能驻进去。
ONE_TIME_OBLIGATIONS = (
    "今天必须把定值单核对完。",
    "下周必须完成整改。",
    "8月25日前必须报送。",
    "这次必须一次验收通过。",
    "本次消缺必须当天闭环。",
)


def _cells(decided: list[tuple[str, str]]) -> list[str]:
    return [cell_id for cell_id, _ in decided]


class StructuralLeftoverTest(unittest.TestCase):
    """`_structural_leftover` 单独看：认得出，也拦得住。"""

    def setUp(self) -> None:
        self.settings = load_memory_settings()

    def decide(self, line: str) -> list[str]:
        return _cells(_structural_leftover([line], self.settings))

    def test_domain_facts_land_in_domain(self) -> None:
        for line in DOMAIN_FACTS:
            with self.subTest(line=line):
                self.assertEqual(self.decide(line), ["domain"], line)

    def test_asset_facts_land_in_assets(self) -> None:
        for line in ASSET_FACTS:
            with self.subTest(line=line):
                self.assertEqual(self.decide(line), ["assets"], line)

    def test_topic_lead_is_not_a_domain_fact(self) -> None:
        """`结论是…` / `难点是…` 不是领域规律——不排话头词，留出集误认 7/14。

        这些句子本属 goals/decisions，而那两格有 cue；到得了这儿说明 cue 没命中，
        所以放宽的后果不是「进错格」而是「凭空多一条领域要点」，比漏收更难查。
        """
        for line in TOPIC_LEAD_LINES:
            with self.subTest(line=line):
                self.assertEqual(self.decide(line), [], line)

    def test_task_lines_are_not_facts(self) -> None:
        """在派活的句子一条都不收，带路径的也不收。

        `把 fw_logic.db 备份一份` 是待办不是资产。定位符只说明句子里提到了东西，
        没说明这句在陈述它在哪。
        """
        for line in TASK_LINES:
            with self.subTest(line=line):
                self.assertEqual(self.decide(line), [], line)

    def test_rule_lines_land_in_constraints(self) -> None:
        """规程语气的长期规矩归红线，不再跟派活句一起被扔掉。

        这四条从前跟派活句共用一张义务表，于是 `遥控出口一律不许动`、`定值单一律两人复核`
        这种教科书式红线**一条都进不来**（实测规则句漏收 9/9、留出 5/5）。两家分开的依据是
        义务落在哪儿：落在事物该是什么样上是规矩，落在现在去做哪件事上是活儿。

        `改 setting_v3.docx 必须两人复核` 有文件名，仍归红线不归资产——它说的是「动这个
        文件要守什么规矩」。旧注释当时就写着「是红线不是资产」，只是红线那会儿没有入口。
        """
        for line in RULE_LINES:
            with self.subTest(line=line):
                self.assertEqual(self.decide(line), ["constraints"], line)

    def test_one_time_obligations_do_not_reach_constraints(self) -> None:
        """带规程语气但说的是这一回的，不驻红线格。

        红线是常驻格、不衰减，错进去就永远在提示里，比漏收贵得多。所以这条判据的
        放宽必须配一条收紧：认得出日期的交给时间线，`这次` / `本次` / `月底前` 这类
        落不到日期上的靠场合词挡住。两条护栏各自都不够——只留日期护栏漏 1/4，
        只留场合词护栏漏 2/4，两条都在才 0/4。
        """
        for line in ONE_TIME_OBLIGATIONS:
            with self.subTest(line=line):
                self.assertEqual(self.decide(line), [], line)

    def test_rule_modal_does_not_swallow_task_assignment(self) -> None:
        """派活标记比规程语气强：`今天必须把定值单核对完` 有 `必须` 也仍是活儿。

        这条锁的是两族的**优先级**，不是某个例子。倒过来的话一切带 `必须` 的派活句
        都会驻进红线格。
        """
        self.assertTrue(_is_imperative("今天必须把定值单核对完。"))
        self.assertTrue(_is_imperative("得先把老站的点表补齐。"))
        self.assertFalse(_is_imperative("站内五防主机与调度主站的点号必须一致。"))

    def test_topic_lead_beats_rule_modal(self) -> None:
        """`原则是必须两人复核` 既有话头词又有规程语气，那是在给自己的判断起头。

        跟 `_asserts_state` 排话头词是同一条理由，只是那条管系动词、这条管规程语气。
        """
        self.assertEqual(self.decide("原则是必须两人复核。"), [])
        self.assertEqual(self.decide("要求是遥信必须双位置。"), [])

    def test_assets_wins_when_both_match(self) -> None:
        """`主站配置备份是 backup_20260801.tar.gz。` 两条判据都命中，只能归一格。

        它是系动词断言，但说的是「东西叫啥」——那是资产。不定先后的话同一句会同时
        占 domain 和 assets 两个槽，等于一条事实吃两份预算。
        """
        self.assertEqual(self.decide("主站配置备份是 backup_20260801.tar.gz。"), ["assets"])

    def test_vague_place_words_are_not_locators(self) -> None:
        """`共享盘` / `档案室` 这类泛指不算定位符：派活句里一样多，认了等于没判据。

        `台账那份 Excel 在共享盘根目录` 因此收不进来——这是明知的漏，换来的是
        `这个月要把共享盘的点表核对完` 不会被当资产。
        """
        for line in ("台账那份 Excel 在共享盘根目录。", "老图纸那批在档案室的 3 号柜。"):
            with self.subTest(line=line):
                self.assertEqual(self.decide(line), [], line)

    def test_obligation_window_counts_words_not_characters(self) -> None:
        """`把 fw_logic.db 备份` 的动词离 `把` 有 13 个字符、2 个词。

        按字符量这条义务句会漏判，于是被当成资产收进来。跟 `_is_question` 的句尾
        窗口是同一个坑，用的是同一个 `_ASCII_RUN`——这条测试是那个共用的锚。
        """
        self.assertTrue(_is_imperative("把 fw_logic.db 备份一份放稳妥。"))
        self.assertTrue(_is_imperative("把 //nas/points/points.xlsx 导出来核对。"))
        self.assertFalse(_is_imperative("点表台账放在 //nas/dispatch/points/ 下面。"))

    def test_stative_verbs_without_copula_are_a_known_miss(self) -> None:
        """`61850 的 GOOSE 报文走独立网口。` 收不进来——没有系动词也没有泛化词。

        补一张状态动词表（`走` / `挂` / `接` …）能收进来，但那是个补不完的表，且
        `走` 在派活句里同样常见。留出集上这是唯一一条漏的领域事实，先认下来。
        """
        self.assertEqual(self.decide("61850 的 GOOSE 报文走独立网口。"), [])

    def test_copula_needs_a_subject_before_it(self) -> None:
        """`是` 前面得有实字。口语里 `我看，是这样` 的 `是` 不领断言。"""
        self.assertFalse(_asserts_state("是这样。"))
        self.assertTrue(_asserts_state("城东变的远动规约是 104。"))

    def test_generalizers_need_their_following_word(self) -> None:
        """泛化词必须带后续词：光认 `一般` / `默认` 会把作息习惯当成领域规律。

        `我一般下午在站里` / `我一般不加班` 都到得了这儿（`我一般` 不沾任何 cue，
        `我在` 那条 who 线索也对不上），说的是这个人的习惯，不是电网的规律。
        领域要点里混进作息，检索时会拿它去答技术问题。
        """
        for line in ("我一般下午在站里。", "我一般不加班。", "一般我不接现场活。"):
            with self.subTest(line=line):
                self.assertEqual(self.decide(line), [], line)
        # 带后续词的才算规律
        self.assertEqual(self.decide("遥信抖动一般是接点接触不良。"), ["domain"])
        self.assertEqual(self.decide("线路保护的重合闸默认是一次。"), ["domain"])

    def test_structural_path_also_serves_the_judge_off_setting(self) -> None:
        """`judge_leftover = false` 那支也得走结构判据，不能悄悄退回全丢。

        配置里现在是 `true`，所以这一支在生产上走不到——正因为走不到，没有测试压住
        它就会在下次改动里被顺手改回 `return []`，而那时没人会注意到。
        """
        off = replace(self.settings, judge_leftover=False)
        decided = _decide_leftover(["遥信抖动一般是接点接触不良。"], "", off, None)
        self.assertEqual(_cells(decided), ["domain"])


class StructuralLeftoverEndToEndTest(unittest.TestCase):
    """端到端：`harvest_user_text` 在判官不可用时真能把这两格填上。

    单测 `_structural_leftover` 只证判据对，证不了它接上了——此前那个分支
    `return []` 也一样「判据对」。这一份跑真的收割，读九宫格文件确认落地。
    """

    def setUp(self) -> None:
        self.settings = load_memory_settings()

    def harvest(self, text: str) -> dict[str, list[str]]:
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp)
            ensure_lattice(user_dir, self.settings)
            # judge_fn=None 且端点无 key，走的正是 `_live_judge_allowed()` 为假那一支
            harvest_user_text(user_dir, text, settings=self.settings, judge_fn=None)
            return {
                cell: _bullets(topic_body(user_dir, cell))
                for cell in ("domain", "assets", "constraints", "followups")
            }

    def test_domain_fact_reaches_the_cell(self) -> None:
        cells = self.harvest("遥信抖动一般是接点接触不良。")
        self.assertTrue(any("接点接触不良" in line for line in cells["domain"]), cells["domain"])

    def test_asset_fact_reaches_the_cell(self) -> None:
        cells = self.harvest("遥信对照表放在 //nas/points/ 目录。")
        self.assertTrue(any("//nas/points/" in line for line in cells["assets"]), cells["assets"])

    def test_obligation_does_not_reach_domain_or_assets(self) -> None:
        """带文件名的红线不能落进资产格。"""
        cells = self.harvest("改 setting_v3.docx 必须两人复核。")
        self.assertEqual([line for line in cells["assets"] if "setting_v3" in line], [])
        self.assertEqual([line for line in cells["domain"] if "setting_v3" in line], [])

    def test_question_still_cannot_get_in(self) -> None:
        """结构判据不能绕过 `_worth_keeping`：`点表放在 //nas/ 哪个目录。` 带路径，但是问句。"""
        cells = self.harvest("点表放在 //nas/dispatch/ 哪个目录。")
        self.assertEqual([line for line in cells["assets"] if "哪个目录" in line], [])


if __name__ == "__main__":
    unittest.main()
