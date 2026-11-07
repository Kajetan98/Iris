package pl.iris.core

import java.time.Instant
import kotlin.math.abs
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.double
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * Zgodność z hostem (docs/spec.md, sekcja 14): każdy przypadek z memory_vectors.json, wygenerowanego z kodu
 * Pythona (python -m host.tests.make_android_vectors), musi dać w Kotlinie ten sam wynik.
 */
class VectorsTest {
    private val root: JsonObject = Json.parseToJsonElement(
        checkNotNull(javaClass.getResource("/memory_vectors.json")) { "brak memory_vectors.json" }.readText(),
    ).jsonObject
    private val rules = root.obj("rules")
    private val cfg = MemoryConfig()

    private fun JsonObject.obj(key: String) = getValue(key).jsonObject
    private fun JsonObject.str(key: String) = getValue(key).jsonPrimitive.content
    private fun JsonObject.strOrNull(key: String) = (this[key] as? JsonPrimitive)?.takeIf { it !is JsonNull }?.content
    private fun JsonElement.objs() = jsonArray.map { it.jsonObject }
    private fun JsonElement.strings() = jsonArray.map { it.jsonPrimitive.content }
    private fun time(s: String): Instant = IsoTime.parse(s)

    private fun close(expected: Double, actual: Double, what: String) =
        assertTrue(abs(expected - actual) <= 1e-12 * maxOf(1.0, abs(expected)), "$what: oczekiwano $expected, jest $actual")

    private fun concept(o: JsonObject) = Concept(
        o.str("key"), o.str("label"), o.getValue("strength").jsonPrimitive.double, time(o.str("last_used")),
        o.getValue("hits").jsonPrimitive.int, time(o.str("created_at")),
        o.obj("domains").mapValues { it.value.jsonPrimitive.int },
    )

    private fun link(o: JsonObject) = Link(o.str("a"), o.str("b"), o.getValue("strength").jsonPrimitive.double, time(o.str("last_used")))

    private fun extraction(o: JsonObject) = Extraction(
        o.getValue("concepts").jsonArray.map { val p = it.strings(); p[0] to p[1] }, o.str("domain"), o.str("source"),
    )

    private fun assertExtraction(expected: JsonElement, actual: Extraction?, what: String) {
        if (expected is JsonNull) return assertNull(actual, what)
        val e = extraction(expected.jsonObject)
        assertEquals(e, actual, what)
    }

    private fun assertConcepts(expected: JsonElement, actual: List<Concept>, what: String) {
        val e = expected.objs().map(::concept)
        assertEquals(e.map { it.copy(strength = 0.0) }, actual.map { it.copy(strength = 0.0) }, what)
        e.zip(actual).forEach { (x, y) -> close(x.strength, y.strength, "$what ${x.key}") }
    }

    private fun assertLinks(expected: JsonElement, actual: List<Link>, what: String) {
        val e = expected.objs().map(::link)
        assertEquals(e.map { it.copy(strength = 0.0) }, actual.map { it.copy(strength = 0.0) }, what)
        e.zip(actual).forEach { (x, y) -> close(x.strength, y.strength, "$what ${x.a}-${x.b}") }
    }

    @Test
    fun `concepts prompt and defaults equal the host`() {
        assertEquals(root.str("concepts_prompt"), CONCEPTS_PROMPT)
        val d = root.obj("defaults")
        val m = d.obj("memory")
        assertEquals(m.getValue("enabled").jsonPrimitive.boolean, cfg.enabled)
        assertEquals(m.str("infer"), cfg.infer.name.lowercase())
        assertEquals(m.getValue("half_life_days").jsonPrimitive.double, cfg.halfLifeDays)
        assertEquals(m.getValue("initial").jsonPrimitive.double, cfg.initial)
        assertEquals(m.getValue("gain_concept").jsonPrimitive.double, cfg.gainConcept)
        assertEquals(m.getValue("gain_link").jsonPrimitive.double, cfg.gainLink)
        assertEquals(m.getValue("gain_recall").jsonPrimitive.double, cfg.gainRecall)
        assertEquals(m.getValue("forget_below").jsonPrimitive.double, cfg.forgetBelow)
        assertEquals(m.getValue("prune_every_s").jsonPrimitive.double, cfg.pruneEveryS)
        assertEquals(m.getValue("max_concepts").jsonPrimitive.int, cfg.maxConcepts)
        assertEquals(m.getValue("recall_k").jsonPrimitive.int, cfg.recallK)
        assertEquals(m.getValue("answer_window_s").jsonPrimitive.double, cfg.answerWindowS)
        assertEquals(m.getValue("feedback").jsonPrimitive.boolean, cfg.feedback)
        assertEquals(m.getValue("feedback_down").jsonPrimitive.double, cfg.feedbackDown)
        val l = d.obj("llm")
        val llm = LlmConfig()
        assertEquals(l.getValue("max_tokens").jsonPrimitive.int, llm.maxTokens)
        assertEquals(l.getValue("timeout_s").jsonPrimitive.double, llm.timeoutS)
        assertEquals(l.getValue("retries").jsonPrimitive.int, llm.retries)
        assertEquals(l.getValue("history_turns").jsonPrimitive.int, llm.historyTurns)
        assertEquals(l.str("send_memory"), llm.sendMemory.name.lowercase())
        assertEquals(l.getValue("tools").jsonPrimitive.boolean, llm.tools)
        assertEquals(l.getValue("max_tool_rounds").jsonPrimitive.int, llm.maxToolRounds)
        assertEquals(l.str("effort"), llm.effort)
        assertEquals(l.getValue("thinking_headroom_tokens").jsonPrimitive.int, llm.thinkingHeadroomTokens)
        assertEquals(l.getValue("fallbacks").jsonPrimitive.boolean, llm.fallbacks)
        assertEquals(d.getValue("sensitive_words").strings(), SENSITIVE_STEMS)
        assertEquals(d.getValue("stop_words").strings(), MEMORY_STOP_WORDS)
        assertEquals(d.obj("domain_words").mapValues { it.value.strings() }, DOMAIN_WORDS)
        assertEquals(d.getValue("domain_words").jsonObject.keys.toList(), DOMAIN_WORDS.keys.toList())
        assertEquals(
            d.getValue("domains").objs().map { MemoryDomain(it.str("id"), it.str("name"), it.str("info"), it.getValue("rgb").jsonArray.map { v -> v.jsonPrimitive.int }) },
            MEMORY_DOMAINS,
        )
    }

    @Test
    fun `extraction rules`() {
        for (c in rules.getValue("stem").objs()) assertEquals(c.str("out"), stem(c.str("word")), c.str("word"))
        for (c in rules.getValue("heuristic").objs()) assertExtraction(c.getValue("out"), heuristic(c.str("text"), cfg), c.str("text"))
        for (c in rules.getValue("domain_of").objs()) assertEquals(c.str("out"), domainOf(c.str("text"), cfg), c.str("text"))
        for (c in rules.getValue("concept_key").objs()) assertEquals(c.str("out"), conceptKey(c.str("label")), c.str("label"))
        for (c in rules.getValue("parse_llm").objs()) assertExtraction(c.getValue("out"), parseLlm(c.str("raw"), cfg), c.str("raw"))
        for (c in rules.getValue("is_sensitive").objs()) {
            assertEquals(c.getValue("out").jsonPrimitive.boolean, isSensitiveText(c.str("text"), cfg.sensitiveWords), c.str("text"))
        }
        for (c in rules.getValue("keep").objs()) {
            val got = keep(extraction(c.obj("in")), cfg, c.getValue("allow_sensitive").jsonPrimitive.boolean)
            assertExtraction(c.getValue("out"), got, "keep")
        }
    }

    @Test
    fun `strength, learning, recall, prune and feedback`() {
        for (c in rules.getValue("effective").objs()) {
            val got = effective(c.getValue("strength").jsonPrimitive.double, time(c.str("last_used")), time(c.str("now")), c.getValue("half_life_days").jsonPrimitive.double)
            close(c.getValue("out").jsonPrimitive.double, got, "effective")
        }
        for ((i, c) in rules.getValue("plan_learn").objs().withIndex()) {
            val known = c.getValue("known").objs().map(::concept).associateBy { it.key }
            val links = c.getValue("links").objs().map(::link).associateBy { it.a to it.b }
            val plan = planLearn(extraction(c.obj("extraction")), known, links, time(c.str("now")), cfg, c.strOrNull("domain"))
            val out = c.obj("out")
            assertConcepts(out.getValue("concepts"), plan.concepts, "plan_learn $i")
            assertLinks(out.getValue("links"), plan.links, "plan_learn $i")
            assertEquals(out.getValue("fresh").strings(), plan.fresh)
            assertEquals(out.getValue("reinforced").strings(), plan.reinforced)
            assertEquals(out.str("domain"), plan.domain)
        }
        val recall = rules.obj("plan_recall")
        val known = recall.getValue("known").objs().map(::concept).associateBy { it.key }
        val links = recall.getValue("links").objs().map(::link)
        for ((i, c) in recall.getValue("cases").objs().withIndex()) {
            val (hits, reinforced) = planRecall(c.getValue("keys").strings(), known, links, time(c.str("now")), cfg, c.getValue("k").jsonPrimitive.int)
            val out = c.obj("out")
            assertEquals(
                out.getValue("hits").objs().map { RecallHit(it.str("key"), it.str("label"), it.getValue("score").jsonPrimitive.double, it.str("via")) },
                hits, "plan_recall $i",
            )
            assertConcepts(out.getValue("reinforced"), reinforced, "plan_recall $i")
        }
        val rk = rules.obj("rekey")
        val (rkConcepts, rkLinks) = rekey(rk.getValue("concepts").objs().map(::concept), rk.getValue("links").objs().map(::link), cfg)
        assertConcepts(rk.obj("out").getValue("concepts"), rkConcepts, "rekey")
        assertLinks(rk.obj("out").getValue("links"), rkLinks, "rekey")
        val prune = rules.obj("plan_prune")
        val (dead, weak) = planPrune(prune.getValue("concepts").objs().map(::concept), prune.getValue("links").objs().map(::link), time(prune.str("now")), cfg)
        assertEquals(prune.obj("out").getValue("dead").strings(), dead)
        assertEquals(prune.obj("out").getValue("weak").jsonArray.map { val p = it.strings(); p[0] to p[1] }, weak)
        val fb = rules.obj("feedback")
        val fbConcepts = fb.getValue("concepts").objs().map(::concept)
        assertConcepts(fb.getValue("praise"), planPraise(fbConcepts, time(fb.str("now")), cfg), "praise")
        val (kept, gone) = planCriticism(fbConcepts, time(fb.str("now")), cfg)
        assertConcepts(fb.obj("criticism").getValue("kept"), kept, "criticism")
        assertEquals(fb.obj("criticism").getValue("dead").strings(), gone)
        val strong = rules.obj("strongest")
        assertEquals(
            strong.getValue("out").strings(),
            strongest(strong.getValue("concepts").objs().map(::concept), time(strong.str("now")), cfg, strong.getValue("n").jsonPrimitive.int).map { it.key },
        )
        for (c in rules.getValue("main_domain").objs()) {
            val domains = c.obj("domains").mapValues { it.value.jsonPrimitive.int }
            assertEquals(c.str("out"), mainDomain(Concept("x", "x", 0.3, Instant.EPOCH, 1, Instant.EPOCH, domains)), domains.toString())
        }
    }

    @Test
    fun `commands, answers, reactions and texts`() {
        for (c in rules.getValue("parse_command").objs()) {
            val got = parseCommand(c.str("text"))
            val out = c["out"]
            if (out is JsonNull) assertNull(got, c.str("text")) else {
                assertEquals(MemoryCommand(MemoryKind.valueOf(out!!.jsonObject.str("kind").uppercase()), out.jsonObject.str("text")), got, c.str("text"))
            }
        }
        for (c in rules.getValue("parse_answer").objs()) assertEquals(c.getValue("out").jsonPrimitive.booleanOrNull, parseYesNo(c.str("text")), c.str("text"))
        for (c in rules.getValue("parse_feedback").objs()) {
            assertEquals(c.strOrNull("out")?.let { Feedback.valueOf(it.uppercase()) }, parseFeedback(c.str("text")), c.str("text"))
        }
        val t = rules.obj("texts")
        for (c in t.getValue("remember").objs()) assertEquals(c.str("out"), rememberText(c.getValue("labels").strings()))
        for (c in t.getValue("forget").objs()) assertEquals(c.str("out"), forgetText(c.getValue("labels").strings()))
        for (c in t.getValue("forget_all_question").objs()) assertEquals(c.str("out"), forgetAllQuestion(c.getValue("n").jsonPrimitive.int))
        for (c in t.getValue("recall").objs()) assertEquals(c.str("out"), recallText(c.getValue("labels").strings(), c.getValue("total").jsonPrimitive.int))
        for (c in t.getValue("decimal_pl").objs()) assertEquals(c.str("out"), decimalPl(c.getValue("x").jsonPrimitive.double), c.toString())
        assertEquals(t.str("tool_recall"), toolRecallText(listOf(RecallHit("lamp", "lampa", 0.625, "match"), RecallHit("salon", "salon", 0.0849, "link"))))
        assertEquals(t.str("tool_recall_empty"), toolRecallText(emptyList()))
        for (c in t.getValue("tool_remember").objs()) {
            assertEquals(c.str("out"), toolRememberText(c.getValue("fresh").strings(), c.getValue("reinforced").strings()))
        }
        val k = t.obj("constants")
        assertEquals(k.str("forget_all_done"), FORGET_ALL_DONE)
        assertEquals(k.str("forget_all_kept"), FORGET_ALL_KEPT)
        assertEquals(k.str("store_failed"), STORE_FAILED_TEXT)
        assertEquals(k.str("praise"), PRAISE_TEXT)
        assertEquals(k.str("criticism"), CRITICISM_TEXT)
        for (c in rules.getValue("clean_for_speech").objs()) assertEquals(c.str("out"), cleanForSpeech(c.str("raw")), c.str("raw"))
    }

    @Test
    fun `the command scenario gives the host replies and graph`() {
        val steps = root.getValue("scenario").objs()
        val clock = ManualClock(time(steps.first().str("now")))
        val store = InMemoryMemoryStore()
        val keeper = MemoryKeeper(cfg, LlmConfig(), clock, store)
        for ((i, step) in steps.withIndex()) {
            val arg = step["arg"]
            val reply: String? = when (step.str("kind")) {
                "say" -> arg!!.jsonPrimitive.content.let { keeper.answer(it) ?: keeper.command(it) }
                "advance" -> null.also { clock.advance(arg!!.jsonPrimitive.double * 86400) }
                "recall" -> keeper.recall(arg!!.jsonPrimitive.content).let { hits ->
                    keeper.noteRecalled(hits.map { it.key })
                    toolRecallText(hits)
                }
                "prune" -> null.also { keeper.maybePrune() }
                else -> error("nieznany krok")
            }
            val what = "krok $i: ${step.str("kind")} ${arg}"
            assertEquals(step.strOrNull("reply"), reply, what)
            assertEquals(step.str("now"), IsoTime.format(clock.now()), what)
            assertConcepts(step.getValue("concepts"), store.concepts().values.sortedBy { it.key }, what)
            assertLinks(step.getValue("links"), store.allLinks().sortedWith(compareBy<Link> { it.a }.thenBy { it.b }), what)
            assertEquals(step.getValue("forgotten").jsonPrimitive.int, store.forgotten(), what)
        }
    }

    @Test
    fun `facts migrate to the same graph as on the host`() {
        val m = root.obj("migration")
        val store = InMemoryMemoryStore()
        migrateFacts(m.getValue("facts").objs().map { LegacyFact(it.str("text"), time(it.str("updated_at"))) }, store)
        assertConcepts(m.getValue("concepts"), store.concepts().values.sortedBy { it.key }, "migracja")
        assertLinks(m.getValue("links"), store.allLinks().sortedWith(compareBy<Link> { it.a }.thenBy { it.b }), "migracja")
    }
}
