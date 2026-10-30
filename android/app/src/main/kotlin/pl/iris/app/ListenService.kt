package pl.iris.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat

/**
 * Usługa pierwszoplanowa typu mikrofon, która utrzymuje proces przy życiu, gdy hasło wybudzające ma działać w tle
 * (docs/spec.md, sekcja 14). Samo rozpoznawanie jest w [AppContainer]; usługa daje powiadomienie i blokadę procesora.
 */
class ListenService : Service() {
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel(CHANNEL, "Słuchanie hasła", NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val notification = Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setContentTitle("IRIS słucha hasła")
            .setContentText("Powiedz „Hej Iris” albo „Irysku”. Wyłączysz to w ustawieniach aplikacji.")
            .setContentIntent(open)
            .setOngoing(true)
            .build()
        ServiceCompat.startForeground(this, NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        running = true
        wakeLock = getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "iris:listen")
            .apply { acquire() }
    }

    // Po zabiciu procesu usługa nie wraca sama: Android 14+ nie pozwala uruchomić mikrofonu z tła.
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_NOT_STICKY

    override fun onDestroy() {
        wakeLock?.takeIf { it.isHeld }?.release()
        wakeLock = null
        running = false
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val CHANNEL = "iris_listen"
        private const val NOTIFICATION_ID = 1

        @Volatile
        var running = false
            private set

        /** Wywołać tylko przy widocznej aplikacji. Zwraca false, gdy system odmówił startu. */
        fun start(context: Context): Boolean =
            runCatching { ContextCompat.startForegroundService(context, Intent(context, ListenService::class.java)) }.isSuccess

        fun stop(context: Context) {
            context.stopService(Intent(context, ListenService::class.java))
        }
    }
}
