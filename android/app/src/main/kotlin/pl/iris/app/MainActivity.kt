package pl.iris.app

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import pl.iris.app.ui.ConversationScreen
import pl.iris.app.ui.IrisTheme
import pl.iris.app.ui.MemoryScreen
import pl.iris.app.ui.SettingsScreen

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
        )
        val container = (application as IrisApplication).container
        setContent { IrisTheme { IrisRoot(container) } }
    }

    override fun onResume() {
        super.onResume()
        val container = (application as IrisApplication).container
        container.micGranted.value = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        container.foreground.value = true
    }

    override fun onPause() {
        (application as IrisApplication).container.foreground.value = false
        super.onPause()
    }
}

private enum class Screen { CONVERSATION, SETTINGS, MEMORY }

@Composable
private fun IrisRoot(container: AppContainer) {
    var screen by rememberSaveable { mutableStateOf(Screen.CONVERSATION) }
    val ui by container.controller.state.collectAsState()
    val settings by container.settings.collectAsState()
    val context = androidx.compose.ui.platform.LocalContext.current
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) container.controller.onTalk() else container.controller.onPermissionDenied()
    }

    val importStatus by container.models.progress.collectAsState()
    val update by container.updater.state.collectAsState()
    val wakeInstalled by container.wakeModel.installed.collectAsState()
    val wakeProgress by container.wakeModel.progress.collectAsState()
    val wakeNotice by container.wakeNotice.collectAsState()
    val wakeProblem by container.wakeProblem.collectAsState()
    val modelNotice by container.modelNotice.collectAsState()
    val pickModel = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) {
            runCatching { context.contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
            container.importModel(uri)
        }
    }

    val wakePermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        container.micGranted.value = granted
    }
    val pickWakeZip = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) container.importWakeModel(uri)
    }
    // Hasło wybudzające potrzebuje zgody na mikrofon od razu po włączeniu, a ekran ma nie wygasać.
    LaunchedEffect(settings.wakeWord) {
        if (settings.wakeWord && !container.micGranted.value) wakePermission.launch(Manifest.permission.RECORD_AUDIO)
    }
    DisposableEffect(settings.wakeWord) {
        val window = (context as? Activity)?.window
        if (settings.wakeWord) window?.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        onDispose { window?.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON) }
    }

    BackHandler(enabled = screen != Screen.CONVERSATION) { screen = Screen.CONVERSATION }

    when (screen) {
        Screen.CONVERSATION -> ConversationScreen(
            ui = ui,
            onTalk = {
                val granted = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
                if (granted) container.controller.onTalk() else permission.launch(Manifest.permission.RECORD_AUDIO)
            },
            onSubmit = container.controller::submit,
            onDismissNotice = container.controller::clearNotice,
            onOpenMemory = { screen = Screen.MEMORY },
            onOpenSettings = { screen = Screen.SETTINGS },
            update = update,
            onInstallUpdate = container.updater::install,
        )
        Screen.SETTINGS -> SettingsScreen(
            initial = settings,
            importStatus = importStatus ?: modelNotice,
            onPickModel = { pickModel.launch(arrayOf("*/*")) },
            onRemoveModel = container::removeModel,
            versionName = container.updater.installedName,
            update = update,
            onCheckUpdate = container.updater::check,
            onInstallUpdate = container.updater::install,
            wakeInstalled = wakeInstalled,
            wakeStatus = wakeProgress ?: wakeNotice ?: wakeProblem,
            onDownloadWake = container::downloadWakeModel,
            onPickWakeZip = { pickWakeZip.launch(arrayOf("*/*")) },
            onRemoveWake = container::removeWakeModel,
            onSave = container::applySettings,
            onBack = { screen = Screen.CONVERSATION },
        )
        Screen.MEMORY -> MemoryScreen(
            conversation = container.conversation,
            onBack = { screen = Screen.CONVERSATION },
        )
    }
}
