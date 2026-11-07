package pl.iris.core

import java.time.Instant

/** Pojęcie dla ekranu pamięci: siła bieżąca i główna domena. */
data class ConceptView(val concept: Concept, val effective: Double, val domain: String)

/** Stan grafu dla ekranu pamięci i eksportu. Pojęcia od najsilniejszych. */
data class MemoryView(val now: Instant, val concepts: List<ConceptView>, val links: List<Link>, val forgotten: Int)

/**
 * Pamięć IRIS w aplikacji (docs/spec.md, sekcje 9 i 14). Port host/api/memory.py bez zdarzeń: polecenia,
 * reakcje, nauka z rozmowy i narzędzia tury. Pojęcia są tylko w [MemoryStore], nie w logach.
 * Wywołujący ([Chat]) dba o kolejność: metody nie są bezpieczne dla wielu wątków naraz.
 */
class MemoryKeeper(
    private val cfg: MemoryConfig,
    private val llm: LlmConfig,
    private val clock: Clock,
    private val store: MemoryStore,
) {
    private var forgetAllUntil: Double? = null // pytanie "zapomnij wszystko" czeka na tak/nie
    private var recalled: List<String> = emptyList() // klucze przypomniane do ostatniej odpowiedzi LLM
    private var lastPrune: Double? = null

    val enabled: Boolean get() = cfg.enabled

    private fun now(): Instant = clock.now().toMillisPrecision()

    // ---- operacje na grafie ----

    fun learn(ext: Extraction, domain: String? = null): LearnPlan {
        val keys = ext.concepts.map { it.first }
        val plan = planLearn(ext, store.concepts(keys), store.linksBetween(keys), now(), cfg, domain)
        if (plan.concepts.isEmpty()) return plan
        return try {
            store.save(plan.concepts, plan.links)
            plan
        } catch (_: Exception) {
            LearnPlan(domain = plan.domain, stored = false) // błąd zapisu: nic nie zapamiętano
        }
    }

    /** Nauka wprost (polecenie "zapamiętaj"): heurystyka, filtr liczb, bez filtra danych wrażliwych. */
    fun teach(text: String, domain: String? = null): LearnPlan = learn(keep(heuristic(text, cfg), cfg, allowSensitive = true), domain)

    fun recall(query: String, k: Int = cfg.recallK): List<RecallHit> {
        val keys = heuristic(query, cfg).concepts.map { it.first }
        if (keys.isEmpty()) return emptyList()
        val matched = store.concepts(keys)
        val links = store.linksTouching(matched.keys)
        val neighbours = links.map { if (it.a in matched) it.b else it.a }.toSet() - matched.keys
        val known = matched + store.concepts(neighbours)
        val (hits, reinforced) = planRecall(keys, known, links, now(), cfg, k)
        if (reinforced.isNotEmpty()) store.save(reinforced)
        return hits
    }

    fun forget(keys: List<String>): List<Concept> = store.deleteConcepts(keys)

    fun clear(): Int = store.clear()

    /** Przy pierwszym wywołaniu i co `pruneEveryS`: usuwa pojęcia i połączenia poniżej progu. */
    fun maybePrune() {
        val mono = clock.monotonic()
        val last = lastPrune
        if (!enabled || (last != null && mono - last < cfg.pruneEveryS)) return
        lastPrune = mono
        val (dead, weak) = planPrune(store.concepts().values, store.allLinks(), now(), cfg)
        if (dead.isEmpty() && weak.isEmpty()) return
        store.save(delete = dead, deleteLinks = weak, forgotten = dead.size)
    }

    fun view(): MemoryView {
        val now = now()
        val concepts = strongest(store.concepts().values, now, cfg, Int.MAX_VALUE)
            .map { ConceptView(it, conceptStrength(it, now, cfg), mainDomain(it)) }
        val links = store.allLinks().sortedWith(compareBy<Link> { it.a }.thenBy { it.b })
        return MemoryView(now, concepts, links, store.forgotten())
    }

    // ---- przed LLM: odpowiedź na pytanie, polecenia, reakcje ----

    /** Odpowiedź na pytanie z "zapomnij wszystko" albo null. Każda wypowiedź zamyka pytanie. */
    fun answer(text: String): String? {
        val until = forgetAllUntil
        forgetAllUntil = null
        if (!enabled || until == null || clock.monotonic() > until) return null
        val yes = parseYesNo(text) ?: return null
        if (!yes) return FORGET_ALL_KEPT
        clear()
        return FORGET_ALL_DONE
    }

    /** Polecenie pamięci albo reakcja na ostatnią odpowiedź; null, gdy wypowiedź trafia do LLM. */
    fun command(text: String): String? {
        if (!enabled) return null
        val cmd = parseCommand(text) ?: return feedback(text)
        return when (cmd.kind) {
            MemoryKind.REMEMBER -> {
                val plan = teach(cmd.text)
                if (plan.stored) rememberText(plan.fresh + plan.reinforced) else STORE_FAILED_TEXT
            }
            MemoryKind.FORGET -> forgetText(forget(heuristic(cmd.text, cfg).concepts.map { it.first }).map { it.label })
            MemoryKind.FORGET_ALL -> {
                val total = store.count()
                if (total > 0) forgetAllUntil = clock.monotonic() + cfg.answerWindowS
                forgetAllQuestion(total)
            }
            MemoryKind.RECALL -> {
                val total = store.count()
                recallText(strongest(store.concepts().values, now(), cfg, 8).map { it.label }, total)
            }
        }
    }

    /** Pojęcia przypomniane do ostatniej odpowiedzi LLM (narzędziem albo w kontekście). */
    fun noteRecalled(keys: List<String>) {
        recalled = keys.distinct()
    }

    /** Pochwała albo krytyka; null, gdy to nie reakcja albo do odpowiedzi nie przypomniano pojęć. */
    fun feedback(text: String): String? {
        if (!(enabled && cfg.feedback)) return null
        val kind = parseFeedback(text) ?: return null
        val used = store.concepts(recalled).values.toList()
        if (used.isEmpty()) return null
        recalled = emptyList()
        val now = now()
        if (kind == Feedback.PRAISE) {
            store.save(planPraise(used, now, cfg))
            return PRAISE_TEXT
        }
        val (kept, dead) = planCriticism(used, now, cfg)
        store.save(kept, delete = dead)
        return CRITICISM_TEXT
    }

    // ---- narzędzia tury i pamięć w kontekście ----

    fun allowedFor(remote: Boolean): Boolean =
        enabled && (llm.sendMemory == SendMemory.ALWAYS || (llm.sendMemory == SendMemory.LOCAL_ONLY && !remote))

    fun tools(): TurnTools = TurnTools(this, cfg)

    // ---- nauka z rozmowy ----

    /** Czy wypowiedź może uczyć graf: wypowiedź z danymi wrażliwymi nie uczy wcale (spec, sekcja 9). */
    fun learnable(utterance: String): Boolean = enabled && !isSensitiveText(utterance, cfg.sensitiveWords)

    fun usesLlm(engine: LlmEngine): Boolean =
        cfg.infer == InferPolicy.ALWAYS || (cfg.infer == InferPolicy.LOCAL_ONLY && !engine.remote)

    /** Pojęcia z LLM (osobne zapytanie z samą wypowiedzią) albo null przy błędzie; wtedy heurystyka. */
    suspend fun extract(engine: LlmEngine, utterance: String): Extraction {
        if (usesLlm(engine)) {
            try {
                val reply = engine.complete(CONCEPTS_PROMPT, listOf(Message(Role.USER, utterance)), CONCEPTS_MAX_TOKENS)
                if (reply.finish != Finish.REFUSAL) parseLlm(reply.text, cfg)?.let { return it }
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (_: Exception) {
                // błąd silnika pomijamy bez treści, zostaje heurystyka
            }
        }
        return heuristic(utterance, cfg)
    }

    /** Nauka z wyciągniętych pojęć, z filtrem liczb i danych wrażliwych. */
    fun learnConversation(ext: Extraction): LearnPlan = learn(keep(ext, cfg, allowSensitive = false))
}

/** Narzędzia pamięci jednej tury rozmowy ([ToolBox]). */
class TurnTools(private val memory: MemoryKeeper, private val cfg: MemoryConfig) : ToolBox {
    val recalled = mutableListOf<String>()

    override fun specs(remote: Boolean): List<ToolSpec> = when {
        !memory.enabled -> emptyList()
        memory.allowedFor(remote) -> listOf(RECALL_TOOL, REMEMBER_TOOL)
        else -> listOf(REMEMBER_TOOL)
    }

    override suspend fun call(call: ToolCall): ToolResult = when (call.name) {
        RECALL_TOOL.name -> {
            val query = call.arguments.stringArg("query")
            if (query == null || query.isBlank() || cpLength(query) > 500) {
                ToolResult(call.id, call.name, "Argument query musi być tekstem (1-500 znaków).", isError = true)
            } else {
                val hits = memory.recall(query)
                recalled += hits.map { it.key }
                ToolResult(call.id, call.name, toolRecallText(hits))
            }
        }
        REMEMBER_TOOL.name -> {
            val text = call.arguments.stringArg("text")
            val domain = call.arguments["domain"]?.let { call.arguments.stringArg("domain") ?: "?" }
            when {
                text == null || text.isBlank() || cpLength(text) > MAX_COMMAND_CHARS ->
                    ToolResult(call.id, call.name, "Argument text musi być tekstem (1-300 znaków).", isError = true)
                domain != null && domain !in MEMORY_DOMAIN_IDS -> ToolResult(call.id, call.name, "Nieznana domena.", isError = true)
                else -> {
                    val plan = memory.learn(keep(heuristic(text, cfg), cfg, allowSensitive = false), domain)
                    ToolResult(call.id, call.name, toolRememberText(plan.fresh, plan.reinforced))
                }
            }
        }
        else -> ToolResult(call.id, call.name, "Nieznane narzędzie: ${call.name}.", isError = true)
    }

    override suspend fun context(text: String, remote: Boolean): List<String> {
        if (!memory.allowedFor(remote)) return emptyList()
        val hits = memory.recall(text)
        recalled += hits.map { it.key }
        return if (hits.isEmpty()) emptyList() else toolRecallText(hits).split("\n")
    }
}
