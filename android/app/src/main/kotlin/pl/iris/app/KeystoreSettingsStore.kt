package pl.iris.app

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import pl.iris.core.DEFAULT_CLAUDE_MODEL
import pl.iris.core.Settings
import pl.iris.core.SettingsStore

/**
 * Ustawienia w SharedPreferences (pamięć prywatna aplikacji). Klucz API jest zaszyfrowany AES-GCM kluczem z
 * Android Keystore i nie opuszcza telefonu poza żądaniami do Claude API (spec, sekcja 14).
 */
class KeystoreSettingsStore(context: Context) : SettingsStore {
    private val prefs = context.applicationContext.getSharedPreferences("iris_settings", Context.MODE_PRIVATE)

    override fun load(): Settings = Settings(
        apiKey = prefs.getString(KEY_API, null)?.let(::decrypt).orEmpty(),
        workspaceId = prefs.getString(KEY_WORKSPACE, null).orEmpty(),
        model = prefs.getString(KEY_MODEL, null) ?: DEFAULT_CLAUDE_MODEL,
        useMemory = prefs.getBoolean(KEY_MEMORY, false),
        learn = prefs.getBoolean(KEY_LEARN, false),
        preferOffline = prefs.getBoolean(KEY_OFFLINE, false),
    )

    override fun save(settings: Settings) {
        prefs.edit().apply {
            val key = settings.apiKey.trim()
            if (key.isEmpty()) remove(KEY_API) else putString(KEY_API, encrypt(key))
            putString(KEY_WORKSPACE, settings.workspaceId.trim())
            putString(KEY_MODEL, settings.model)
            putBoolean(KEY_MEMORY, settings.useMemory)
            putBoolean(KEY_LEARN, settings.learn)
            putBoolean(KEY_OFFLINE, settings.preferOffline)
        }.apply()
    }

    private fun secretKey(): SecretKey {
        val store = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return generator.generateKey()
    }

    private fun encrypt(plain: String): String {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, secretKey())
        val encrypted = cipher.doFinal(plain.toByteArray(Charsets.UTF_8))
        return Base64.encodeToString(cipher.iv + encrypted, Base64.NO_WRAP)
    }

    /** Null, gdy nie da się odszyfrować (np. po przywróceniu danych bez klucza z Keystore): traktujemy to jak brak klucza. */
    private fun decrypt(token: String): String? = try {
        val bytes = Base64.decode(token, Base64.NO_WRAP)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(128, bytes, 0, IV_BYTES))
        String(cipher.doFinal(bytes, IV_BYTES, bytes.size - IV_BYTES), Charsets.UTF_8)
    } catch (_: Exception) {
        null
    }

    private companion object {
        const val ANDROID_KEYSTORE = "AndroidKeyStore"
        const val ALIAS = "iris_api_key"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val IV_BYTES = 12
        const val KEY_API = "api_key_enc"
        const val KEY_WORKSPACE = "workspace_id"
        const val KEY_MODEL = "model"
        const val KEY_MEMORY = "use_memory"
        const val KEY_LEARN = "learn"
        const val KEY_OFFLINE = "prefer_offline"
    }
}
