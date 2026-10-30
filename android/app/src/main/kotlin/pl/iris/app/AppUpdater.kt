package pl.iris.app

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings as AndroidSettings
import androidx.core.content.FileProvider
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import pl.iris.core.HttpGet
import pl.iris.core.UpdateCheck
import pl.iris.core.UpdateChecker
import pl.iris.core.UpdateInfo

sealed interface UpdateState {
    data object Idle : UpdateState

    data object Checking : UpdateState

    data object UpToDate : UpdateState

    data class Available(val info: UpdateInfo) : UpdateState

    data class Downloading(val info: UpdateInfo, val percent: Int) : UpdateState

    /** System nie pozwala jeszcze IRIS instalować aplikacji; otwarto jego ustawienie, trzeba dotknąć ponownie. */
    data class NeedsPermission(val info: UpdateInfo) : UpdateState

    data class Failed(val reason: String, val info: UpdateInfo? = null) : UpdateState
}

/**
 * Sprawdzanie, pobieranie i instalacja nowych wersji z wydania `android-latest` (docs/spec.md, sekcja 14,
 * "Aktualizacje"). Instalację zatwierdza użytkownik w oknie systemu, które sprawdza też podpis pliku.
 */
class AppUpdater(private val context: Context, http: HttpGet, private val scope: CoroutineScope) {
    private val checker = UpdateChecker(http)
    private val _state = MutableStateFlow<UpdateState>(UpdateState.Idle)
    val state: StateFlow<UpdateState> = _state

    val installedCode: Int
        get() = runCatching { context.packageManager.getPackageInfo(context.packageName, 0).longVersionCode.toInt() }.getOrDefault(0)

    val installedName: String
        get() = runCatching { context.packageManager.getPackageInfo(context.packageName, 0).versionName }.getOrNull().orEmpty()

    fun check() {
        if (_state.value is UpdateState.Checking || _state.value is UpdateState.Downloading) return
        _state.value = UpdateState.Checking
        scope.launch {
            _state.value = when (val result = checker.check(installedCode)) {
                is UpdateCheck.Available -> UpdateState.Available(result.info)
                UpdateCheck.UpToDate -> UpdateState.UpToDate
                is UpdateCheck.Failed -> UpdateState.Failed(result.reason)
            }
        }
    }

    fun install(info: UpdateInfo) {
        if (_state.value is UpdateState.Downloading) return
        if (!context.packageManager.canRequestPackageInstalls()) {
            _state.value = UpdateState.NeedsPermission(info)
            context.startActivity(
                Intent(AndroidSettings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${context.packageName}"))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
            return
        }
        _state.value = UpdateState.Downloading(info, 0)
        scope.launch {
            try {
                val file = download(info)
                _state.value = UpdateState.Available(info)
                val uri = FileProvider.getUriForFile(context, "${context.packageName}.updates", file)
                context.startActivity(
                    Intent(Intent.ACTION_VIEW)
                        .setDataAndType(uri, "application/vnd.android.package-archive")
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK),
                )
            } catch (e: CancellationException) {
                throw e
            } catch (e: UpdateException) {
                _state.value = UpdateState.Failed(e.message.orEmpty(), info)
            } catch (e: Exception) {
                _state.value = UpdateState.Failed("Nie udało się zainstalować aktualizacji (${e.javaClass.simpleName}).", info)
            }
        }
    }

    private class UpdateException(message: String) : Exception(message)

    private suspend fun download(info: UpdateInfo): File = withContext(Dispatchers.IO) {
        val dir = File(context.cacheDir, "updates").apply { mkdirs() }
        dir.listFiles()?.forEach { it.delete() }
        val target = File(dir, "iris-${info.versionCode}.apk")
        val digest = MessageDigest.getInstance("SHA-256")
        val connection = URL(info.apkUrl).openConnection() as HttpURLConnection
        try {
            connection.connectTimeout = 15_000
            connection.readTimeout = 30_000
            connection.instanceFollowRedirects = true
            if (connection.responseCode != 200) throw UpdateException("GitHub zwrócił kod ${connection.responseCode} przy pobieraniu.")
            val total = connection.contentLengthLong
            connection.inputStream.use { input ->
                target.outputStream().use { out ->
                    val buffer = ByteArray(1 shl 16)
                    var done = 0L
                    var last = -1
                    while (true) {
                        val n = input.read(buffer)
                        if (n < 0) break
                        out.write(buffer, 0, n)
                        digest.update(buffer, 0, n)
                        done += n
                        if (total > 0) {
                            val percent = (done * 100 / total).toInt()
                            if (percent != last) {
                                last = percent
                                _state.value = UpdateState.Downloading(info, percent)
                            }
                        }
                    }
                }
            }
        } catch (e: IOException) {
            target.delete()
            throw UpdateException("Pobieranie nie powiodło się (brak połączenia lub miejsca na telefonie).")
        } finally {
            connection.disconnect()
        }
        val actual = digest.digest().joinToString("") { "%02x".format(it) }
        if (actual != info.sha256) {
            target.delete()
            throw UpdateException("Pobrany plik ma inny skrót niż w wydaniu, więc go nie zainstalowano.")
        }
        target
    }
}
