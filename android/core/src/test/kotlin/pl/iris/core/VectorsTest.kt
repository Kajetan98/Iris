package pl.iris.core

import java.time.LocalDateTime
import java.time.OffsetDateTime
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNull
import kotlin.test.assertTrue
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.double
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long

/**
 * Zgodność z hostem (docs/spec.md, sekcja 14): każdy przypadek z learning_vectors.json, wygenerowanego
 * z kodu Pythona (python -m host.tests.make_android_vectors), musi dać w Kotlinie ten sam wynik.
 */
class VectorsTest {
    private val root: JsonObject = Json.parseToJsonElement(
        checkNotNull(javaClass.getResource("/learning_vectors.json")) { "brak learning_vectors.json" }.readText(),
    ).jsonObject
    private val rules = root.obj("rules")
    private val facts = root.obj("facts")

    private fun JsonObject.obj(key: String) = getValue(key).jsonObject
    private fun JsonObject.arr(key: String) = getValue(key).jsonArray
    private fun JsonObject.str(key: String) = getValue(key).jsonPrimitive.content
    private fun JsonElement.objs() = jsonArray.map { it.jsonObject }
    private fun JsonObject.strOrNull(key: String) = (getValue(key) as? JsonPrimitive)?.takeIf { it !is JsonNull }?.content

    private fun fact(o: JsonObject) = Fact(
        id = o.getValue("id").jsonPrimitive.long,
        text = o.str("text"),
        source = Source.entries.first { it.wire == o.str("source") },
        confidence = o.getValue("confidence").jsonPrimitive.double,
        createdAt = OffsetDateTime.parse(o.str("created_at")).toInstant(),
        updatedAt = OffsetDateTime.parse(o.str("updated_at")).toInstant(),
        reinforced = o.getValue("reinforced").jsonPrimitive.int,
        askedAt = o.strOrNull("asked_at")?.let { OffsetDateTime.parse(it).toInstant() },
    )

    private fun factsOf(a: JsonElement) = a.jsonArray.map { fact(it.jsonObject) }

    private fun learningConfig(overrides: JsonObject?): LearningConfig {
        var c = LearningConfig()
        for ((k, v) in overrides ?: JsonObject(emptyMap())) {
            val p = v.jsonPrimitive
            c = when (k) {
                "enabled" -> c.copy(enabled = p.boolean)
                "ask" -> c.copy(ask = p.boolean)
                "feedback" -> c.copy(feedback = p.boolean)
                "infer" -> c.copy(infer = InferPolicy.valueOf(p.content.uppercase()))
                else -> error("nieobsłużone ustawienie learning.$k")
            }
        }
        return c
    }

    @Test
    fun `extract prompt is identical to the host prompt`() {
        assertEquals(root.str("extract_prompt"), EXTRACT_PROMPT)
    }

    @Test
    fun `default settings equal the host defaults`() {
        val l = root.obj("defaults").obj("learning")
        val c = LearningConfig()
        assertEquals(l.getValue("enabled").jsonPrimitive.boolean, c.enabled)
        assertEquals(l.str("infer"), c.infer.name.lowercase())
        assertEquals(l.getValue("max_per_turn").jsonPrimitive.int, c.maxPerTurn)
        assertEquals(l.getValue("similar").jsonPrimitive.double, c.similar)
        assertEquals(l.getValue("forget_match").jsonPrimitive.double, c.forgetMatch)
        assertEquals(l.getValue("start_max").jsonPrimitive.double, c.startMax)
        assertEquals(l.getValue("reinforce_step").jsonPrimitive.double, c.reinforceStep)
        assertEquals(l.getValue("max_inferred").jsonPrimitive.double, c.maxInferred)
        assertEquals(l.getValue("ask").jsonPrimitive.boolean, c.ask)
        assertEquals(l.getValue("ask_min").jsonPrimitive.double, c.askMin)
        assertEquals(l.getValue("ask_interval_s").jsonPrimitive.double, c.askIntervalS)
        assertEquals(l.getValue("ask_again_days").jsonPrimitive.double, c.askAgainDays)
        assertEquals(l.getValue("answer_window_s").jsonPrimitive.double, c.answerWindowS)
        assertEquals(l.getValue("decay_after_days").jsonPrimitive.double, c.decayAfterDays)
        assertEquals(l.getValue("decay_step").jsonPrimitive.double, c.decayStep)
        assertEquals(l.getValue("forget_below").jsonPrimitive.double, c.forgetBelow)
        assertEquals(l.getValue("feedback").jsonPrimitive.boolean, c.feedback)
        assertEquals(l.getValue("feedback_up").jsonPrimitive.double, c.feedbackUp)
        assertEquals(l.getValue("feedback_down").jsonPrimitive.double, c.feedbackDown)
        assertEquals(l.arr("sensitive_words").map { it.jsonPrimitive.content }, c.sensitiveWords)

        val m = root.obj("defaults").obj("llm")
        val d = LlmConfig()
        assertEquals(m.getValue("max_tokens").jsonPrimitive.int, d.maxTokens)
        assertEquals(m.getValue("timeout_s").jsonPrimitive.double, d.timeoutS)
        assertEquals(m.getValue("retries").jsonPrimitive.int, d.retries)
        assertEquals(m.getValue("history_turns").jsonPrimitive.int, d.historyTurns)
        assertEquals(m.getValue("max_facts").jsonPrimitive.int, d.maxFacts)
        assertEquals(m.getValue("min_inferred_confidence").jsonPrimitive.double, d.minInferredConfidence)
        assertEquals(m.str("effort"), d.effort)
        assertEquals(m.getValue("thinking_headroom_tokens").jsonPrimitive.int, d.thinkingHeadroomTokens)
        assertEquals(m.getValue("fallbacks").jsonPrimitive.boolean, d.fallbacks)
        assertEquals(root.obj("defaults").getValue("quiet").jsonPrimitive.boolean, CoreConfig().quiet)
    }

    @Test
    fun `memory commands`() {
        for (v in rules.arr("parse_memory").objs()) {
            val input = v.str("in")
            val expected = v.getValue("out")
            val actual = parseMemory(input)
            if (expected is JsonNull) {
                assertNull(actual, input)
            } else {
                val e = expected.jsonObject
                assertEquals(e.str("kind").uppercase(), actual?.kind?.name, input)
                assertEquals(e.str("text"), actual?.text, input)
            }
        }
    }

    @Test
    fun `answers to a question`() {
        for (v in rules.arr("parse_answer").objs()) {
            val actual = parseAnswer(v.str("in"))
            val yes = v.getValue("yes")
            assertEquals(if (yes is JsonNull) null else yes.jsonPrimitive.boolean, actual.yes, v.str("in"))
            assertEquals(v.str("correction"), actual.correction, v.str("in"))
        }
    }

    @Test
    fun `user reactions`() {
        for (v in rules.arr("parse_feedback").objs()) {
            val expected = v.strOrNull("out")
            assertEquals(expected, parseFeedback(v.str("in"))?.name?.lowercase()?.let { if (it == "criticism") "criticism" else "praise" }, v.str("in"))
        }
    }

    @Test
    fun `fact text`() {
        for (v in rules.arr("fact_text").objs()) assertEquals(v.str("out"), factText(v.str("in")), v.str("in"))
    }

    @Test
    fun `word stems`() {
        for (v in rules.arr("stems").objs()) {
            assertEquals(v.arr("out").map { it.jsonPrimitive.content }.toSet(), stems(v.str("in")), v.str("in"))
        }
    }

    @Test
    fun similarity() {
        for (v in rules.arr("similarity").objs()) {
            assertEquals(v.getValue("out").jsonPrimitive.double, similarity(v.str("a"), v.str("b")), "${v.str("a")} | ${v.str("b")}")
        }
    }

    @Test
    fun `sensitive words`() {
        for (v in rules.arr("is_sensitive").objs()) {
            assertEquals(v.getValue("out").jsonPrimitive.boolean, isSensitive(v.str("in"), SENSITIVE_STEMS), v.str("in"))
        }
    }

    @Test
    fun `cleaning for speech`() {
        for (v in rules.arr("clean_for_speech").objs()) assertEquals(v.str("out"), cleanForSpeech(v.str("in")), v.str("in"))
    }

    @Test
    fun `guesses from the model`() {
        for (v in rules.arr("parse_guesses").objs()) {
            val raw = v.str("raw")
            val limit = v.getValue("limit").jsonPrimitive.int
            val out = v.getValue("out")
            if (out is JsonObject) {
                assertFailsWith<LearnError>(raw) { parseGuesses(raw, limit) }
            } else {
                val expected = (out as JsonArray).map { it.jsonObject.str("text") to it.jsonObject.getValue("confidence").jsonPrimitive.double }
                assertEquals(expected, parseGuesses(raw, limit).map { it.text to it.confidence }, raw)
            }
        }
    }

    @Test
    fun `best match`() {
        for (v in facts.arr("best_match").objs()) {
            val expected = v.getValue("out").let { if (it is JsonNull) null else it.jsonPrimitive.long }
            val actual = bestMatch(v.str("in"), factsOf(v.getValue("facts")), v.getValue("min").jsonPrimitive.double)
            assertEquals(expected, actual?.id, v.str("in"))
        }
    }

    @Test
    fun `confirmation question text`() {
        for (v in facts.arr("question_for").objs()) assertEquals(v.str("out"), questionFor(fact(v.obj("fact"))))
    }

    @Test
    fun `which guess is asked about`() {
        for (v in facts.arr("pick_question").objs()) {
            val expected = v.getValue("out").let { if (it is JsonNull) null else it.jsonPrimitive.long }
            val now = OffsetDateTime.parse(v.str("now")).toInstant()
            assertEquals(expected, pickQuestion(factsOf(v.getValue("facts")), now, LearningConfig())?.id)
        }
    }

    @Test
    fun `what do you know about me`() {
        for (v in facts.arr("recall_text").objs()) {
            assertEquals(v.str("out"), recallText(factsOf(v.getValue("facts")), v.getValue("min").jsonPrimitive.double))
        }
    }

    @Test
    fun `fact selection for the context`() {
        for (v in facts.arr("select_facts").objs()) {
            val cfg = LlmConfig(
                sendFacts = SendFacts.valueOf(v.str("send_facts").uppercase()),
                maxFacts = v.getValue("max_facts").jsonPrimitive.int,
                minInferredConfidence = v.getValue("min_inferred_confidence").jsonPrimitive.double,
            )
            val expected = v.arr("out").map { it.jsonPrimitive.long }
            val actual = selectFacts(factsOf(v.getValue("facts")), cfg, v.getValue("remote").jsonPrimitive.boolean).map { it.id }
            assertEquals(expected, actual, v.toString().take(120))
        }
    }

    @Test
    fun `context block`() {
        val c = facts.obj("context")
        val chosen = selectFacts(factsOf(c.getValue("facts")), LlmConfig(sendFacts = SendFacts.ALWAYS), remote = true)
        val lines = renderContext(chosen, LocalDateTime.parse(c.str("now"))).split("\n")
        assertEquals(c.str("time_line"), lines.first { it.startsWith("Czas: ") })
        assertEquals(c.arr("fact_lines").map { it.jsonPrimitive.content }, lines.drop(lines.indexOf("Znane fakty o użytkowniku:") + 1))
        for (t in c.arr("times").objs()) assertEquals(t.str("out"), formatTime(LocalDateTime.parse(t.str("now"))), t.str("now"))
    }

    @Test
    fun `learner scenarios replay the host`() = runTest {
        val start = OffsetDateTime.parse(root.str("start")).toInstant()
        val scenarios = root.arr("scenarios").objs()
        assertTrue(scenarios.size >= 15)
        for (s in scenarios) {
            val name = s.str("name")
            val config = s.obj("config")
            val clock = ManualClock(start, 0.0)
            val store = InMemoryFactStore(clock)
            val quiet = config["initiative"]?.jsonObject?.getValue("quiet")?.jsonPrimitive?.boolean ?: false
            val learner = Learner(learningConfig(config["learning"]?.jsonObject), 0.5, quiet, clock, store)
            val steps = s.arr("steps").objs()
            val expected = s.arr("expected").objs()
            for ((i, step) in steps.withIndex()) {
                val at = "$name, krok ${i + 1} (${step.str("op")})"
                var reply: String? = null
                when (step.str("op")) {
                    "say" -> reply = learner.answer(step.str("text")) ?: learner.command(step.str("text"))
                    "question" -> reply = learner.question()
                    "merge" -> learner.merge(step.arr("guesses").map { Guess(it.jsonArray[0].jsonPrimitive.content, it.jsonArray[1].jsonPrimitive.double) })
                    "seed" -> store.addFact(step.str("text"), Source.entries.first { it.wire == step.str("source") }, step.getValue("confidence").jsonPrimitive.double)
                    "decay" -> learner.maybeDecay()
                    "context" -> learner.noteContext(step.arr("ids").map { it.jsonPrimitive.long })
                    "advance" -> clock.advance(step.getValue("seconds").jsonPrimitive.double)
                    else -> error(step.str("op"))
                }
                assertEquals(expected[i].strOrNull("reply"), reply, at)
                val wanted = expected[i].arr("facts").map { it.jsonArray }
                val actual = store.facts()
                assertEquals(wanted.size, actual.size, "$at: liczba faktów")
                for ((w, f) in wanted.zip(actual)) {
                    assertEquals(w[0].jsonPrimitive.long, f.id, "$at: id")
                    assertEquals(w[1].jsonPrimitive.content, f.text, "$at: tekst")
                    assertEquals(w[2].jsonPrimitive.content, f.source.wire, "$at: źródło")
                    assertEquals(w[3].jsonPrimitive.double, roundTo6(f.confidence), 1e-9, "$at: pewność")
                    assertEquals(w[4].jsonPrimitive.int, f.reinforced, "$at: wzmocnienia")
                    assertEquals(w[5].jsonPrimitive.boolean, f.askedAt != null, "$at: pytano")
                }
            }
        }
    }
}
