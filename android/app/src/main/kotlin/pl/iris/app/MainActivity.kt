package pl.iris.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
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
    val modelNotice by container.modelNotice.collectAsState()
    val pickModel = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) {
            runCatching { context.contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
            container.importModel(uri)
        }
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
        )
        Screen.SETTINGS -> SettingsScreen(
            initial = settings,
            importStatus = importStatus ?: modelNotice,
            onPickModel = { pickModel.launch(arrayOf("*/*")) },
            onRemoveModel = container::removeModel,
            onSave = container::applySettings,
            onBack = { screen = Screen.CONVERSATION },
        )
        Screen.MEMORY -> MemoryScreen(
            conversation = container.conversation,
            onBack = { screen = Screen.CONVERSATION },
        )
    }
}
