package pl.iris.app

import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runInterruptible
import pl.iris.core.HttpResponse
import pl.iris.core.HttpTransport
import pl.iris.core.TransportException

/**
 * Transport HTTP z platformy (HttpURLConnection), bez dodatkowych bibliotek. Treść żądania i odpowiedzi
 * (rozmowa, klucz API) nie jest logowana.
 */
class UrlConnectionTransport : HttpTransport {
    override suspend fun post(url: String, headers: Map<String, String>, body: String, timeoutMs: Long): HttpResponse =
        try {
            runInterruptible(Dispatchers.IO) { execute(url, headers, body, timeoutMs.toInt()) }
        } catch (e: IOException) {
            throw TransportException("błąd sieci: ${e.javaClass.simpleName}", e)
        }

    private fun execute(url: String, headers: Map<String, String>, body: String, timeoutMs: Int): HttpResponse {
        val connection = URL(url).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = "POST"
            connection.connectTimeout = minOf(timeoutMs, 15_000)
            connection.readTimeout = timeoutMs
            connection.doOutput = true
            connection.instanceFollowRedirects = false
            for ((k, v) in headers) connection.setRequestProperty(k, v)
            connection.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            val status = connection.responseCode
            val stream = if (status >= 400) connection.errorStream else connection.inputStream
            val text = stream?.use { it.readBytes().toString(Charsets.UTF_8) }.orEmpty()
            return HttpResponse(status, text)
        } finally {
            connection.disconnect()
        }
    }
}
