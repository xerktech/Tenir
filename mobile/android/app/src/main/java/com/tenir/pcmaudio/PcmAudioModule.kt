package com.tenir.pcmaudio

import android.Manifest
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Base64
import androidx.core.content.ContextCompat
import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import com.facebook.react.bridge.WritableMap
import com.facebook.react.modules.core.DeviceEventManagerModule
import java.io.ByteArrayOutputStream
import java.util.concurrent.atomic.AtomicBoolean

/**
 * `PcmAudio` native module — phone-microphone capture for the live session.
 *
 * Captures 16 kHz, mono, signed-16-bit little-endian PCM (matching the api STT input,
 * so nothing is resampled on the wire) and emits each ~100 ms slice as a base64 string
 * on the `PcmAudio.chunk` event. While streaming, a microphone-typed foreground service
 * keeps capture alive when the app is backgrounded.
 */
class PcmAudioModule(private val reactContext: ReactApplicationContext) :
    ReactContextBaseJavaModule(reactContext) {

  companion object {
    const val NAME = "PcmAudio"
    private const val SAMPLE_RATE = 16_000
    private const val CHUNK_MS = 100
    // 16 kHz * 0.1 s * 2 bytes (s16le) mono = 3200 bytes per ~100 ms slice.
    private const val CHUNK_BYTES = SAMPLE_RATE / 1000 * CHUNK_MS * 2
  }

  private val recording = AtomicBoolean(false)
  private var thread: Thread? = null

  override fun getName(): String = NAME

  // Required so JS `NativeEventEmitter` can subscribe without warnings.
  @ReactMethod fun addListener(eventName: String) = Unit

  @ReactMethod fun removeListeners(count: Int) = Unit

  private fun hasPermission(): Boolean =
      ContextCompat.checkSelfPermission(reactContext, Manifest.permission.RECORD_AUDIO) ==
          PackageManager.PERMISSION_GRANTED

  private fun emit(base64: String) {
    val params: WritableMap = Arguments.createMap().apply { putString("base64", base64) }
    reactContext
        .getJSModule(DeviceEventManagerModule.RCTDeviceEventEmitter::class.java)
        .emit("PcmAudio.chunk", params)
  }

  private fun newRecorder(): AudioRecord {
    val minBuf =
        AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
    val bufSize = maxOf(minBuf, CHUNK_BYTES * 4)
    return AudioRecord(
        MediaRecorder.AudioSource.VOICE_RECOGNITION,
        SAMPLE_RATE,
        AudioFormat.CHANNEL_IN_MONO,
        AudioFormat.ENCODING_PCM_16BIT,
        bufSize)
  }

  /** Begin continuous capture; slices stream on `PcmAudio.chunk`. */
  @ReactMethod
  fun start(promise: Promise) {
    if (recording.get()) {
      promise.resolve(true)
      return
    }
    if (!hasPermission()) {
      promise.reject("E_PERMISSION", "RECORD_AUDIO not granted")
      return
    }
    val recorder =
        try {
          newRecorder()
        } catch (e: Exception) {
          promise.reject("E_INIT", e)
          return
        }
    if (recorder.state != AudioRecord.STATE_INITIALIZED) {
      recorder.release()
      promise.reject("E_INIT", "AudioRecord failed to initialize")
      return
    }

    // Keep capture alive while backgrounded (mic foreground-service type).
    MicForegroundService.start(reactContext)
    recording.set(true)
    thread =
        Thread {
              val buffer = ByteArray(CHUNK_BYTES)
              try {
                recorder.startRecording()
                while (recording.get()) {
                  var off = 0
                  while (off < buffer.size && recording.get()) {
                    val n = recorder.read(buffer, off, buffer.size - off)
                    if (n <= 0) break
                    off += n
                  }
                  if (off > 0) {
                    val slice = if (off == buffer.size) buffer else buffer.copyOf(off)
                    emit(Base64.encodeToString(slice, Base64.NO_WRAP))
                  }
                }
              } catch (_: Exception) {
                // Swallow; stop() releases resources below.
              } finally {
                try {
                  recorder.stop()
                } catch (_: Exception) {}
                recorder.release()
              }
            }
            .also {
              it.isDaemon = true
              it.start()
            }
    promise.resolve(true)
  }

  /** Stop continuous capture and tear down the foreground service. */
  @ReactMethod
  fun stop(promise: Promise) {
    recording.set(false)
    thread?.let {
      try {
        it.join(500)
      } catch (_: InterruptedException) {}
    }
    thread = null
    MicForegroundService.stop(reactContext)
    promise.resolve(null)
  }

  /** Record one fixed-length clip; resolves with base64 16 kHz s16le mono PCM. */
  @ReactMethod
  fun recordOnce(seconds: Double, promise: Promise) {
    if (!hasPermission()) {
      promise.reject("E_PERMISSION", "RECORD_AUDIO not granted")
      return
    }
    Thread {
          val recorder =
              try {
                newRecorder()
              } catch (e: Exception) {
                promise.reject("E_INIT", e)
                return@Thread
              }
          if (recorder.state != AudioRecord.STATE_INITIALIZED) {
            recorder.release()
            promise.reject("E_INIT", "AudioRecord failed to initialize")
            return@Thread
          }
          val total = (SAMPLE_RATE * 2 * seconds).toInt()
          val out = ByteArrayOutputStream(maxOf(total, CHUNK_BYTES))
          val buf = ByteArray(CHUNK_BYTES)
          try {
            recorder.startRecording()
            while (out.size() < total) {
              val n = recorder.read(buf, 0, buf.size)
              if (n <= 0) break
              out.write(buf, 0, minOf(n, total - out.size()))
            }
            promise.resolve(Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP))
          } catch (e: Exception) {
            promise.reject("E_RECORD", e)
          } finally {
            try {
              recorder.stop()
            } catch (_: Exception) {}
            recorder.release()
          }
        }
        .also { it.isDaemon = true }
        .start()
  }

  /**
   * Android requests RECORD_AUDIO via `PermissionsAndroid` on the JS side; this just
   * reports the current grant state (the JS layer only calls it on iOS).
   */
  @ReactMethod
  fun requestPermission(promise: Promise) {
    promise.resolve(hasPermission())
  }
}
