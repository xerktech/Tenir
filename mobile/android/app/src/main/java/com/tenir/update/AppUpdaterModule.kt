package com.tenir.update

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.util.Log
import androidx.core.content.FileProvider
import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import com.facebook.react.bridge.WritableMap
import com.facebook.react.modules.core.DeviceEventManagerModule
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

/**
 * `AppUpdater` native module (XERK-63) — the Android half of the in-app updater,
 * mirroring the Turma Android client's `Updater`: download a newer release APK and
 * hand it to the system package installer, a stopgap self-update for a sideloaded
 * app until it ships on a store.
 *
 * The pure release-picking + version compare and the (anonymous, public) GitHub
 * release fetch live on the JS side (`src/lib/updater.ts`); this module owns the two
 * things only native code can do: reading the installed `versionName`, and the
 * download + install handoff.
 *
 * The OS verifies the APK's signature on install — the real integrity gate for
 * updating an already-installed app, which is why we don't re-verify a checksum here
 * and why the release must keep a stable signing key (see `mobile/android` build.gradle
 * / XERK-60: an update whose certificate differs from the installed one is refused).
 */
class AppUpdaterModule(private val reactContext: ReactApplicationContext) :
    ReactContextBaseJavaModule(reactContext) {

  companion object {
    const val NAME = "AppUpdater"
    private const val TAG = "AppUpdater"
    private const val PROGRESS_EVENT = "AppUpdater.progress"
    // Matches the FileProvider authority declared in AndroidManifest.xml.
    private const val AUTHORITY_SUFFIX = ".updates"
    private const val CONNECT_TIMEOUT_MS = 15_000
    private const val READ_TIMEOUT_MS = 120_000
  }

  override fun getName(): String = NAME

  // Required so a JS `NativeEventEmitter` can subscribe without warnings.
  @ReactMethod fun addListener(eventName: String) = Unit

  @ReactMethod fun removeListeners(count: Int) = Unit

  /** The installed app's `versionName` (e.g. "0.1.5") for the JS-side compare. */
  @ReactMethod
  fun getInstalledVersion(promise: Promise) {
    try {
      val info = reactContext.packageManager.getPackageInfo(reactContext.packageName, 0)
      promise.resolve(info.versionName ?: "")
    } catch (e: Exception) {
      promise.reject("E_VERSION", e)
    }
  }

  /**
   * Download the APK at [url] to app cache and hand it to the system installer.
   * Emits `AppUpdater.progress` ({version, pct}) while downloading, then resolves
   * with a status string:
   *   - "installing"       — the installer intent was fired (user confirms in the OS UI);
   *   - "needs_permission" — API 26+ first needs the user to allow installs from this
   *                          app, so we opened that settings screen; a re-tap installs.
   * Rejects on a download or install-handoff failure.
   */
  @ReactMethod
  fun downloadAndInstall(url: String, version: String, promise: Promise) {
    thread(isDaemon = true) {
      val file =
          try {
            download(url, version)
          } catch (e: Exception) {
            Log.w(TAG, "update download failed", e)
            promise.reject("E_DOWNLOAD", e)
            return@thread
          }
      try {
        promise.resolve(install(file))
      } catch (e: Exception) {
        Log.w(TAG, "install handoff failed", e)
        promise.reject("E_INSTALL", e)
      }
    }
  }

  private fun emitProgress(version: String, pct: Int) {
    val params: WritableMap =
        Arguments.createMap().apply {
          putString("version", version)
          putInt("pct", pct)
        }
    reactContext
        .getJSModule(DeviceEventManagerModule.RCTDeviceEventEmitter::class.java)
        .emit(PROGRESS_EVENT, params)
  }

  private fun download(url: String, version: String): File {
    val dir = File(reactContext.cacheDir, "updates").apply { mkdirs() }
    // Only ever keep the one APK we're installing now.
    dir.listFiles()?.forEach { if (it.name.endsWith(".apk")) it.delete() }
    val out = File(dir, "tenir-$version.apk")

    val conn = URL(url).openConnection() as HttpURLConnection
    // A GitHub asset download 302s to a signed objects host (https→https); the
    // default same-protocol redirect following handles that hop.
    conn.instanceFollowRedirects = true
    conn.connectTimeout = CONNECT_TIMEOUT_MS
    conn.readTimeout = READ_TIMEOUT_MS
    try {
      conn.connect()
      if (conn.responseCode !in 200..299) throw IOException("HTTP ${conn.responseCode}")
      val total = conn.contentLengthLong
      conn.inputStream.use { input ->
        out.outputStream().use { output ->
          val buf = ByteArray(64 * 1024)
          var read = 0L
          var lastPct = -1
          while (true) {
            val n = input.read(buf)
            if (n < 0) break
            output.write(buf, 0, n)
            read += n
            if (total > 0) {
              val pct = ((read * 100) / total).toInt()
              if (pct != lastPct) {
                lastPct = pct
                emitProgress(version, pct)
              }
            }
          }
        }
      }
    } finally {
      conn.disconnect()
    }
    return out
  }

  private fun install(file: File): String {
    // On API 26+ an app must be granted "install unknown apps" before it can request
    // an install. If it isn't yet, send the user to that settings screen and report
    // back so the banner offers a re-tap that installs once they're back.
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O &&
        !reactContext.packageManager.canRequestPackageInstalls()) {
      startActivitySafely(
          Intent(
                  Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                  Uri.parse("package:${reactContext.packageName}"),
              )
              .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
      return "needs_permission"
    }
    val uri =
        FileProvider.getUriForFile(
            reactContext, "${reactContext.packageName}$AUTHORITY_SUFFIX", file)
    startActivitySafely(
        Intent(Intent.ACTION_VIEW).apply {
          setDataAndType(uri, "application/vnd.android.package-archive")
          addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
          addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        })
    return "installing"
  }

  private fun startActivitySafely(intent: Intent) {
    // Prefer the foreground activity; fall back to the app context (the intents
    // above set FLAG_ACTIVITY_NEW_TASK, which a context-started activity requires).
    val activity: Activity? = currentActivity
    if (activity != null) activity.startActivity(intent) else reactContext.startActivity(intent)
  }
}
