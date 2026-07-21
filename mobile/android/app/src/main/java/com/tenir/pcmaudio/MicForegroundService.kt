package com.tenir.pcmaudio

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat

/**
 * Minimal microphone foreground service so the OS keeps audio capture running while the
 * app is backgrounded. Started/stopped by [PcmAudioModule] around a live session.
 */
class MicForegroundService : Service() {

  override fun onBind(intent: Intent?): IBinder? = null

  override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    val channelId = "tenir.capture"
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
      val mgr = getSystemService(NotificationManager::class.java)
      if (mgr.getNotificationChannel(channelId) == null) {
        mgr.createNotificationChannel(
            NotificationChannel(channelId, "Live capture", NotificationManager.IMPORTANCE_LOW))
      }
    }
    val notification: Notification =
        NotificationCompat.Builder(this, channelId)
            .setContentTitle("Tenir")
            .setContentText("Capturing audio")
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .build()

    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
      startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
    } else {
      startForeground(NOTIFICATION_ID, notification)
    }
    return START_STICKY
  }

  companion object {
    private const val NOTIFICATION_ID = 4711

    fun start(context: Context) {
      val intent = Intent(context, MicForegroundService::class.java)
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        context.startForegroundService(intent)
      } else {
        context.startService(intent)
      }
    }

    fun stop(context: Context) {
      context.stopService(Intent(context, MicForegroundService::class.java))
    }
  }
}
