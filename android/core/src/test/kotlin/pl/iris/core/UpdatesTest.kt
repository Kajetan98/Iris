package pl.iris.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlinx.coroutines.test.runTest

class UpdatesTest {
    private val sha = "a".repeat(64)
    private val apk = RELEASE_BASE + "android-latest/iris-debug.apk"

    private fun json(code: String = "42", name: String = "\"0.1.42\"", apk: String = "\"${this.apk}\"", sha: String = "\"${this.sha}\"") =
        """{"versionCode": $code, "versionName": $name, "apk": $apk, "sha256": $sha}"""

    private class FakeGet(private val result: Any) : HttpGet {
        val urls = mutableListOf<String>()

        override suspend fun get(url: String, timeoutMs: Long): HttpResponse {
            urls += url
            if (result is Exception) throw result
            return result as HttpResponse
        }
    }

    @Test
    fun `a valid file is parsed and the hash is normalized`() {
        assertEquals(UpdateInfo(42, "0.1.42", apk, sha), parseUpdateInfo(json(sha = "\"${sha.uppercase()}\"")))
    }

    @Test
    fun `invalid files are rejected`() {
        assertNull(parseUpdateInfo("nie json"))
        assertNull(parseUpdateInfo("[]"))
        assertNull(parseUpdateInfo(json(code = "0")))
        assertNull(parseUpdateInfo(json(code = "\"42\"")))
        assertNull(parseUpdateInfo(json(code = "4.5")))
        assertNull(parseUpdateInfo(json(apk = "\"https://example.com/iris.apk\"")))
        assertNull(parseUpdateInfo(json(apk = "\"http://github.com/Kajetan98/Iris/releases/download/x.apk\"")))
        assertNull(parseUpdateInfo(json(sha = "\"abc\"")))
        assertNull(parseUpdateInfo(json(name = "null")))
        assertNull(parseUpdateInfo("""{"versionCode": 1}"""))
    }

    @Test
    fun `only a higher version code is an update`() = runTest {
        val get = FakeGet(HttpResponse(200, json()))
        val checker = UpdateChecker(get)
        assertEquals(UpdateCheck.Available(UpdateInfo(42, "0.1.42", apk, sha)), checker.check(41))
        assertEquals(UpdateCheck.UpToDate, checker.check(42))
        assertEquals(UpdateCheck.UpToDate, checker.check(43))
        assertEquals(UPDATE_URL, get.urls.first())
    }

    @Test
    fun `failures become a message without throwing`() = runTest {
        assertEquals(UpdateCheck.Failed("Brak połączenia z GitHubem."), UpdateChecker(FakeGet(TransportException("x"))).check(1))
        assertEquals(UpdateCheck.Failed("GitHub zwrócił kod 404."), UpdateChecker(FakeGet(HttpResponse(404, ""))).check(1))
        assertEquals(UpdateCheck.Failed("Plik aktualizacji ma nieprawidłowy format."), UpdateChecker(FakeGet(HttpResponse(200, "x"))).check(1))
    }
}
