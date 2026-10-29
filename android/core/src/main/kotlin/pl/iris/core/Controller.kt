package pl.iris.core

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * Sterowanie ekranem rozmowy (docs/spec.md, sekcja 14): stany, napisy, komunikaty. Mowa i synteza
 * są portami ([Recognizer], [Speaker]), które na telefonie implementuje Android; reszta działa na JVM.
 */

enum class Phase { IDLE, LISTENING, THINKING, SPEAKING }

enum class RecognizerError { NO_MATCH, NO_PERMISSION, UNAVAILABLE, NETWORK, BUSY, OTHER }

interface Recognizer {
    interface Listener {
        /** Poziom mikrofonu 0-1 podczas słuchania. */
        fun onLevel(level: Float)

        fun onResult(text: String)

        fun onError(error: RecognizerError)
    }

    fun start(listener: Listener, preferOffline: Boolean)

    /** Kończy nagrywanie; wynik przyjdzie przez [Listener.onResult]. */
    fun stop()

    /** Porzuca rozpoznawanie bez wyniku. */
    fun cancel()
}

interface Speaker {
    /** Czy system ma głos do czytania odpowiedzi po polsku. */
    val available: Boolean

    /** Czyta [text]; [onDone] wywołane po zakończeniu albo po [stop]. */
    fun speak(text: String, onDone: () -> Unit)

    fun stop()
}

data class Turn(
    val id: Int,
    val heard: String? = null,
    val reply: String? = null,
    val note: String? = null,
    val warn: Boolean = false,
)

data class UiState(
    val phase: Phase = Phase.IDLE,
    val turns: List<Turn> = emptyList(),
    val notice: String? = null,
    /** Poziom dźwięku dla fali (0-1): mikrofon przy słuchaniu. */
    val level: Float = 0f,
    val hasKey: Boolean = false,
)

object Texts {
    const val STATE_IDLE = "Dotknij i mów"
    const val STATE_IDLE_NO_KEY = "Wpisz klucz API w ustawieniach"
    const val STATE_LISTENING = "Słucham"
    const val STATE_THINKING = "Przygotowuję odpowiedź"
    const val STATE_SPEAKING = "Mówię"

    const val NO_KEY = "Wpisz klucz API w ustawieniach, żeby rozmawiać z modelem. Polecenia pamięci działają bez klucza."
    const val NO_MATCH = "Nic nie rozpoznano. Mów bliżej mikrofonu."
    const val NO_PERMISSION = "Brak zgody na mikrofon. Zezwól na nią w ustawieniach systemu albo pisz."
    const val RECOGNIZER_UNAVAILABLE = "Ten telefon nie ma usługi rozpoznawania mowy. Możesz pisać."
    const val RECOGNIZER_NETWORK = "Rozpoznawanie mowy potrzebuje sieci albo pakietu offline. Spróbuj ponownie."
    const val RECOGNIZER_BUSY = "Rozpoznawanie mowy jest zajęte. Spróbuj za chwilę."
    const val RECOGNIZER_OTHER = "Rozpoznawanie mowy nie zadziałało. Spróbuj ponownie."
    const val REFUSED = "Model odmówił odpowiedzi."
    const val UNAVAILABLE = "Brak połączenia z modelem. Spróbuj ponownie."
    const val CONFIG = "Model odrzucił klucz API albo ustawienia. Sprawdź je w ustawieniach."
    const val FAILED = "Nie udało się przygotować odpowiedzi."
    const val NO_VOICE = "System nie ma polskiego głosu, więc odpowiedzi są tylko w napisach."

    fun state(phase: Phase, hasKey: Boolean): String = when (phase) {
        Phase.IDLE -> if (hasKey) STATE_IDLE else STATE_IDLE_NO_KEY
        Phase.LISTENING -> STATE_LISTENING
        Phase.THINKING -> STATE_THINKING
        Phase.SPEAKING -> STATE_SPEAKING
    }

    fun recognizer(error: RecognizerError): String = when (error) {
        RecognizerError.NO_MATCH -> NO_MATCH
        RecognizerError.NO_PERMISSION -> NO_PERMISSION
        RecognizerError.UNAVAILABLE -> RECOGNIZER_UNAVAILABLE
        RecognizerError.NETWORK -> RECOGNIZER_NETWORK
        RecognizerError.BUSY -> RECOGNIZER_BUSY
        RecognizerError.OTHER -> RECOGNIZER_OTHER
    }

    fun chat(kind: ChatErrorKind): String = when (kind) {
        ChatErrorKind.REFUSED -> REFUSED
        ChatErrorKind.UNAVAILABLE -> UNAVAILABLE
        ChatErrorKind.CONFIG -> CONFIG
        ChatErrorKind.FAILED -> FAILED
    }
}

const val MAX_TURNS = 20

class IrisController(
    private val conversation: Conversation,
    private val recognizer: Recognizer,
    private val speaker: Speaker,
    private val scope: CoroutineScope,
    hasKey: Boolean = false,
    private var preferOffline: Boolean = false,
) {
    private val _state = MutableStateFlow(UiState(hasKey = hasKey))
    val state: StateFlow<UiState> = _state.asStateFlow()

    private var counter = 0
    private var job: Job? = null

    /** Dotknięcie przycisku: zaczyna słuchanie, kończy je albo przerywa mowę. */
    fun onTalk() {
        when (_state.value.phase) {
            Phase.IDLE -> startListening()
            Phase.LISTENING -> recognizer.stop()
            Phase.SPEAKING -> {
                speaker.stop()
                _state.update { it.copy(phase = Phase.IDLE) }
            }
            Phase.THINKING -> Unit
        }
    }

    /** Wypowiedź wpisana z klawiatury. */
    fun submit(text: String) {
        val t = text.trim()
        if (t.isEmpty()) return
        when (_state.value.phase) {
            Phase.LISTENING -> recognizer.cancel()
            Phase.SPEAKING -> speaker.stop()
            Phase.THINKING -> return
            Phase.IDLE -> Unit
        }
        handle(t)
    }

    /** Użytkownik nie dał zgody na mikrofon. */
    fun onPermissionDenied() = _state.update { it.copy(phase = Phase.IDLE, notice = Texts.NO_PERMISSION) }

    fun clearNotice() = _state.update { it.copy(notice = null) }

    fun configure(hasKey: Boolean, preferOffline: Boolean) {
        this.preferOffline = preferOffline
        _state.update { it.copy(hasKey = hasKey) }
    }

    fun shutdown() {
        recognizer.cancel()
        speaker.stop()
        job?.cancel()
    }

    private fun startListening() {
        _state.update { it.copy(phase = Phase.LISTENING, notice = null, level = 0f) }
        recognizer.start(
            object : Recognizer.Listener {
                override fun onLevel(level: Float) = _state.update {
                    if (it.phase == Phase.LISTENING) it.copy(level = level.coerceIn(0f, 1f)) else it
                }

                override fun onResult(text: String) {
                    if (_state.value.phase != Phase.LISTENING) return
                    val t = text.trim()
                    if (t.isEmpty()) onError(RecognizerError.NO_MATCH) else handle(t)
                }

                override fun onError(error: RecognizerError) {
                    if (_state.value.phase != Phase.LISTENING) return
                    _state.update { it.copy(phase = Phase.IDLE, level = 0f, notice = Texts.recognizer(error)) }
                }
            },
            preferOffline,
        )
    }

    private fun handle(text: String) {
        val id = ++counter
        _state.update {
            it.copy(phase = Phase.THINKING, level = 0f, notice = null, turns = (it.turns + Turn(id, heard = text)).takeLast(MAX_TURNS))
        }
        job = scope.launch {
            try {
                val reply = conversation.say(text)
                setTurn(id) { it.copy(reply = reply.text, note = null) }
                if (speaker.available && reply.text.isNotBlank()) {
                    _state.update { if (it.phase == Phase.THINKING) it.copy(phase = Phase.SPEAKING) else it }
                    speaker.speak(reply.text) { finishSpeaking(id) }
                } else {
                    if (!speaker.available) _state.update { if (it.notice == null) it.copy(notice = Texts.NO_VOICE) else it }
                    idle()
                }
            } catch (e: CancellationException) {
                throw e
            } catch (_: ChatDisabled) {
                failTurn(id, Texts.NO_KEY, warn = false)
            } catch (e: ChatError) {
                failTurn(id, Texts.chat(e.kind), warn = true)
            } catch (_: Exception) {
                failTurn(id, Texts.FAILED, warn = true) // np. baza faktów; treść wyjątku może zawierać dane
            }
        }
    }

    private fun finishSpeaking(id: Int) {
        if (counter == id) _state.update { if (it.phase == Phase.SPEAKING) it.copy(phase = Phase.IDLE) else it }
    }

    private fun idle() = _state.update { it.copy(phase = Phase.IDLE) }

    private fun failTurn(id: Int, note: String, warn: Boolean) {
        setTurn(id) { it.copy(note = note, warn = warn) }
        idle()
    }

    private fun setTurn(id: Int, change: (Turn) -> Turn) = _state.update { s ->
        s.copy(turns = s.turns.map { if (it.id == id) change(it) else it })
    }
}
