package io.github.marthofdoom.harmony

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.util.Log
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress
import kotlin.concurrent.thread

private const val TAG = "HarmonyRtp"

/** Plays an inter-instance audio route on this phone. An instance's
 *  `module-rtp-send` (the RTP transport of Harmony's routing) unicasts RTP
 *  payload-type 10 — 16-bit big-endian PCM, 44.1 kHz, stereo — to this device;
 *  we strip the 12-byte RTP header, byte-swap to little-endian, and feed the
 *  PCM to an AudioTrack. No native ROC library required.
 *
 *  Best-effort for the alpha: assumes the default 44.1 kHz/stereo format and
 *  runs on a background thread (a production build would use a foreground
 *  MediaSession service so playback survives the screen locking). */
class RtpReceiver(private val port: Int = 5004) {

    @Volatile private var running = false
    private var socket: DatagramSocket? = null
    private var track: AudioTrack? = null

    val isRunning: Boolean get() = running

    fun start() {
        if (running) return
        running = true
        thread(name = "harmony-rtp", isDaemon = true) { loop() }
    }

    private fun loop() {
        val sampleRate = 44100
        val channelMask = AudioFormat.CHANNEL_OUT_STEREO
        val encoding = AudioFormat.ENCODING_PCM_16BIT
        val minBuf = AudioTrack.getMinBufferSize(sampleRate, channelMask, encoding)
        val bufSize = maxOf(minBuf, sampleRate) // ~ a few hundred ms of headroom

        val at: AudioTrack
        val sock: DatagramSocket
        try {
            at = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setSampleRate(sampleRate)
                        .setChannelMask(channelMask)
                        .setEncoding(encoding)
                        .build()
                )
                .setBufferSizeInBytes(bufSize)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build()
            sock = DatagramSocket(null).apply {
                reuseAddress = true
                soTimeout = 1000
                bind(InetSocketAddress(port))
            }
        } catch (e: Exception) {
            Log.w(TAG, "couldn't start receiver", e)
            running = false
            return
        }
        track = at
        socket = sock
        at.play()

        val buf = ByteArray(4096)
        val pkt = DatagramPacket(buf, buf.size)
        while (running) {
            try {
                sock.receive(pkt)
                val len = pkt.length
                if (len <= 12) continue // RTP header is 12 bytes; nothing to play
                val off = 12
                val n = len - off
                val out = ByteArray(n)
                var i = 0
                while (i + 1 < n) { // big-endian → little-endian
                    out[i] = buf[off + i + 1]
                    out[i + 1] = buf[off + i]
                    i += 2
                }
                at.write(out, 0, n)
            } catch (_: java.net.SocketTimeoutException) {
                // idle; loop to re-check `running`
            } catch (e: Exception) {
                if (running) Log.d(TAG, "rtp recv hiccup: ${e.message}")
            }
        }
    }

    fun stop() {
        running = false
        runCatching { socket?.close() }
        runCatching { track?.pause(); track?.flush(); track?.stop(); track?.release() }
        socket = null
        track = null
    }
}
