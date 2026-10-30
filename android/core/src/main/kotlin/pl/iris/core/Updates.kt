package pl.iris.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/** Aktualizacje aplikacji (docs/spec.md, sekcja 14, "Aktualizacje"). */

const val RELEASE_BASE = "https://github.com/Kajetan98/Iris/releases/download/"
const val UPDATE_URL = RELEASE_BASE + "android-latest/latest.json"

data class UpdateInfo(val versionCode: Int, val versionName: String, val apkUrl: String, val sha256: String)

sealed interface UpdateCheck {
    data class Available(val info: UpdateInfo) : UpdateCheck

    data object UpToDate : UpdateCheck

    data class Failed(val reason: String) : UpdateCheck
}

interface HttpGet {
    /** Rzuca [TransportException] przy braku połączenia lub przekroczeniu [timeoutMs]. */
    suspend fun get(url: String, timeoutMs: Long): HttpResponse
}

private val SHA256 = Regex("[0-9a-fA-F]{64}")

/** Null, gdy plik nie ma wymaganych pól, ma zły adres APK albo zły skrót. */
fun parseUpdateInfo(body: String): UpdateInfo? {
    val obj = runCatching { Json.parseToJsonElement(body).jsonObject }.getOrNull() ?: return null
    val code = obj.int("versionCode") ?: return null
    val name = obj.str("versionName") ?: return null
    val apk = obj.str("apk") ?: return null
    val sha = obj.str("sha256") ?: return null
    if (code <= 0 || !apk.startsWith(RELEASE_BASE) || !SHA256.matches(sha)) return null
    return UpdateInfo(code, name, apk, sha.lowercase())
}

private fun JsonObject.str(key: String): String? = get(key)?.jsonPrimitive?.takeIf { it.isString }?.contentOrNull
private fun JsonObject.int(key: String): Int? = get(key)?.jsonPrimitive?.takeIf { !it.isString }?.intOrNull

class UpdateChecker(private val http: HttpGet, private val url: String = UPDATE_URL, private val timeoutMs: Long = 10_000) {
    suspend fun check(installedVersionCode: Int): UpdateCheck {
        val response = try {
            http.get(url, timeoutMs)
        } catch (e: TransportException) {
            return UpdateCheck.Failed("Brak połączenia z GitHubem.")
        }
        if (response.status != 200) return UpdateCheck.Failed("GitHub zwrócił kod ${response.status}.")
        val info = parseUpdateInfo(response.body) ?: return UpdateCheck.Failed("Plik aktualizacji ma nieprawidłowy format.")
        return if (info.versionCode > installedVersionCode) UpdateCheck.Available(info) else UpdateCheck.UpToDate
    }
}
