package pl.iris.app

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import pl.iris.core.Recognizer
import pl.iris.core.RecognizerError

/**
 * Rozpoznawanie mowy z systemu (spec, sekcja 14): `SpeechRecognizer`, język pl-PL. Zależnie od telefonu nagranie
 * trafia do usługi systemu (zwykle Google) albo jest rozpoznawane offline. Wszystkie wywołania z głównego wątku.
 */
class AndroidRecognizer(private val context: Context) : Recognizer {
    private var recognizer: SpeechRecognizer? = null

    override fun start(listener: Recognizer.Listener, preferOffline: Boolean) {
        if (!SpeechRecognizer.isRecognitionAvailable(context)) {
            listener.onError(RecognizerError.UNAVAILABLE)
            return
        }
        val sr = recognizer ?: SpeechRecognizer.createSpeechRecognizer(context).also { recognizer = it }
        sr.setRecognitionListener(Bridge(listener))
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, "pl-PL")
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, preferOffline)
            putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, context.packageName)
        }
        sr.startListening(intent)
    }

    override fun stop() {
        recognizer?.stopListening()
    }

    override fun cancel() {
        recognizer?.cancel()
    }

    private class Bridge(private val listener: Recognizer.Listener) : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) = Unit

        override fun onBeginningOfSpeech() = Unit

        // rmsdB mieści się zwykle w przedziale od -2 do 10; do fali przeliczamy go na 0-1.
        override fun onRmsChanged(rmsdB: Float) = listener.onLevel(((rmsdB + 2f) / 12f).coerceIn(0f, 1f))

        override fun onBufferReceived(buffer: ByteArray?) = Unit

        override fun onEndOfSpeech() = Unit

        override fun onError(error: Int) = listener.onError(map(error))

        override fun onResults(results: Bundle?) {
            val best = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull().orEmpty()
            listener.onResult(best)
        }

        override fun onPartialResults(partialResults: Bundle?) = Unit

        override fun onEvent(eventType: Int, params: Bundle?) = Unit

        private fun map(error: Int): RecognizerError = when (error) {
            SpeechRecognizer.ERROR_NO_MATCH, SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> RecognizerError.NO_MATCH
            SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> RecognizerError.NO_PERMISSION
            SpeechRecognizer.ERROR_NETWORK, SpeechRecognizer.ERROR_NETWORK_TIMEOUT, SpeechRecognizer.ERROR_SERVER,
            ERROR_SERVER_DISCONNECTED, ERROR_LANGUAGE_NOT_SUPPORTED, ERROR_LANGUAGE_UNAVAILABLE,
            -> RecognizerError.NETWORK
            SpeechRecognizer.ERROR_RECOGNIZER_BUSY, SpeechRecognizer.ERROR_TOO_MANY_REQUESTS -> RecognizerError.BUSY
            else -> RecognizerError.OTHER
        }
    }

    private companion object {
        // Stałe z API 31+, a minSdk to 26.
        const val ERROR_SERVER_DISCONNECTED = 11
        const val ERROR_LANGUAGE_NOT_SUPPORTED = 12
        const val ERROR_LANGUAGE_UNAVAILABLE = 13
    }
}
