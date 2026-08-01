package com.tenir.pcmaudio

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import androidx.core.app.NotificationCompat

import com.tenir.R

/**
 * Microphone foreground service so the OS keeps audio capture running while the app is
 * backgrounded. Started/stopped by [PcmAudioModule] around a live session; its ongoing
 * notification is the running-session indicator, and [update] rewrites that notification
 * in place so live cues surface in the shade while the app is backgrounded (XERK-163).
 */
class MicForegroundService : Service() {

  override fun onBind(intent: Intent?): IBinder? = null

  override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    running = true
    ensureChannel(this)
    val title = intent?.getStringExtra(EXTRA_TITLE) ?: DEFAULT_TITLE
    val text = intent?.getStringExtra(EXTRA_TEXT) ?: DEFAULT_TEXT
    val notification = buildNotification(this, title, text)

    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
      startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
    } else {
      startForeground(NOTIFICATION_ID, notification)
    }
    return START_STICKY
  }

  override fun onDestroy() {
    running = false
    cancelRevert()
    super.onDestroy()
  }

  companion object {
    private const val NOTIFICATION_ID = 4711
    private const val CHANNEL_ID = "tenir.capture"
    private const val EXTRA_TITLE = "title"
    private const val EXTRA_TEXT = "text"
    private const val DEFAULT_TITLE = "Tenir"
    private const val DEFAULT_TEXT = "Live session · capturing audio"

    // Tracks whether the service is live so a late [update] after stop() can't post a
    // stray notification once the foreground service (and its notification) is gone.
    @Volatile private var running = false

    // Reverts a cue to the plain capturing line when its window closes. Runs on the main
    // looper, which the foreground service keeps alive while the app is backgrounded — so
    // it fires on time even though the JS cue-release timer is frozen there (XERK-163).
    private val handler = Handler(Looper.getMainLooper())
    private var pendingRevert: Runnable? = null

    private fun cancelRevert() {
      pendingRevert?.let { handler.removeCallbacks(it) }
      pendingRevert = null
    }

    private fun ensureChannel(context: Context) {
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        val mgr = context.getSystemService(NotificationManager::class.java)
        if (mgr.getNotificationChannel(CHANNEL_ID) == null) {
          mgr.createNotificationChannel(
              NotificationChannel(CHANNEL_ID, "Live capture", NotificationManager.IMPORTANCE_LOW))
        }
      }
    }

    /** Tapping the notification returns to the running session rather than a cold launch. */
    private fun contentIntent(context: Context): PendingIntent? {
      val launch =
          context.packageManager.getLaunchIntentForPackage(context.packageName)?.apply {
            addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
          } ?: return null
      var flags = PendingIntent.FLAG_UPDATE_CURRENT
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) flags = flags or PendingIntent.FLAG_IMMUTABLE
      return PendingIntent.getActivity(context, 0, launch, flags)
    }

    private fun buildNotification(context: Context, title: String, text: String): Notification =
        NotificationCompat.Builder(context, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(text)
            // Expand to the full cue body when the shade entry is opened.
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            // Keep the session/brand context visible even when a cue fills the title.
            .setSubText(DEFAULT_TITLE)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentIntent(contentIntent(context))
            .setOngoing(true)
            // LOW-importance updates are silent already; be explicit so a stream of
            // cues never buzzes or peeks.
            .setOnlyAlertOnce(true)
            .build()

    fun start(context: Context) {
      val intent = Intent(context, MicForegroundService::class.java)
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        context.startForegroundService(intent)
      } else {
        context.startService(intent)
      }
    }

    /**
     * Rewrite the ongoing notification's content in place — e.g. surface the live cue
     * while the app is backgrounded (XERK-163). No-op unless the service is running, so
     * updates racing a stop() can't leave a lingering notification. Reuses NOTIFICATION_ID
     * so it replaces (not stacks on) the foreground notification.
     *
     * When [endsAt] is a future wall-clock time (epoch ms), the content is a cue and is
     * auto-reverted to the plain capturing line at that time via [handler] — the cue must
     * clear itself here because the JS release timer that would normally do it is frozen
     * while the app is backgrounded. `endsAt <= 0` means no expiry (the capturing line),
     * so any pending revert is simply cancelled.
     */
    fun update(context: Context, title: String, text: String, endsAt: Long) {
      if (!running) return
      val appContext = context.applicationContext
      ensureChannel(appContext)
      post(appContext, title, text)

      cancelRevert()
      if (endsAt > 0L) {
        val revert =
            Runnable {
              pendingRevert = null
              if (running) post(appContext, DEFAULT_TITLE, DEFAULT_TEXT)
            }
        pendingRevert = revert
        handler.postDelayed(revert, (endsAt - System.currentTimeMillis()).coerceAtLeast(0L))
      }
    }

    private fun post(context: Context, title: String, text: String) {
      context
          .getSystemService(NotificationManager::class.java)
          .notify(NOTIFICATION_ID, buildNotification(context, title, text))
    }

    fun stop(context: Context) {
      cancelRevert()
      context.stopService(Intent(context, MicForegroundService::class.java))
    }
  }
}
