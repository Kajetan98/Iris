package pl.iris.app

import android.app.Application
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import pl.iris.core.ConversationHost
import pl.iris.core.IrisController
import pl.iris.core.RowFactStore
import pl.iris.core.Settings
import pl.iris.core.SettingsStore
import pl.iris.core.SystemClock

class IrisApplication : Application() {
    lateinit var container: AppContainer
        private set

    override fun onCreate() {
        super.onCreate()
        container = AppContainer(this)
    }
}

/** Obiekty żyjące tak długo jak proces: stan rozmowy przeżywa obrót ekranu i powrót do aplikacji. */
class AppContainer(app: Application) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val settingsStore: SettingsStore = KeystoreSettingsStore(app)
    private val _settings = MutableStateFlow(settingsStore.load())

    val settings: StateFlow<Settings> = _settings

    val conversation = ConversationHost(
        store = RowFactStore(SystemClock, SqliteFactRows(app)),
        clock = SystemClock,
        transport = UrlConnectionTransport(),
        scope = scope,
        initial = _settings.value,
    )

    val controller = IrisController(
        conversation = conversation,
        recognizer = AndroidRecognizer(app),
        speaker = AndroidSpeaker(app),
        scope = scope,
        hasKey = _settings.value.hasKey,
        preferOffline = _settings.value.preferOffline,
    )

    fun applySettings(settings: Settings) {
        settingsStore.save(settings)
        _settings.value = settings
        conversation.update(settings)
        controller.configure(settings.hasKey, settings.preferOffline)
    }
}
