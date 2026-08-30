"""收割前门的两条判据：这句是在提问/派活，还是只是句子里带了个疑问词或动词字。

`_worth_keeping` 有三个身份，所以这两条判据错一次要赔三次：
1. `_match_cues` 的入口——线索命中了也得过这关；
2. `_keep_decided_line` 对**模型判官输出**再过一遍，判官判对了格子也照样扔；
3. `scrub_transient_domain` 的**保留判据**——它把领域要点里不过关的条目删掉。
第 3 条最要命：误判不是「这条没收」，是「已经记住的条目下一轮被抹掉」。

此前一见疑问词就算提问、一见动词字打头就算派活，于是这些句子进不来也留不下：
`不管多少次遥控都要先报备`（红线）、`这就是为什么我们不用自动生成的点表`（领域事实）、
`改定值必须先走调度许可`（红线，`改` 是动名词不是动词）、`查线记录归二次班保管`。
实测：25 条耐久事实被挡 8 条；判官判对格子又被扔 6/14；已入格又被 scrub 抹掉 5/10。
改后依次是 0/25、1/14、0/10，同时 18 条噪音一条没漏。

判据形状（按留出集选的，不是对着调参集调的）：
- 提问 = 正反问/问号（无条件）→ 句末 `吗/呢` → 陈述框架否决 → 疑问词站句首（任指除外）
  或落在句尾 6 字内。
- 派活 = 明说请托（`请`/`帮我`…）→ 双字动词打头 → 单字动词 + 量词/趋向补语。
句尾窗口 4~8 字实测是平的，取 6；3 字会漏 `五防逻辑该怎么改`。

句首那一段是后补的。只有句尾窗口时，判据等于假定问句总把疑问词放在末尾，于是同一个问题
换个语序结论就翻：`数字化审计是什么` 判对，`什么是数字化审计` 判错——差别只在句子多一个字，
句首的疑问词掉出了窗口。真库（`~/.witty/…/memory/user`）里被当成事实存下来的问句全是这个
形状。实测问句落库 8/11 → 0/11，同时耐久事实 0/10 一条没误挡。顺序有讲究：框架词并不都站
句首（`为什么变电站会不一致，原因是双位置` 的 `原因是` 在后半句），所以句首判据必须排在
框架否决后面。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from witty_agent.memory import _bullets, ensure_lattice, topic_body, write_topic
from witty_agent.memory_config import load_memory_settings
from witty_agent.memory_harvest import (
    _is_question,
    _looks_like_task,
    _worth_keeping,
    harvest_user_text,
    scrub_transient_domain,
)

# 陈述句里嵌着疑问词——疑问词在句中，或者被 `不管 / 搞清楚 / 原因是` 这类框架引住。
DECLARATIVE_WITH_QUESTION_WORD = (
    "不管多少次遥控都要先报备。",
    "无论多少个间隔都要逐个核对。",
    "我要搞清楚点表为什么会不一致。",
    "我得弄清楚为什么遥测会跳变。",
    "这就是为什么我们不用自动生成的点表。",
    "为什么会不一致，原因是双位置。",
    "多少个间隔都得逐个核对，不能抽检。",
    "不一致的原因是什么我已经查明了。",
    "这批遥信怎么配都不影响五防逻辑。",
    "多少年前的老图纸都还留着。",
)
# 真提问——疑问词落在句尾，或者是正反问。
REAL_QUESTIONS = (
    "五防校验失败一般是什么原因。",
    "点表台账放在哪里。",
    "这个规约怎么配。",
    "遥测跳变是什么原因。",
    "这个定值单在哪份文件里。",
    "五防逻辑该怎么改。",
    "要不要把差异导出来。",
    "是不是要先报调度。",
    "这份台账你读过吗。",
    "这批点表核对完了吗。",
)
# 疑问词站句首的真提问。中文两头都问，`数字化审计是什么` 和 `什么是数字化审计` 是同一个
# 问题——上面那张表全是句尾形状，所以句首这一路漏了很久也没人看见。
HEAD_QUESTIONS = (
    "什么是数据治理。",
    "哪些台区要做农网改造。",
    "哪个班组管抄核收。",
    "怎么申请业扩报装。",
    "怎样编制反措计划。",
    "如何编制检修计划。",
    "为什么反措要提前下发。",
    "为何这批遥信要重配。",
    "谁负责变电站施工图会签。",
    "多少个间隔要重新核对。",
)
# 任指：疑问词 + `都/也`，说「全都」不是在问。`都` 跟疑问词能隔很远，所以判据不能设距离窗口。
ANY_REFERENT_DECLARATIVES = (
    "什么资料都要归档。",
    "什么定值单也不许私自改。",
    "哪个间隔都得逐个核对。",
    "谁来抄核收都要先报备。",
    "多少年前的施工图都还留着。",
    "怎么配都不影响五防逻辑。",
)
# 动名词打头的陈述句——`改` / `查` / `读` / `写` / `看` 是名词的一部分，不是在派活。
DECLARATIVE_WITH_VERB_LEAD = (
    "改定值必须先走调度许可。",
    "改造范围以初设批复为准。",
    "改批次的时候以调度令为准。",
    "查线记录归二次班保管。",
    "查评报告归安监科归档。",
    "读数偏差在 0.5% 以内算正常。",
    "写字楼那个专变是双电源。",
    "看图纸的规矩是先看一次系统图。",
)
# 真派活——明说请托，或动词带上量词/趋向补语。
REAL_TASKS = (
    "帮我把这份点表和台账比一遍。",
    "帮我核对一下这批遥信。",
    "请生成一份本周的核对报告。",
    "请把差异整理成表格。",
    "麻烦看下这个五防逻辑。",
    "先读一下 config 目录再说。",
    "写个脚本把点表导出来。",
    "分析一下这批 SOE。",
    "总结一下今天的核对结果。",
    "生成一份点表差异清单。",
    "再查一遍那个遥信双位置。",
    "看下这个点表对不对。",
)


class QuestionShapeTest(unittest.TestCase):
    """疑问词的**位置**决定它是不是在提问，光看有没有不行。"""

    def test_declarative_sentences_with_question_words_are_not_questions(self) -> None:
        for line in DECLARATIVE_WITH_QUESTION_WORD:
            with self.subTest(line=line):
                self.assertFalse(_is_question(line), line)

    def test_real_questions_are_still_caught(self) -> None:
        for line in REAL_QUESTIONS:
            with self.subTest(line=line):
                self.assertTrue(_is_question(line), line)

    def test_question_mark_and_a_not_a_beat_the_declarative_frame(self) -> None:
        """`不管` 之类的框架否决只压得住句中的疑问词，压不住正反问和问号。

        否则 `不管能不能通过都要报备` 这种反问句式会把 `不管 X 是不是 Y？` 一起放进来。
        """
        self.assertTrue(_is_question("不管怎么配，是不是都要报调度。"))
        self.assertTrue(_is_question("无论多少个间隔，要核对到什么时候？"))

    def test_final_particle_beats_the_declarative_frame(self) -> None:
        """句末 `吗/呢` 是问句最硬的标记，框架否决也不能压过它。"""
        self.assertTrue(_is_question("不管多少次，都要报备吗。"))
        self.assertTrue(_is_question("这个能改吗，还是要报批。"))

    def test_particle_inside_a_word_is_not_a_question(self) -> None:
        """`吗/呢` 只在句末（或逗号前）才算语气词，词里撞上不算。"""
        self.assertFalse(_is_question("尼呢边那台机器是备用的。"))


class HeadQuestionShapeTest(unittest.TestCase):
    """疑问词站句首也是提问，而且跟句子长短无关。

    改前只有句尾窗口那一条判据，等于假定问句总把疑问词放在末尾。于是同一个问题换个语序
    结论就翻：`数字化审计是什么` 判对，`什么是数字化审计` 判错。实测真库里被当成事实存下来
    的问句全是句首形状（探针：问句落库 8/11）。
    """

    def test_head_position_questions_are_caught(self) -> None:
        for line in HEAD_QUESTIONS:
            with self.subTest(line=line):
                self.assertTrue(_is_question(line), line)

    def test_verdict_does_not_depend_on_sentence_length(self) -> None:
        """句子变长不能让问句变成陈述句。这是改前那个 bug 的**病理**，不是它的某个例子。

        句尾窗口量的是「疑问词离句尾多远」，句首的疑问词会随着句子变长掉出窗口。改前
        `什么是数据治理`（距句尾 5 词）判对、`什么是数字化审计`（6 词）判错——多一个字，
        同一个问题结论就反。所以这里不列具体句子，而是把同一个问头一路加长着断言。
        """
        for stem in ("什么是", "怎么做", "谁来管", "哪个班组管", "如何编制"):
            tail = ""
            for word in ("点表", "核对", "标准化", "验收", "流程"):
                tail += word
                with self.subTest(sentence=stem + tail):
                    self.assertTrue(_is_question(stem + tail), stem + tail)

    def test_any_referent_is_not_a_question(self) -> None:
        """任指（疑问词 + `都/也`）说的是「全都」，不是在问。"""
        for line in ANY_REFERENT_DECLARATIVES:
            with self.subTest(line=line):
                self.assertFalse(_is_question(line), line)

    def test_any_referent_marker_can_sit_far_from_the_question_word(self) -> None:
        """`都` 跟疑问词能隔很远，任指判据不能设距离窗口。

        `多少年前的施工图都还留着` 里隔了 6 个字。设了窗口这句就被判成提问，而
        `_worth_keeping` 兼作 `scrub_transient_domain` 的保留判据——判错不只是不收，
        是把已经存进库的条目抹掉。
        """
        self.assertFalse(_is_question("多少年前的施工图都还留着。"))
        self.assertFalse(_is_question("哪一年建的变电站都得建档。"))

    def test_any_referent_does_not_cross_a_clause_boundary(self) -> None:
        """跨逗号的 `都` 是后半句的事，遮不住前半句在提问。"""
        self.assertTrue(_is_question("什么是数据治理，这些我都得先弄明白"))

    def test_why_plus_all_is_still_a_question(self) -> None:
        """`为什么/为何` 没有任指用法：它们问原因，后面跟 `都` 照样是提问。

        把它们收进任指表就会放跑 `为什么都要报备` 这类真问句。
        """
        self.assertTrue(_is_question("为什么都要先报调度"))
        self.assertTrue(_is_question("为何这些间隔都要重新核对"))

    def test_declarative_frame_beats_a_head_question_word(self) -> None:
        """框架词不都站句首，所以句首判据必须排在框架否决**后面**。

        `为什么变电站会不一致，原因是双位置` 的 `原因是` 在后半句，疑问词照样占着句首。
        句首判据排前面的话这句被判成提问——它是领域事实，判错等于把它从库里洗掉。
        """
        self.assertFalse(_is_question("为什么变电站会不一致，原因是双位置"))
        self.assertFalse(_is_question("为什么遥测会跳变，是因为死区设小了"))

    def test_head_alternative_word_is_a_decision_not_a_question(self) -> None:
        """句首的 `还是` 是拍板（`还是按老规约跑`），不是选择问，所以不进句首表。"""
        self.assertFalse(_is_question("还是按老规约跑一段"))
        self.assertFalse(_is_question("还是用共享盘那份点表"))

    def test_who_counts_at_both_ends(self) -> None:
        """`谁` 改前根本不在疑问词表里，两头都漏。"""
        self.assertTrue(_is_question("谁审核可研报告"))
        self.assertTrue(_is_question("这次业扩报装谁负责"))

    def test_how_written_the_other_way_counts_at_both_ends(self) -> None:
        """`怎样` 是 `怎么` 的同义写法，改前只收了后者。"""
        self.assertTrue(_is_question("怎样编制反措计划"))
        self.assertTrue(_is_question("检修计划该怎样编制"))


class TaskShapeTest(unittest.TestCase):
    """派活得有派活的形状：光是动词字打头不算。"""

    def test_verb_initial_statements_are_not_tasks(self) -> None:
        for line in DECLARATIVE_WITH_VERB_LEAD:
            with self.subTest(line=line):
                self.assertFalse(_looks_like_task(line), line)

    def test_real_tasks_are_still_caught(self) -> None:
        for line in REAL_TASKS:
            with self.subTest(line=line):
                self.assertTrue(_looks_like_task(line), line)

    def test_explicit_request_lead_wins_regardless_of_verb(self) -> None:
        """`请`/`帮我` 打头就是派活，动词在不在表里都一样——动词表永远补不全。"""
        self.assertTrue(_looks_like_task("帮我把这两份台账比一遍。"))
        self.assertTrue(_looks_like_task("麻烦捋一遍这批遥信。"))

    def test_single_char_verb_needs_a_quantifier(self) -> None:
        """单字动词得跟量词或趋向补语，否则分不开动名词。"""
        self.assertTrue(_looks_like_task("看下这个点表。"))
        self.assertFalse(_looks_like_task("看图纸的规矩是先看一次系统图。"))

    def test_directional_complement_is_not_a_quantifier(self) -> None:
        """`查出来` / `改完` / `写下来` 不算派活：它们照样能领一个名词短语。

        跟「单字动词裸奔」是同一个坑。收进量词表换不来任何一条真派活（真派活里
        `写个脚本把点表导出来` 靠的是 `个`），只白送三条误挡。
        """
        for line in ("查出来的差异归二次班保管。", "改完的点表以共享盘那份为准。", "写下来的口径以调度令为准。"):
            with self.subTest(line=line):
                self.assertFalse(_looks_like_task(line), line)
                self.assertTrue(_worth_keeping(line), line)


class WorthKeepingTest(unittest.TestCase):
    """两条判据合进 `_worth_keeping`：耐久事实一条不挡，噪音一条不漏。"""

    def test_durable_facts_pass(self) -> None:
        for line in (
            DECLARATIVE_WITH_QUESTION_WORD
            + DECLARATIVE_WITH_VERB_LEAD
            + ANY_REFERENT_DECLARATIVES
        ):
            with self.subTest(line=line):
                self.assertTrue(_worth_keeping(line), line)

    def test_questions_and_tasks_are_blocked(self) -> None:
        for line in REAL_QUESTIONS + HEAD_QUESTIONS + REAL_TASKS:
            with self.subTest(line=line):
                self.assertFalse(_worth_keeping(line), line)


class ScrubKeepsDurableFactsTest(unittest.TestCase):
    """第 3 个身份：`scrub_transient_domain` 用同一条判据决定**已存条目**去留。

    所以判据误判不是「没收进来」，是「记住了又被下一轮抹掉」——用户会看到记忆自己缩水。
    """

    def setUp(self) -> None:
        self.settings = load_memory_settings()
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "memory"
        self.dir.mkdir(parents=True)
        ensure_lattice(self.dir, self.settings)
        self.addCleanup(self._tmp.cleanup)

    def _seed_domain(self, lines: tuple[str, ...]) -> None:
        cell = self.settings.cell("domain")
        assert cell is not None
        write_topic(
            self.dir,
            "domain",
            description=cell.description or cell.title,
            body="\n".join(f"- {line}" for line in lines),
        )

    def test_stored_declaratives_survive_the_scrub(self) -> None:
        seeded = (
            DECLARATIVE_WITH_QUESTION_WORD
            + DECLARATIVE_WITH_VERB_LEAD
            + ANY_REFERENT_DECLARATIVES
        )
        self._seed_domain(seeded)
        dropped = scrub_transient_domain(self.dir, self.settings)
        rows = _bullets(topic_body(self.dir, "domain"))
        self.assertEqual(dropped, 0, rows)
        self.assertEqual(len(rows), len(seeded), rows)

    def test_scrub_removes_head_position_questions(self) -> None:
        """句首问句进了库也得洗出去：真库里的污染就是这么攒起来的。"""
        keeper = "城东变的远动规约是 104。"
        self._seed_domain((keeper, *HEAD_QUESTIONS))
        dropped = scrub_transient_domain(self.dir, self.settings)
        rows = _bullets(topic_body(self.dir, "domain"))
        self.assertEqual(dropped, len(HEAD_QUESTIONS), rows)
        self.assertEqual(rows, [keeper])

    def test_scrub_still_removes_questions_and_tasks(self) -> None:
        """反向也要锁：scrub 的本职是清掉误收的问句和派活，不能因为放宽判据就不清了。"""
        keeper = "城东变的远动规约是 104。"
        self._seed_domain((keeper, *REAL_QUESTIONS[:3], *REAL_TASKS[:3]))
        dropped = scrub_transient_domain(self.dir, self.settings)
        rows = _bullets(topic_body(self.dir, "domain"))
        self.assertEqual(dropped, 6, rows)
        self.assertEqual(rows, [keeper], rows)


class JudgeOutputGateTest(unittest.TestCase):
    """第 2 个身份：判官判对了格子，也要过这两条判据。

    有 API key 的部署里这是主路径，所以判据误判会让「模型明明判对了」的事实照样丢。
    """

    def setUp(self) -> None:
        self.settings = load_memory_settings()

    def _harvest_via_judge(self, sentence: str, cell: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "memory"
            directory.mkdir(parents=True)
            ensure_lattice(directory, self.settings)
            harvest_user_text(
                directory,
                sentence,
                settings=self.settings,
                judge_fn=lambda lines, _text, _settings: [(cell, line) for line in lines],
            )
            return _bullets(topic_body(directory, cell))

    def test_judged_declaratives_reach_their_cell(self) -> None:
        for sentence, cell in (
            ("不管多少次遥控都要先报备。", "constraints"),
            ("多少个间隔都得逐个核对，不能抽检。", "constraints"),
            ("这就是为什么我们不用自动生成的点表。", "domain"),
            ("为什么会不一致，原因是双位置。", "domain"),
            ("我要搞清楚点表为什么会不一致。", "goals"),
            ("查线记录归二次班保管。", "people"),
        ):
            with self.subTest(sentence=sentence):
                rows = self._harvest_via_judge(sentence, cell)
                self.assertTrue(any(sentence[:9] in row for row in rows), rows)

    def test_judge_cannot_smuggle_a_question_into_a_cell(self) -> None:
        """判官说要记也拦得住——这一关是双向的，不是只拦确定性路径。

        必须挑 `domain` **以外**的格子：`_keep_decided_line` 对 domain 另有一支
        `_worth_keeping`，用 domain 测等于测了那一支，把这一关拆掉也照样绿。
        `constraints` 是常驻格、不衰减，塞错了自己不会走，反而是最该拦住的那个。
        """
        for cell in ("constraints", "people", "domain"):
            with self.subTest(cell=cell):
                self.assertEqual(self._harvest_via_judge("点表台账放在哪里。", cell), [])
                self.assertEqual(self._harvest_via_judge("帮我核对一下这批遥信。", cell), [])


class CueHitsPassTheSameGateTest(unittest.TestCase):
    """线索命中的句子也要过判据——`harvest_user_text` 曾经自己抄了一遍写入、漏了这道门。

    `_match_cues` 里的 `_worth_keeping` 对 prefs 例外（短偏好过不了碎片下限），本意是
    放宽；但那份抄本连 `_keep_decided_line` 也没跑，于是**带 prefs 线索的问句直接落进
    prefs**——常驻格、不衰减、进去不走，用户会看到自己的偏好里躺着一句问话。实测 8 句
    这种里 7 句能驻进去。修法是让线索分支走 `_apply_decided`（那道门本来就在里面），
    不是再加一道新的。
    """

    # 带 prefs 线索、但根本不是偏好
    NOT_PREFS = (
        "以后叫我什么好呢。",
        "是不是都叫我老王。",
        "表格要不要一律用 Markdown。",
        "请用哪个规约比较好。",
        "叫我老王还是叫我老陈。",
        "我习惯用 vim 还是 nano 好呢。",
        "偏好这一栏填什么。",
        "默认用哪份台账。",
    )
    # 真偏好：加了门也必须照旧收进来
    REAL_PREFS = (
        "以后都叫我老王。",
        "我习惯用 vim 改配置。",
        "请用 Markdown 排表格。",
        "默认用共享盘那份台账。",
        "下次不要每条都加铺垫。",
        "我喜欢先看结论。",
        "叫我老王就行。",
    )

    def setUp(self) -> None:
        self.settings = load_memory_settings()

    def _prefs_after(self, sentence: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "memory"
            directory.mkdir(parents=True)
            ensure_lattice(directory, self.settings)
            harvest_user_text(directory, sentence, settings=self.settings)
            return _bullets(topic_body(directory, "prefs"))

    def test_questions_with_pref_cues_stay_out_of_the_standing_cell(self) -> None:
        for line in self.NOT_PREFS:
            with self.subTest(line=line):
                self.assertEqual(self._prefs_after(line), [], line)

    def test_real_preferences_still_land(self) -> None:
        for line in self.REAL_PREFS:
            with self.subTest(line=line):
                self.assertTrue(self._prefs_after(line), line)

    def test_imperative_preferences_survive_the_task_check(self) -> None:
        """偏好天生长成祈使句，所以派活判据在 prefs 这一格是按构造错的。

        `请用 Markdown 排表格` 跟 `请生成一份报告` 是同一个句型，分开只能靠线索词
        （`请用` 在 prefs 线索里、`请生成` 不在），不能靠句型。这一条锁的是那个例外。
        """
        self.assertTrue(self._prefs_after("请用 Markdown 排表格。"))
        self.assertFalse(_worth_keeping("请用 Markdown 排表格。"))

    def test_alternative_questions_are_questions(self) -> None:
        """`还是` 是唯一不带疑问标记的问句形状，靠句尾窗口跟陈述用法分开。"""
        self.assertTrue(_is_question("叫我老王还是叫我老陈。"))
        self.assertTrue(_is_question("用 104 还是 101 规约。"))
        self.assertFalse(_is_question("规约是 104 还是 101 得看现场。"))
        self.assertFalse(_is_question("不管是 104 还是 101 都要报备。"))

    def test_tail_window_counts_words_not_characters(self) -> None:
        """一个型号不该把疑问词挤出窗口：`104` / `IEC 61850` 各算一个词。

        `用 104 还是 101 规约` 里 `还是` 距句尾 7 个字符、2 个词——按字符量就漏了。
        电网的句子里到处是型号和路径，按字符量等于对这类句子单独放宽。
        """
        self.assertTrue(_is_question("用哪个 IEC 61850 版本。"))
        self.assertTrue(_is_question("点表放在 //nas/dispatch/ 哪个目录。"))
        self.assertFalse(_is_question("点表台账放在共享盘 //nas/dispatch/points/ 下面。"))

    def test_question_word_covers_unenumerated_measure_words(self) -> None:
        """`哪.` 一把收掉 `哪个/哪台/哪年`——枚举永远补不全，窗口负责挡住定语用法。"""
        self.assertTrue(_is_question("用哪台机器跑。"))
        self.assertTrue(_is_question("这个定值单在哪份文件里。"))
        self.assertFalse(_is_question("哪个间隔都得核对一遍。"))
        self.assertFalse(_is_question("无论哪种规约都要报备。"))


class TaxonomyPathBlocksQuestionsTest(unittest.TestCase):
    """分类那条路是真库被污染的路，得按真配置端到端锁一遍。

    `_is_question` 判对了不等于库是干净的：分类分支（`for item in settings.taxonomy`）自己
    调 `_worth_keeping`，而且命中之后**同时**写分类格和 `assets` 两处。真库里
    `什么是数字化审计` 就是这么在 `assets.md` 和 `digital.md` 各躺了一条。

    关键词用真 `config/memory.toml` 里的：句子必须真撞上关键词才走到判据跟前，撞不上的话
    「没落库」是因为没人认领，不是因为判对了——那种用例量不出任何东西。
    """

    def setUp(self) -> None:
        self.settings = load_memory_settings()
        keywords = {word for item in self.settings.taxonomy for word in item.keywords}
        # 下面的句子都拿这几个词做锚。真配置里没有了就该改测试，不是让它悄悄空跑。
        for word in ("数字化", "课件", "获客", "合同审查", "施工图"):
            self.assertIn(word, keywords, f"真配置里没有关键词 {word}，这条测试会空跑")

    def _cells_after(self, sentence: str) -> dict[str, list[str]]:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "memory"
            directory.mkdir(parents=True)
            ensure_lattice(directory, self.settings)
            harvest_user_text(
                directory,
                sentence,
                settings=self.settings,
                judge_fn=lambda *_args, **_kwargs: [],
            )
            landed = {}
            for path in sorted(directory.glob("*.md")):
                if path.stem in {"MEMORY", "profile"}:
                    continue
                rows = _bullets(topic_body(directory, path.stem))
                if rows:
                    landed[path.stem] = rows
            return landed

    def test_head_questions_with_taxonomy_keywords_do_not_land(self) -> None:
        for sentence in (
            "什么是数字化审计",
            "哪些班级要换新课件",
            "怎么提升获客转化",
            "如何准备合同审查",
            "谁负责施工图会签",
            "怎样准备合同审查",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(self._cells_after(sentence), {}, sentence)

    def test_facts_with_the_same_keywords_still_land(self) -> None:
        """反向锁：判据放宽了，带同样关键词的陈述句必须照旧收进来。

        少了这一条，把判据改成「见疑问词就挡」也能让上面那条绿。
        """
        for sentence, cell in (
            ("数字化审计的责任部门是信息通信公司", "digital"),
            ("课件改版的验收标准按教研组要求走", "teaching"),
            ("获客活动的资料包放在运营共享目录", "marketing-project"),
            ("施工图会签由基建部统一收口", "engineering-project"),
        ):
            with self.subTest(sentence=sentence):
                landed = self._cells_after(sentence)
                self.assertIn(cell, landed, sentence)
                self.assertIn("assets", landed, sentence)


if __name__ == "__main__":
    unittest.main()
