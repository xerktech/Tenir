package com.tenir.audioplayer

import android.media.AudioAttributes
import android.media.MediaPlayer
import android.os.Handler
import android.os.Looper
import android.util.Log
import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import com.facebook.react.bridge.WritableMap
import com.facebook.react.modules.core.DeviceEventManagerModule

/**
 * `AudioPlayer` native module (XERK-67) — in-app playback of a retained
 * conversation's audio for the History detail screen, so a recording plays with a
 * seek bar inside the app instead of being handed off to the browser.
 *
 * It wraps a single Android [MediaPlayer]: `load` streams the clip URL (the api
 * serves it `audio/wav` with byte-range support, so seeking works), then
 * `play`/`pause`/`seek`/`release` drive it. While a clip is loaded it emits
 * `AudioPlayer.tick` ({positionMs, durationMs, playing, ended}) on a ~250 ms
 * cadence so the JS seek bar tracks the playhead; the pure state machine that
 * consumes those ticks lives on the JS side (`src/lib/audioPlayer.ts`).
 *
 * All MediaPlayer calls are marshalled onto the main looper — MediaPlayer isn't
 * thread-safe and React methods arrive on the native-modules thread.
 */
class AudioPlayerModule(private val reactContext: ReactApplicationContext) :
    ReactContextBaseJavaModule(reactContext) {

  companion object {
    const val NAME = "AudioPlayer"
    private const val TAG = "AudioPlayer"
    private const val TICK_EVENT = "AudioPlayer.tick"
    private const val TICK_MS = 250L
  }

  private val main = Handler(Looper.getMainLooper())
  private var player: MediaPlayer? = null
  private var ended = false

  // Re-posts itself every TICK_MS while a clip is playing, streaming the playhead
  // to JS. Stopped whenever playback isn't advancing (paused, ended, released).
  private val ticker =
      object : Runnable {
        override fun run() {
          emitTick()
          val p = player
          if (p != null && p.isPlaying) main.postDelayed(this, TICK_MS)
        }
      }

  override fun getName(): String = NAME

  // Required so a JS `NativeEventEmitter` can subscribe without warnings.
  @ReactMethod fun addListener(eventName: String) = Unit

  @ReactMethod fun removeListeners(count: Int) = Unit

  /**
   * Prepare [url] for playback, replacing any clip already loaded. Resolves the
   * clip length in milliseconds once prepared; rejects on a prepare/IO error.
   */
  @ReactMethod
  fun load(url: String, promise: Promise) {
    main.post {
      teardown()
      ended = false
      val mp = MediaPlayer()
      player = mp
      var settled = false
      mp.setAudioAttributes(
          AudioAttributes.Builder()
              .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
              .setUsage(AudioAttributes.USAGE_MEDIA)
              .build())
      mp.setOnPreparedListener {
        if (settled) return@setOnPreparedListener
        settled = true
        emitTick() // seed the seek bar with the (paused) starting position
        promise.resolve(durationMs())
      }
      mp.setOnCompletionListener {
        ended = true
        main.removeCallbacks(ticker)
        emitTick()
      }
      mp.setOnErrorListener { _, what, extra ->
        Log.w(TAG, "MediaPlayer error what=$what extra=$extra")
        if (!settled) {
          settled = true
          promise.reject("E_PLAYBACK", "playback error ($what/$extra)")
        }
        true // handled — suppress the default reset-to-error path
      }
      try {
        mp.setDataSource(url)
        mp.prepareAsync()
      } catch (e: Exception) {
        Log.w(TAG, "load failed", e)
        if (!settled) {
          settled = true
          promise.reject("E_LOAD", e)
        }
        teardown()
      }
    }
  }

  @ReactMethod
  fun play(promise: Promise) {
    main.post {
      val mp = player
      if (mp == null) {
        promise.reject("E_STATE", "no clip loaded")
        return@post
      }
      // Replaying a clip that ran to the end restarts it from the top.
      if (ended) {
        mp.seekTo(0)
        ended = false
      }
      mp.start()
      main.removeCallbacks(ticker)
      main.post(ticker)
      promise.resolve(null)
    }
  }

  @ReactMethod
  fun pause(promise: Promise) {
    main.post {
      player?.pause()
      main.removeCallbacks(ticker)
      emitTick()
      promise.resolve(null)
    }
  }

  @ReactMethod
  fun seek(positionMs: Double, promise: Promise) {
    main.post {
      val mp = player
      if (mp == null) {
        promise.reject("E_STATE", "no clip loaded")
        return@post
      }
      val target = positionMs.toInt().coerceIn(0, durationMs())
      // Scrubbing back into a finished clip re-arms it so play() resumes here.
      if (ended && target < durationMs()) ended = false
      mp.seekTo(target)
      emitTick()
      promise.resolve(null)
    }
  }

  @ReactMethod
  fun release(promise: Promise) {
    main.post {
      teardown()
      promise.resolve(null)
    }
  }

  // Host teardown: don't leak a MediaPlayer (and the audio focus) if JS goes away.
  override fun invalidate() {
    main.post { teardown() }
    super.invalidate()
  }

  private fun teardown() {
    main.removeCallbacks(ticker)
    ended = false
    player?.let {
      try {
        it.release()
      } catch (e: Exception) {
        Log.w(TAG, "release failed", e)
      }
    }
    player = null
  }

  /** The clip length in ms, or 0 while it's still unknown (MediaPlayer returns -1). */
  private fun durationMs(): Int = player?.duration?.takeIf { it > 0 } ?: 0

  private fun emitTick() {
    val mp = player ?: return
    val duration = durationMs()
    val position = if (ended) duration else mp.currentPosition.coerceIn(0, if (duration > 0) duration else Int.MAX_VALUE)
    val params: WritableMap =
        Arguments.createMap().apply {
          putInt("positionMs", position)
          putInt("durationMs", duration)
          putBoolean("playing", !ended && mp.isPlaying)
          putBoolean("ended", ended)
        }
    reactContext
        .getJSModule(DeviceEventManagerModule.RCTDeviceEventEmitter::class.java)
        .emit(TICK_EVENT, params)
  }
}
