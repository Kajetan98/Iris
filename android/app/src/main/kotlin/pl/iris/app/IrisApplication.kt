package pl.iris.app

import android.app.Application
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import android.net.Uri
import pl.iris.core.EngineKind
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

    private val transport = UrlConnectionTransport()
    val updater = AppUpdater(app, transport, scope)
    val models = ModelFiles(app)
    private val generator = LiteRtGenerator(app, models, scope)

    /** Komunikat o wyborze modelu (np. zły plik); null, gdy nie ma czego pokazać. */
    val modelNotice = MutableStateFlow<String?>(null)

    val conversation = ConversationHost(
        store = RowFactStore(SystemClock, SqliteFactRows(app)),
        clock = SystemClock,
        transport = transport,
        scope = scope,
        initial = _settings.value,
        local = generator,
    )

    val controller = IrisController(
        conversation = conversation,
        recognizer = AndroidRecognizer(app),
        speaker = AndroidSpeaker(app),
        scope = scope,
        ready = _settings.value.ready,
        preferOffline = _settings.value.preferOffline,
    )

    init {
        if (_settings.value.checkUpdates) updater.check()
        generator.useModel(if (_settings.value.engine == EngineKind.LOCAL) _settings.value.localModel else "")
    }

    fun applySettings(settings: Settings) {
        settingsStore.save(settings)
        _settings.value = settings
        generator.useModel(if (settings.engine == EngineKind.LOCAL) settings.localModel else "")
        conversation.update(settings)
        controller.configure(settings.ready, settings.preferOffline)
    }

    /** Kopiuje wskazany plik modelu i przełącza na silnik lokalny. */
    fun importModel(uri: Uri) {
        modelNotice.value = null
        scope.launch {
            try {
                val name = models.import(uri)
                applySettings(_settings.value.copy(engine = EngineKind.LOCAL, localModel = name))
            } catch (e: ModelImportException) {
                modelNotice.update { e.message }
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (e: Exception) {
                modelNotice.update { "Nie udało się wybrać pliku (${e.javaClass.simpleName}). Spróbuj ponownie albo wskaż plik z innego folderu, np. Pobrane." }
            }
        }
    }

    fun removeModel() {
        modelNotice.value = null
        generator.useModel("")
        models.clear()
        applySettings(_settings.value.copy(localModel = ""))
    }
}
