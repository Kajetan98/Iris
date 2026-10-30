package pl.iris.app

import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONObject
import org.vosk.LibVosk
import org.vosk.LogLevel
import org.vosk.Model
import org.vosk.Recognizer
import org.vosk.android.RecognitionListener
import org.vosk.android.SpeechService
import pl.iris.core.matchesWakeWord

/**
 * Nasłuch hasła wybudzającego przez Vosk, offline (docs/spec.md, sekcja 14). Dźwięk jest przetwarzany tylko
 * na telefonie. Po wykryciu hasła mikrofon jest zwalniany, a dopiero potem wywoływane jest [onWake].
 */
class WakeWordListener(
    private val model: WakeModel,
    private val scope: CoroutineScope,
    private val onWake: suspend () -> Unit,
) {
    private val lock = Mutex()
    private var voskModel: Model? = null
    private var recognizer: Recognizer? = null
    private var service: SpeechService? = null
    private val fired = AtomicBoolean(false)

    private val _problem = MutableStateFlow<String?>(null)

    /** Opis błędu nasłuchu (np. nie wczytano modelu) albo null. */
    val problem: StateFlow<String?> = _problem

    suspend fun start() = withContext(Dispatchers.Default) {
        lock.withLock {
            if (service != null) return@withLock
            try {
                LibVosk.setLogLevel(LogLevel.WARNINGS)
                val m = voskModel ?: Model(model.path).also { voskModel = it }
                val r = Recognizer(m, SAMPLE_RATE)
                val s = SpeechService(r, SAMPLE_RATE)
                fired.set(false)
                s.startListening(listener)
                recognizer = r
                service = s
                _problem.value = null
            } catch (e: Exception) {
                _problem.value = "Nasłuch hasła nie działa (${e.javaClass.simpleName}). Sprawdź model w ustawieniach."
            } catch (e: UnsatisfiedLinkError) {
                _problem.value = "Nasłuch hasła nie działa na tym telefonie (brak biblioteki natywnej)."
            }
        }
    }

    suspend fun stop() = withContext(Dispatchers.Default) { lock.withLock { stopLocked() } }

    /** Zatrzymuje nasłuch i zwalnia model z pamięci. */
    suspend fun release() = withContext(Dispatchers.Default) {
        lock.withLock {
            stopLocked()
            runCatching { voskModel?.close() }
            voskModel = null
        }
    }

    private fun stopLocked() {
        service?.let {
            runCatching { it.stop() }
            runCatching { it.shutdown() }
        }
        runCatching { recognizer?.close() }
        service = null
        recognizer = null
    }

    private val listener = object : RecognitionListener {
        override fun onPartialResult(hypothesis: String?) = check(hypothesis, "partial")

        override fun onResult(hypothesis: String?) = check(hypothesis, "text")

        override fun onFinalResult(hypothesis: String?) = check(hypothesis, "text")

        override fun onError(exception: Exception?) {
            _problem.value = "Nasłuch hasła przerwał błąd (${exception?.javaClass?.simpleName ?: "nieznany"})."
        }

        override fun onTimeout() = Unit
    }

    private fun check(hypothesis: String?, key: String) {
        val text = runCatching { JSONObject(hypothesis.orEmpty()).optString(key) }.getOrDefault("")
        if (text.isNotEmpty() && matchesWakeWord(text) && fired.compareAndSet(false, true)) {
            scope.launch {
                stop()
                onWake()
            }
        }
    }

    private companion object {
        const val SAMPLE_RATE = 16000.0f
    }
}
