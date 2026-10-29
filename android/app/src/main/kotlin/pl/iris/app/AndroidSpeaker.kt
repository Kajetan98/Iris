package pl.iris.app

import android.content.Context
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import java.util.Locale
import pl.iris.core.Speaker

/** Synteza mowy z systemu (spec, sekcja 14): `TextToSpeech`, polski głos, jeśli system go ma. */
class AndroidSpeaker(context: Context) : Speaker {
    @Volatile
    private var ready = false

    @Volatile
    private var onDone: (() -> Unit)? = null

    private val tts: TextToSpeech = TextToSpeech(context.applicationContext) { status -> init(status) }

    override val available: Boolean get() = ready

    private fun init(status: Int) {
        if (status != TextToSpeech.SUCCESS) return
        val result = tts.setLanguage(Locale.forLanguageTag("pl-PL"))
        ready = result == TextToSpeech.LANG_AVAILABLE || result == TextToSpeech.LANG_COUNTRY_AVAILABLE ||
            result == TextToSpeech.LANG_COUNTRY_VAR_AVAILABLE
        tts.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) = Unit

            override fun onDone(utteranceId: String?) = finish()

            @Deprecated("Zastąpione przez onError(String?, Int)")
            override fun onError(utteranceId: String?) = finish()

            override fun onError(utteranceId: String?, errorCode: Int) = finish()

            override fun onStop(utteranceId: String?, interrupted: Boolean) = finish()
        })
    }

    override fun speak(text: String, onDone: () -> Unit) {
        this.onDone = onDone
        val limited = text.take(TextToSpeech.getMaxSpeechInputLength())
        if (tts.speak(limited, TextToSpeech.QUEUE_FLUSH, null, UTTERANCE_ID) == TextToSpeech.ERROR) finish()
    }

    override fun stop() {
        onDone = null
        tts.stop()
    }

    private fun finish() {
        val callback = onDone
        onDone = null
        callback?.invoke()
    }

    private companion object {
        const val UTTERANCE_ID = "iris"
    }
}
