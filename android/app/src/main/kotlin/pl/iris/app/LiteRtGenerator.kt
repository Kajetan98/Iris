package pl.iris.app

import android.content.Context
import com.google.ai.edge.litertlm.Backend
import com.google.ai.edge.litertlm.Contents
import com.google.ai.edge.litertlm.ConversationConfig
import com.google.ai.edge.litertlm.Engine
import com.google.ai.edge.litertlm.EngineConfig
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import pl.iris.core.LlmConfigError
import pl.iris.core.LlmException
import pl.iris.core.LocalGenerator

/**
 * Model językowy na telefonie przez LiteRT-LM (docs/spec.md, sekcja 14). Silnik ładuje się przy pierwszej
 * rozmowie i zostaje w pamięci do zmiany modelu lub silnika. Każde wywołanie to nowa rozmowa; historię
 * składa [pl.iris.core.LocalEngine]. Treść rozmowy nie trafia do komunikatów błędów.
 */
class LiteRtGenerator(
    private val context: Context,
    private val models: ModelFiles,
    private val scope: CoroutineScope,
) : LocalGenerator {
    private val lock = Mutex()
    private var engine: Engine? = null
    private var loaded = ""

    @Volatile
    private var wanted = ""

    /** Wskazuje model do użycia ("" = żaden); pamięć zajmowana przez inny model jest zwalniana. */
    fun useModel(name: String) {
        wanted = name
        scope.launch(Dispatchers.IO) {
            lock.withLock { if (loaded != wanted) closeEngine() }
        }
    }

    override suspend fun generate(system: String, message: String): String = withContext(Dispatchers.IO) {
        lock.withLock {
            val e = engineFor(wanted)
            try {
                e.createConversation(ConversationConfig(systemInstruction = Contents.of(system))).use { it.sendMessage(message).toString() }
            } catch (ex: kotlinx.coroutines.CancellationException) {
                throw ex
            } catch (ex: Exception) {
                throw LlmException("Model lokalny zwrócił błąd (${ex.javaClass.simpleName}).")
            }
        }
    }

    private fun engineFor(name: String): Engine {
        engine?.let { if (loaded == name) return it }
        closeEngine()
        if (name.isEmpty()) throw LlmConfigError("Nie wybrano modelu. Wskaż plik .litertlm w ustawieniach.")
        val file = models.file(name)
        if (!file.isFile) throw LlmConfigError("Brak pliku modelu na telefonie. Wybierz go ponownie w ustawieniach.")
        try {
            val created = Engine(EngineConfig(modelPath = file.path, backend = Backend.CPU(), cacheDir = context.cacheDir.path))
            created.initialize()
            engine = created
            loaded = name
            return created
        } catch (ex: kotlinx.coroutines.CancellationException) {
            throw ex
        } catch (ex: OutOfMemoryError) {
            throw LlmConfigError("Za mało pamięci, żeby wczytać model. Wybierz mniejszy model.")
        } catch (ex: Exception) {
            val detail = ex.message?.take(200)?.let { ": $it" }.orEmpty()
            throw LlmConfigError("Nie udało się wczytać modelu (${ex.javaClass.simpleName}$detail). Sprawdź, czy to plik .litertlm.")
        }
    }

    private fun closeEngine() {
        runCatching { engine?.close() }
        engine = null
        loaded = ""
    }
}
