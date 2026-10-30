package pl.iris.app

import android.app.Application
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import pl.iris.core.wakeListening
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
    private val appContext = app
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

    // Hasło wybudzające: nasłuch tylko przy otwartej aplikacji, w spoczynku i z mikrofonem (spec, sekcja 14).
    val foreground = MutableStateFlow(false)
    val micGranted = MutableStateFlow(false)
    val wakeModel = WakeModel(app)
    val wakeNotice = MutableStateFlow<String?>(null)

    private val wake = WakeWordListener(wakeModel, scope) {
        delay(150) // mikrofon musi się zwolnić, zanim ruszy rozpoznawanie systemu
        withContext(Dispatchers.Main) { controller.onTalk() }
    }

    val wakeProblem: StateFlow<String?> = wake.problem

    init {
        scope.launch {
            combine(_settings, controller.state, foreground, micGranted, wakeModel.installed) { s, ui, fg, mic, installed ->
                Pair(wakeListening(s.wakeWord, installed, mic, fg || s.wakeBackground, ui.phase), s.wakeWord && installed)
            }.distinctUntilChanged().collect { (listen, loaded) ->
                if (listen) wake.start() else wake.stop()
                if (!loaded) wake.release()
            }
        }
    }

    // Słuchanie w tle: usługa działa, dopóki jest włączone i możliwe; startuje tylko przy widocznej aplikacji.
    init {
        scope.launch {
            combine(_settings, wakeModel.installed, micGranted, foreground) { s, installed, mic, fg ->
                val want = s.wakeWord && s.wakeBackground && installed && mic
                when {
                    !want -> ServiceAction.STOP
                    fg -> ServiceAction.START
                    else -> ServiceAction.KEEP
                }
            }.distinctUntilChanged().collect { action ->
                when (action) {
                    ServiceAction.START -> if (!ListenService.running) {
                        if (ListenService.start(appContext)) {
                            wakeNotice.value = null
                        } else {
                            wakeNotice.value = "Android nie pozwolił uruchomić słuchania w tle. Otwórz aplikację i spróbuj ponownie."
                        }
                    }
                    ServiceAction.STOP -> if (ListenService.running) ListenService.stop(appContext)
                    ServiceAction.KEEP -> Unit
                }
            }
        }
    }

    private enum class ServiceAction { START, STOP, KEEP }

    fun downloadWakeModel() = runWakeModel { wakeModel.download() }

    fun importWakeModel(uri: Uri) = runWakeModel { wakeModel.importZip(uri) }

    fun removeWakeModel() {
        wakeNotice.value = null
        wakeModel.clear()
    }

    private fun runWakeModel(block: suspend () -> Unit) {
        wakeNotice.value = null
        scope.launch {
            try {
                block()
            } catch (e: WakeModelException) {
                wakeNotice.value = e.message
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (e: Exception) {
                wakeNotice.value = "Nie udało się zainstalować modelu hasła (${e.javaClass.simpleName})."
            }
        }
    }

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
