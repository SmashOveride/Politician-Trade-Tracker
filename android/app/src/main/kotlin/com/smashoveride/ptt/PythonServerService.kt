package com.smashoveride.ptt

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/**
 * Foreground service that hosts the app's Flask backend (via Chaquopy) for
 * as long as Android is willing to keep it alive -- including while
 * MainActivity/its WebView are backgrounded, which is what lets the
 * existing background auto-refresh scheduler thread (backend/app.py's
 * _auto_refresh_loop) keep checking for new disclosures the same way it
 * does on desktop.
 */
class PythonServerService : Service() {

    companion object {
        const val ACTION_SERVER_READY = "com.smashoveride.ptt.SERVER_READY"
        const val EXTRA_PORT = "port"
        private const val NOTIFICATION_CHANNEL_ID = "ptt_server"
        private const val NOTIFICATION_ID = 1

        // Read by MainActivity if it's created *after* the service has
        // already started the server (e.g. the Activity was recreated on
        // rotation, or the user re-opened the app while the service was
        // still running in the background) -- avoids waiting for a fresh
        // broadcast that will never come since the server is already up.
        @Volatile
        var lastKnownPort: Int = -1
            private set
    }

    private val scope = CoroutineScope(Dispatchers.IO)

    override fun onCreate() {
        super.onCreate()
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(applicationContext))
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Must be called within a few seconds of the service starting, per
        // Android's foreground-service rules -- do this before touching
        // Python at all, since starting the interpreter + Flask can take a
        // moment on first launch.
        startForeground(NOTIFICATION_ID, buildNotification())

        if (lastKnownPort <= 0) {
            scope.launch {
                val py = Python.getInstance()
                val module = py.getModule("backend.android_entry")
                // backend.android_entry.start() is idempotent -- safe even
                // if onStartCommand() runs more than once (e.g. START_STICKY
                // restarting this service after Android killed it).
                val port = module.callAttr("start", filesDir.absolutePath).toInt()
                lastKnownPort = port
                broadcastPort(port)
            }
        } else {
            broadcastPort(lastKnownPort)
        }

        return START_STICKY
    }

    private fun broadcastPort(port: Int) {
        val intent = Intent(ACTION_SERVER_READY).apply {
            setPackage(packageName)
            putExtra(EXTRA_PORT, port)
        }
        sendBroadcast(intent)
    }

    private fun buildNotification(): Notification {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                NOTIFICATION_CHANNEL_ID,
                getString(R.string.app_name),
                NotificationManager.IMPORTANCE_LOW,
            )
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(channel)
        }

        val openAppIntent = Intent(this, MainActivity::class.java)
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            openAppIntent,
            PendingIntent.FLAG_IMMUTABLE,
        )

        return NotificationCompat.Builder(this, NOTIFICATION_CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText("Running in the background")
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
