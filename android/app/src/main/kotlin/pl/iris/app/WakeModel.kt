package pl.iris.app

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import java.io.File
import java.io.FilterInputStream
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.zip.ZipInputStream
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.withContext

const val WAKE_MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-pl-0.22.zip"

class WakeModelException(message: String) : Exception(message)

/**
 * Model polski Vosk dla hasła wybudzającego (docs/spec.md, sekcja 14): pobierany z sieci albo wskazany jako
 * plik .zip, rozpakowywany do pamięci prywatnej aplikacji.
 */
class WakeModel(private val context: Context) {
    private val dir get() = File(context.filesDir, "vosk-model-pl")
    private val tmp get() = File(context.filesDir, "vosk-model-pl.tmp")

    val path: String get() = dir.path

    private val _installed = MutableStateFlow(isReady())
    val installed: StateFlow<Boolean> = _installed

    private val _progress = MutableStateFlow<String?>(null)

    /** Tekst stanu pobierania lub rozpakowywania; null, gdy nic się nie dzieje. */
    val progress: StateFlow<String?> = _progress

    private fun isReady() = File(dir, "am/final.mdl").isFile

    fun clear() {
        dir.deleteRecursively()
        tmp.deleteRecursively()
        _installed.value = false
    }

    suspend fun download() = withContext(Dispatchers.IO) {
        val connection = URL(WAKE_MODEL_URL).openConnection() as HttpURLConnection
        try {
            _progress.value = "Pobieram model..."
            connection.connectTimeout = 15_000
            connection.readTimeout = 30_000
            connection.instanceFollowRedirects = true
            if (connection.responseCode != 200) throw WakeModelException("Serwer modelu zwrócił kod ${connection.responseCode}. Pobierz plik .zip modelu ręcznie i wskaż go przyciskiem obok.")
            unpack(connection.inputStream, connection.contentLengthLong)
        } catch (e: IOException) {
            tmp.deleteRecursively()
            throw WakeModelException("Pobieranie nie powiodło się (brak połączenia lub miejsca na telefonie).")
        } finally {
            connection.disconnect()
            _progress.value = null
        }
    }

    suspend fun importZip(uri: Uri) = withContext(Dispatchers.IO) {
        try {
            var total = -1L
            context.contentResolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)?.use { c ->
                if (c.moveToFirst() && !c.isNull(0)) total = c.getLong(0)
            }
            val input = context.contentResolver.openInputStream(uri) ?: throw WakeModelException("Nie można otworzyć wybranego pliku.")
            _progress.value = "Rozpakowuję model..."
            unpack(input, total)
        } catch (e: IOException) {
            tmp.deleteRecursively()
            throw WakeModelException("Rozpakowanie nie powiodło się (zły plik .zip albo brak miejsca na telefonie).")
        } finally {
            _progress.value = null
        }
    }

    /** Rozpakowuje archiwum bez katalogu nadrzędnego; ścieżki wychodzące poza katalog modelu są odrzucane. */
    private fun unpack(raw: InputStream, total: Long) {
        tmp.deleteRecursively()
        tmp.mkdirs()
        val root = tmp.canonicalPath + File.separator
        var read = 0L
        var written = 0L
        var lastPercent = -1
        val counting = object : FilterInputStream(raw) {
            override fun read(): Int = super.read().also { if (it >= 0) read++ }

            override fun read(b: ByteArray, off: Int, len: Int): Int = super.read(b, off, len).also { if (it > 0) read += it }
        }
        ZipInputStream(counting).use { zip ->
            val buffer = ByteArray(1 shl 16)
            while (true) {
                val entry = zip.nextEntry ?: break
                val rel = entry.name.replace('\\', '/').substringAfter('/', "")
                if (rel.isEmpty()) continue
                val target = File(tmp, rel)
                if (!target.canonicalPath.startsWith(root)) throw WakeModelException("Archiwum zawiera niedozwoloną ścieżkę.")
                if (entry.isDirectory) {
                    target.mkdirs()
                    continue
                }
                target.parentFile?.mkdirs()
                target.outputStream().use { out ->
                    while (true) {
                        val n = zip.read(buffer)
                        if (n < 0) break
                        out.write(buffer, 0, n)
                        written += n
                        if (written > MAX_UNPACKED) throw WakeModelException("Archiwum jest zbyt duże jak na model hasła.")
                    }
                }
                if (total > 0) {
                    val percent = (read * 100 / total).toInt().coerceAtMost(100)
                    if (percent != lastPercent) {
                        lastPercent = percent
                        _progress.value = "Model: $percent%"
                    }
                }
            }
        }
        if (!File(tmp, "am/final.mdl").isFile) {
            tmp.deleteRecursively()
            throw WakeModelException("To nie jest model Vosk (brak pliku am/final.mdl).")
        }
        dir.deleteRecursively()
        if (!tmp.renameTo(dir)) throw WakeModelException("Nie udało się zapisać modelu w pamięci aplikacji.")
        _installed.value = true
    }

    private companion object {
        const val MAX_UNPACKED = 600L * 1024 * 1024
    }
}
